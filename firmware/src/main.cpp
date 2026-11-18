// haunt node: one firmware for every ESP32 in a piece.
//
// The board knows nothing about the piece. It joins the haunt Wi-Fi, finds the
// broker, says hello, and is told its name and config (LED pin/count, inputs,
// thresholds) by the hub, which reads devices.yaml. It caches that config, so
// it boots straight into its last job even if the hub is not up yet.
//
// Wire protocol: haunt/topics.py. Serial: send `?` for the id line, `help` for commands.

#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPUpdate.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include <vector>

#ifndef HAUNT_FW
#define HAUNT_FW "dev"
#endif
#ifndef HAUNT_ENV
#define HAUNT_ENV "?"
#endif
#ifndef HAUNT_WIFI_SSID
#define HAUNT_WIFI_SSID "haunt"
#endif
#ifndef HAUNT_WIFI_PASS
#define HAUNT_WIFI_PASS "hauntnet"
#endif

static const char *FW = HAUNT_FW " " __DATE__ " " __TIME__;

Preferences prefs;
WiFiClient net;
PubSubClient mqtt(net);

String hwid, name, ssid, pass, brokerSetting, brokerResolved;
uint32_t lastStatus = 0, lastConnectTry = 0, bootMs = 0;
bool mqttWasUp = false;

// ---------------------------------------------------------------- topics
String tStatus() { return "haunt/n/" + name + "/status"; }
String tAssign() { return "haunt/assign/" + hwid; }
String tOut() { return "haunt/n/" + name + "/out/#"; }
String tIn(const String &input) { return "haunt/n/" + name + "/in/" + input; }

// ---------------------------------------------------------------- LEDs
enum Effect { OFF, SOLID, BREATHE, CHASE, RAINBOW, SPARKLE };

struct LedSpec {
  Effect effect = OFF;
  uint32_t c1 = 0xFFFFFF, c2 = 0x000000;
  float speed = 0.5f, bright = 1.0f, fade = 0.3f;
};

Adafruit_NeoPixel *strip = nullptr;
int ledCount = 0;
float maxBright = 0.5f;          // heat budget from devices.yaml; never exceeded
LedSpec spec, fallbackSpec;
std::vector<uint32_t> frame, fromFrame;
uint32_t fadeStart = 0, fadeMs = 0, identifyUntil = 0;

static uint32_t parseColor(const char *s, uint32_t dflt) {
  if (!s || s[0] != '#') return dflt;
  if (strlen(s) == 4) {  // #rgb
    uint32_t r = strtoul(String(s[1]).c_str(), 0, 16), g = strtoul(String(s[2]).c_str(), 0, 16),
             b = strtoul(String(s[3]).c_str(), 0, 16);
    return (r * 17) << 16 | (g * 17) << 8 | (b * 17);
  }
  return strtoul(s + 1, nullptr, 16);
}

static Effect parseEffect(const char *s) {
  if (!s) return SOLID;
  if (!strcmp(s, "off")) return OFF;
  if (!strcmp(s, "breathe")) return BREATHE;
  if (!strcmp(s, "chase")) return CHASE;
  if (!strcmp(s, "rainbow")) return RAINBOW;
  if (!strcmp(s, "sparkle")) return SPARKLE;
  return SOLID;
}

static LedSpec parseSpec(JsonVariantConst j) {
  LedSpec s;
  s.effect = parseEffect(j["effect"] | "solid");
  s.c1 = parseColor(j["color"] | "#ffffff", 0xFFFFFF);
  s.c2 = parseColor(j["color2"] | "#000000", 0);
  s.speed = j["speed"] | 0.5f;
  s.bright = constrain(j["brightness"] | 1.0f, 0.0f, 1.0f);
  s.fade = j["fade"] | 0.3f;
  return s;
}

static uint32_t scale(uint32_t c, float k) {
  uint8_t r = ((c >> 16) & 255) * k, g = ((c >> 8) & 255) * k, b = (c & 255) * k;
  return (uint32_t)r << 16 | (uint32_t)g << 8 | b;
}

static uint32_t blend(uint32_t a, uint32_t b, float t) {
  uint8_t r = ((a >> 16) & 255) * (1 - t) + ((b >> 16) & 255) * t;
  uint8_t g = ((a >> 8) & 255) * (1 - t) + ((b >> 8) & 255) * t;
  uint8_t bl = (a & 255) * (1 - t) + (b & 255) * t;
  return (uint32_t)r << 16 | (uint32_t)g << 8 | bl;
}

static uint32_t hsv(float h) {
  return Adafruit_NeoPixel::ColorHSV((uint16_t)(fmodf(h, 1.0f) * 65535), 255, 255);
}

static void renderEffect(const LedSpec &s, float t, std::vector<uint32_t> &out) {
  for (int i = 0; i < ledCount; i++) {
    uint32_t c = 0;
    switch (s.effect) {
      case OFF: c = 0; break;
      case SOLID: c = s.c1; break;
      case BREATHE: c = scale(s.c1, 0.15f + 0.85f * (0.5f - 0.5f * cosf(2 * PI * s.speed * t))); break;
      case CHASE: c = (int)floorf(i / 4.0f - t * s.speed * 8) % 2 ? s.c1 : s.c2; break;
      case RAINBOW: c = hsv((float)i / max(ledCount, 1) + t * s.speed * 0.2f); break;
      case SPARKLE: c = (esp_random() % 100) < 6 ? s.c1 : s.c2; break;
    }
    out[i] = scale(c, s.bright);
  }
}

void setLeds(const LedSpec &s) {
  fromFrame = frame;
  spec = s;
  fadeStart = millis();
  fadeMs = (uint32_t)(s.fade * 1000);
}

void configureLeds(JsonVariantConst cfg) {
  int pin = cfg["pin"] | -1, count = cfg["count"] | 0;
  String order = cfg["order"] | "GRB";
  maxBright = constrain(cfg["max_brightness"] | 0.5f, 0.0f, 1.0f);
  neoPixelType type = NEO_GRB;
  if (order == "RGB") type = NEO_RGB;
  else if (order == "BRG") type = NEO_BRG;
  else if (order == "RBG") type = NEO_RBG;
  else if (order == "GBR") type = NEO_GBR;
  else if (order == "BGR") type = NEO_BGR;
  if (strip && (strip->getPin() != pin || ledCount != count)) {
    strip->clear();
    strip->show();
    delete strip;
    strip = nullptr;
  }
  ledCount = (pin >= 0 && count > 0) ? count : 0;
  if (ledCount && !strip) {
    strip = new Adafruit_NeoPixel(ledCount, pin, type + NEO_KHZ800);
    strip->begin();
  }
  frame.assign(ledCount, 0);
  fromFrame.assign(ledCount, 0);
}

void tickLeds() {
  static uint32_t last = 0;
  uint32_t now = millis();
  if (!strip || now - last < 16) return;
  last = now;
  float t = now / 1000.0f;
  std::vector<uint32_t> next(ledCount);
  if (now < identifyUntil) {
    LedSpec id;
    id.effect = SOLID;
    id.c1 = (now / 150) % 2 ? 0xFFFFFF : 0x000000;
    renderEffect(id, t, next);
  } else {
    renderEffect(spec, t, next);
    if (now - fadeStart < fadeMs && fromFrame.size() == next.size()) {
      float k = (float)(now - fadeStart) / fadeMs;
      for (int i = 0; i < ledCount; i++) next[i] = blend(fromFrame[i], next[i], k);
    }
  }
  frame = next;
  for (int i = 0; i < ledCount; i++) strip->setPixelColor(i, scale(frame[i], maxBright));
  strip->show();
}

// ---------------------------------------------------------------- inputs
enum InKind { IN_BUTTON, IN_DIGITAL, IN_ANALOG };

struct Input {
  String name;
  InKind kind = IN_BUTTON;
  int pin = -1;
  bool pullup = true, invert = false, state = false;
  int threshold = 2000, hysteresis = 100, deadband = 40, value = 0, reported = -10000;
  uint32_t changedAt = 0, reportedAt = 0, reportMs = 0, debounceMs = 30;
};
std::vector<Input> inputs;

void publishIn(const Input &in, const char *event, int value, bool withValue) {
  if (!mqtt.connected()) return;
  JsonDocument d;
  d["event"] = event;
  if (withValue) d["value"] = value;
  char buf[96];
  size_t n = serializeJson(d, buf);
  mqtt.publish(tIn(in.name).c_str(), (uint8_t *)buf, n, false);
}

void configureInputs(JsonVariantConst cfg) {
  inputs.clear();
  for (JsonPairConst kv : cfg.as<JsonObjectConst>()) {
    Input in;
    in.name = kv.key().c_str();
    JsonVariantConst c = kv.value();
    String kind = c["kind"] | "button";
    in.kind = kind == "analog" ? IN_ANALOG : kind == "digital" ? IN_DIGITAL : IN_BUTTON;
    in.pin = c["pin"] | -1;
    in.pullup = c["pullup"] | true;
    in.invert = c["invert"] | false;
    in.threshold = c["threshold"] | 2000;
    in.hysteresis = c["hysteresis"] | 100;
    in.deadband = c["deadband"] | 40;
    in.reportMs = c["report_ms"] | 0;        // 0: only threshold crossings, no value stream
    in.debounceMs = c["debounce_ms"] | 30;
    if (in.pin < 0) continue;
    if (in.kind != IN_ANALOG) pinMode(in.pin, in.pullup ? INPUT_PULLUP : INPUT);
    inputs.push_back(in);
  }
}

void tickInputs() {
  uint32_t now = millis();
  for (auto &in : inputs) {
    if (in.kind == IN_ANALOG) {
      in.value = analogRead(in.pin);
      bool on = in.state ? in.value > in.threshold - in.hysteresis : in.value > in.threshold;
      if (in.invert) on = in.state ? in.value < in.threshold + in.hysteresis : in.value < in.threshold;
      if (on != in.state) {
        in.state = on;
        publishIn(in, on ? "on" : "off", in.value, true);
      }
      if (in.reportMs && now - in.reportedAt >= in.reportMs && abs(in.value - in.reported) >= in.deadband) {
        in.reportedAt = now;
        in.reported = in.value;
        publishIn(in, "value", in.value, true);
      }
    } else {
      bool raw = digitalRead(in.pin);
      bool active = in.pullup ? !raw : raw;  // pull-up buttons are active low
      if (in.invert) active = !active;
      if (active != in.state && now - in.changedAt > in.debounceMs) {
        in.state = active;
        in.changedAt = now;
        if (in.kind == IN_BUTTON) publishIn(in, active ? "press" : "release", 0, false);
        else publishIn(in, active ? "on" : "off", 0, false);
      }
    }
  }
}

// ---------------------------------------------------------------- config
void applyConfig(JsonVariantConst cfg) {
  configureLeds(cfg["leds"]);
  configureInputs(cfg["inputs"]);
  if (!cfg["fallback"].isNull()) fallbackSpec = parseSpec(cfg["fallback"]);
}

void loadSavedConfig() {
  String saved = prefs.getString("config", "");
  if (saved.isEmpty()) return;
  JsonDocument d;
  if (deserializeJson(d, saved)) return;
  applyConfig(d.as<JsonVariantConst>());
  // Until the hub speaks, show the fallback look (default: a dim breathe).
  fallbackSpec.effect = fallbackSpec.effect == OFF ? BREATHE : fallbackSpec.effect;
  fallbackSpec.bright = 0.3f;
  setLeds(fallbackSpec);
}

// ---------------------------------------------------------------- status
void publishStatus(bool online = true) {
  JsonDocument d;
  d["online"] = online;
  d["id"] = hwid;
  d["kind"] = "esp32";
  d["fw"] = FW;
  d["env"] = HAUNT_ENV;
  d["ip"] = WiFi.localIP().toString();
  d["rssi"] = WiFi.RSSI();
  d["uptime"] = (millis() - bootMs) / 1000;
  d["heap"] = ESP.getFreeHeap();
  JsonArray caps = d["caps"].to<JsonArray>();
  if (strip) caps.add("leds");
  for (auto &in : inputs) caps.add(in.name);
  String s;
  serializeJson(d, s);
  mqtt.publish(tStatus().c_str(), s.c_str(), true);
}

void whoami() {
  Serial.printf("HAUNT-ID id=%s name=%s fw=\"%s\" env=%s chip=%s ip=%s broker=%s mqtt=%s\n", hwid.c_str(),
                name.c_str(), FW, HAUNT_ENV, ESP.getChipModel(), WiFi.localIP().toString().c_str(),
                brokerResolved.c_str(), mqtt.connected() ? "up" : "down");
}

// ---------------------------------------------------------------- OTA
void doOta(const char *url) {
  Serial.printf("OTA from %s\n", url);
  JsonDocument d;
  d["online"] = true;
  d["id"] = hwid;
  d["kind"] = "esp32";
  d["fw"] = FW;
  d["updating"] = url;
  String s;
  serializeJson(d, s);
  mqtt.publish(tStatus().c_str(), s.c_str(), true);
  mqtt.loop();
  if (strip) {
    strip->clear();
    strip->show();
  }
  WiFiClient client;
  httpUpdate.rebootOnUpdate(true);
  t_httpUpdate_return r = httpUpdate.update(client, url);
  Serial.printf("OTA failed: %s\n", httpUpdate.getLastErrorString().c_str());
  (void)r;
}

// ---------------------------------------------------------------- MQTT
void onMessage(char *topic, byte *payload, unsigned int len) {
  String t(topic);
  JsonDocument d;
  bool empty = len == 0;
  if (!empty && deserializeJson(d, payload, len)) return;

  if (t == tAssign()) {
    String newName = empty ? hwid : String(d["name"] | hwid.c_str());
    if (!empty && !d["config"].isNull()) {
      String cfg;
      serializeJson(d["config"], cfg);
      if (cfg != prefs.getString("config", "")) prefs.putString("config", cfg);
      applyConfig(d["config"]);
    }
    if (newName != name) {
      Serial.printf("assigned name %s (was %s)\n", newName.c_str(), name.c_str());
      mqtt.publish(tStatus().c_str(), "", true);  // clear the old name's status
      prefs.putString("name", newName);
      name = newName;
      mqtt.disconnect();  // reconnect with the new name's will and subscriptions
    } else {
      publishStatus();
    }
    return;
  }
  String prefix = "haunt/n/" + name + "/out/";
  if (!t.startsWith(prefix)) return;
  String out = t.substring(prefix.length());
  if (out == "leds") setLeds(parseSpec(d.as<JsonVariantConst>()));
  else if (out == "identify") identifyUntil = millis() + (uint32_t)((d["seconds"] | 3.0f) * 1000);
  else if (out == "ota") doOta(d["url"] | "");
  else if (out == "reboot") ESP.restart();
}

bool resolveBroker() {
  String b = brokerSetting;
  if (b.isEmpty()) b = "haunt.local";
  if (b.endsWith(".local")) {
    IPAddress ip = MDNS.queryHost(b.substring(0, b.length() - 6), 1500);
    if (ip != IPAddress(0, 0, 0, 0)) {
      brokerResolved = ip.toString();
      return true;
    }
    if (!brokerSetting.isEmpty()) return false;
    // On the hub's own access point the hub is the gateway.
    brokerResolved = WiFi.gatewayIP().toString();
    return true;
  }
  brokerResolved = b;
  return true;
}

void tickMqtt() {
  if (mqtt.connected()) {
    mqtt.loop();
    if (millis() - lastStatus > 5000) {
      lastStatus = millis();
      publishStatus();
    }
    return;
  }
  if (mqttWasUp) {
    mqttWasUp = false;
    Serial.println("mqtt lost");
  }
  if (WiFi.status() != WL_CONNECTED || millis() - lastConnectTry < 2000) return;
  lastConnectTry = millis();
  if (!resolveBroker()) {
    Serial.printf("cannot resolve broker %s\n", brokerSetting.c_str());
    return;
  }
  mqtt.setServer(brokerResolved.c_str(), 1883);
  String will = "{\"online\":false,\"id\":\"" + hwid + "\",\"kind\":\"esp32\"}";
  if (mqtt.connect(("haunt-" + hwid).c_str(), nullptr, nullptr, tStatus().c_str(), 1, true, will.c_str())) {
    mqttWasUp = true;
    mqtt.subscribe(tAssign().c_str(), 1);
    mqtt.subscribe(tOut().c_str());
    publishStatus();
    lastStatus = millis();
    Serial.printf("mqtt up: %s as %s\n", brokerResolved.c_str(), name.c_str());
  } else {
    Serial.printf("mqtt connect to %s failed (%d)\n", brokerResolved.c_str(), mqtt.state());
  }
}

// ---------------------------------------------------------------- serial
void serialCommand(String line) {
  line.trim();
  if (line == "?") return whoami();
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp), arg = sp < 0 ? "" : line.substring(sp + 1);
  arg.trim();
  if (cmd == "wifi") {
    int s2 = arg.indexOf(' ');
    prefs.putString("ssid", s2 < 0 ? arg : arg.substring(0, s2));
    prefs.putString("pass", s2 < 0 ? "" : arg.substring(s2 + 1));
    Serial.println("saved; rebooting");
    delay(100);
    ESP.restart();
  } else if (cmd == "broker") {
    prefs.putString("broker", arg);
    brokerSetting = arg;
    mqtt.disconnect();
    Serial.printf("broker = %s\n", arg.isEmpty() ? "(auto: haunt.local, else gateway)" : arg.c_str());
  } else if (cmd == "forget") {
    prefs.clear();
    Serial.println("settings cleared; rebooting");
    delay(100);
    ESP.restart();
  } else if (cmd == "reboot") {
    ESP.restart();
  } else if (cmd.length()) {
    Serial.println("commands: ?  |  wifi <ssid> <pass>  |  broker <host or empty>  |  forget  |  reboot");
  }
}

void tickSerial() {
  static String buf;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length()) serialCommand(buf);
      buf = "";
    } else if (buf.length() < 200) {
      buf += c;
    }
  }
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
  bootMs = millis();
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char id[16];
  snprintf(id, sizeof id, "esp-%02x%02x%02x", mac[3], mac[4], mac[5]);
  hwid = id;

  prefs.begin("haunt", false);
  name = prefs.getString("name", hwid);
  ssid = prefs.getString("ssid", HAUNT_WIFI_SSID);
  pass = prefs.getString("pass", HAUNT_WIFI_PASS);
  brokerSetting = prefs.getString("broker", "");
  loadSavedConfig();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(hwid.c_str());
  WiFi.setSleep(false);  // power save adds 100 ms+ of latency to every message
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid.c_str(), pass.c_str());
  MDNS.begin(hwid.c_str());

  mqtt.setBufferSize(4096);
  mqtt.setKeepAlive(10);
  mqtt.setSocketTimeout(3);
  mqtt.setCallback(onMessage);
  delay(300);
  whoami();
}

void loop() {
  tickSerial();
  tickMqtt();
  tickInputs();
  tickLeds();
  delay(1);
}
