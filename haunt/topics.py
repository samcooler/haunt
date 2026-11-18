"""Every MQTT topic haunt uses. The wire protocol in one place.

Payloads are JSON objects. An empty payload clears a retained topic.

Nodes (ESP32, Pi agent, simulated) are addressed by *name* once assigned:

  haunt/assign/<hwid>          hub -> node, retained: {"name", "config"}
  haunt/n/<name>/status        node -> all, retained, also the MQTT will:
                               {"online", "id", "kind", "fw", "ip", "rssi", "uptime", "caps"}
  haunt/n/<name>/in/<input>    node -> hub: {"event": "press"|"release"|"on"|"off"|"value", "value"}
  haunt/n/<name>/out/<output>  hub -> node: e.g. out/leds {"effect", "color", ...}

The show (the hub's engine):

  haunt/show/state             retained: {"piece", "state", "since", "states", "actions", "error"}
  haunt/show/params            retained: {name: {"value", "min", "max", ...}}
  haunt/show/vars              retained: the show's variables, for the console
  haunt/show/log               {"t", "level", "msg"}

Commands, from the web UI, the CLI, or Claude (anyone on the bus):

  haunt/cmd/goto      {"state"}
  haunt/cmd/reset     {}
  haunt/cmd/param     {"name", "value"}
  haunt/cmd/action    {"name"}
  haunt/cmd/blackout  {"on"}
  haunt/cmd/event     {"name", "value"}   inject an event as if a node sent it
  haunt/cmd/identify  {"node"}            blink a node so you can find it
"""

ROOT = "haunt"


def assign(hwid: str) -> str:
    return f"{ROOT}/assign/{hwid}"


def status(name: str) -> str:
    return f"{ROOT}/n/{name}/status"


def node_in(name: str, input_: str) -> str:
    return f"{ROOT}/n/{name}/in/{input_}"


def node_out(name: str, output: str) -> str:
    return f"{ROOT}/n/{name}/out/{output}"


SHOW_STATE = f"{ROOT}/show/state"
SHOW_PARAMS = f"{ROOT}/show/params"
SHOW_VARS = f"{ROOT}/show/vars"
SHOW_LOG = f"{ROOT}/show/log"


# High-rate outputs (overlay frames): not recorded, not sent to browsers.
NOISY_OUTPUTS = ("overlay",)


def noisy(topic: str) -> bool:
    return topic.startswith(f"{ROOT}/n/") and topic.rsplit("/", 1)[-1] in NOISY_OUTPUTS and "/out/" in topic


def cmd(verb: str) -> str:
    return f"{ROOT}/cmd/{verb}"


def matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard match: + is one level, # is the rest."""
    p = pattern.split("/")
    t = topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(p) == len(t)


def split_node_topic(topic: str) -> tuple[str, str, str] | None:
    """haunt/n/<name>/<dir>/<thing> -> (name, dir, thing); status -> (name, "status", "")."""
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != ROOT or parts[1] != "n":
        return None
    if parts[3] == "status" and len(parts) == 4:
        return parts[2], "status", ""
    if len(parts) == 5 and parts[3] in ("in", "out"):
        return parts[2], parts[3], parts[4]
    return None
