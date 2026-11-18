#!/usr/bin/env bash
# Set up a Raspberry Pi (Raspberry Pi OS Bookworm, NetworkManager) as a haunt hub or node.
#
#   sudo deploy/install.sh hub [piece] [node]   the hub: broker, engine, web UI, Wi-Fi AP "haunt";
#                                        with a node name, this Pi is also that node (video + audio)
#   sudo deploy/install.sh node [name]   a satellite: plays audio for the show (name: default hostname)
#
# Run it from the haunt checkout on the Pi. Safe to rerun, and to rerun in the other mode.
#
# The hub's network, two ways:
#
#   AP mode (the default): the hub's own Wi-Fi is the show's network, "haunt". It uses
#   NetworkManager's "shared" mode: it hands out addresses, and if the Pi has another
#   way out (Ethernet, a phone on USB) it shares that internet with everything on it.
#   Fine for a dozen or so boards; the Pi's radio is not made for more.
#
#   Router mode (--router --uplink SSID --uplink-psk PSK): a Wi-Fi router is the
#   show's network, and the hub is wired to it. The hub's Wi-Fi instead joins your
#   phone's hotspot (or any network with internet), which your laptop joins too:
#
#       phone hotspot ──Wi-Fi── laptop
#            └──Wi-Fi── hub Pi ──Ethernet── router ──2.4 GHz── boards, screens
#
#   Ethernet is never the default route (a router with nothing in its WAN port would
#   swallow internet traffic), so the internet goes via the hotspot. If no router is
#   on the Ethernet 45 s after boot, the hub falls back to being the AP itself.
#   Set the router's 2.4 GHz network to the same name and password as the hub's AP
#   ($HAUNT_SSID / $HAUNT_PSK, default haunt / hauntnet) and the boards join either.

set -euo pipefail

NET=ap
UPLINK_SSID=""
UPLINK_PSK=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --router) NET=router; shift ;;
    --ap) NET=ap; shift ;;
    --uplink) UPLINK_SSID="$2"; shift 2 ;;
    --uplink-psk) UPLINK_PSK="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]}"

ROLE="${1:-}"
PIECE="${2:-hello}"
HUB_NODE="${3:-}"
SSID="${HAUNT_SSID:-haunt}"
PSK="${HAUNT_PSK:-hauntnet}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"

if [[ "$ROLE" != hub && "$ROLE" != node ]]; then
  echo "usage: sudo $0 hub [piece] [node] [--router --uplink SSID --uplink-psk PSK] | node [name]" >&2
  exit 2
fi
if [[ "$ROLE" == hub && "$NET" == router && -z "$UPLINK_SSID" ]]; then
  echo "router mode needs --uplink <your hotspot's name> (and --uplink-psk <its password>)" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

echo "== packages"
apt-get update -qq
apt-get install -y -qq python3-venv git mpv avahi-daemon rsync >/dev/null
if [[ "$ROLE" == hub ]]; then
  apt-get install -y -qq mosquitto >/dev/null
fi

echo "== python env"
sudo -u "$USER_NAME" python3 -m venv "$REPO/.venv"
sudo -u "$USER_NAME" "$REPO/.venv/bin/pip" install -q -e "$REPO"

if [[ "$ROLE" == hub ]]; then
  echo "== hostname haunt (so everything can find haunt.local)"
  hostnamectl set-hostname haunt
  sed -i 's/127.0.1.1.*/127.0.1.1\thaunt/' /etc/hosts
  systemctl restart avahi-daemon

  echo "== mosquitto: listen on the network, not just localhost"
  cat > /etc/mosquitto/conf.d/haunt.conf <<EOF
listener 1883 0.0.0.0
allow_anonymous true
EOF
  systemctl enable --now mosquitto
  systemctl restart mosquitto

  has_con() { nmcli -t -f NAME connection show | grep -qx "$1"; }

  # The AP profile exists in both modes: in router mode it is the fallback.
  if ! has_con haunt-ap; then
    nmcli connection add type wifi ifname wlan0 con-name haunt-ap autoconnect no ssid "$SSID" \
      802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
      ipv4.method shared ipv4.addresses 10.77.0.1/24 \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" >/dev/null
  fi
  nmcli connection modify haunt-ap 802-11-wireless.powersave 2

  if [[ "$NET" == ap ]]; then
    echo "== network: AP mode, Wi-Fi '$SSID' from the hub itself"
    nmcli connection modify haunt-ap connection.autoconnect yes connection.autoconnect-priority 100
    if has_con haunt-uplink; then nmcli connection modify haunt-uplink connection.autoconnect no; fi
    if has_con haunt-lan; then nmcli connection delete haunt-lan >/dev/null; fi
    systemctl disable --now haunt-netcheck.service 2>/dev/null || true
    nmcli connection up haunt-ap >/dev/null || echo "   (AP did not come up now; it will at boot)"
  else
    echo "== network: router mode (show on Ethernet, internet from '$UPLINK_SSID')"
    # Ethernet: the show's router. Its DHCP gives the hub an address (reserve one for the
    # hub on the router), but never the default route or DNS: those come from the uplink.
    for con in $(nmcli -t -f NAME,TYPE connection show | awk -F: '$2 == "802-3-ethernet" {print $1}'); do
      [[ "$con" == haunt-lan ]] || nmcli connection modify "$con" connection.autoconnect no
    done
    if ! has_con haunt-lan; then
      nmcli connection add type ethernet ifname eth0 con-name haunt-lan >/dev/null
    fi
    nmcli connection modify haunt-lan connection.autoconnect yes connection.autoconnect-priority 100 \
      ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method ignore
    # Wi-Fi: the phone's hotspot, for the internet.
    if has_con haunt-uplink; then nmcli connection delete haunt-uplink >/dev/null; fi
    if [[ -n "$UPLINK_PSK" ]]; then
      nmcli connection add type wifi ifname wlan0 con-name haunt-uplink ssid "$UPLINK_SSID" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$UPLINK_PSK" >/dev/null
    else
      nmcli connection add type wifi ifname wlan0 con-name haunt-uplink ssid "$UPLINK_SSID" >/dev/null
    fi
    nmcli connection modify haunt-uplink connection.autoconnect yes connection.autoconnect-priority 100 \
      ipv4.route-metric 50 802-11-wireless.powersave 2
    nmcli connection modify haunt-ap connection.autoconnect no
    # No router on the Ethernet after boot: the hub becomes the show's AP itself.
    install -m 755 "$REPO/deploy/haunt-netcheck.sh" /usr/local/bin/haunt-netcheck
    cat > /etc/systemd/system/haunt-netcheck.service <<EOF
[Unit]
Description=haunt: be the AP if the show's router is not there
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/haunt-netcheck 45

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable haunt-netcheck.service
    nmcli connection up haunt-lan >/dev/null || echo "   (no router on the Ethernet now)"
    nmcli connection up haunt-uplink >/dev/null || echo "   (hotspot '$UPLINK_SSID' not in range now; it joins when it is)"
    LAN_IP="$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)"
    LAN_MAC="$(cat /sys/class/net/eth0/address)"
    echo "   On the router: reserve ${LAN_IP:-an address} for the hub (MAC $LAN_MAC), give its"
    echo "   2.4 GHz network the name '$SSID' and password '$PSK', turn off client/AP isolation."
    echo "   In pieces/$PIECE/piece.yaml:  hub: ${LAN_IP:-<that address>}"
  fi

  echo "== service haunt-hub (piece: $PIECE)"
  cat > /etc/default/haunt <<EOF
HAUNT_PIECE=$REPO/pieces/$PIECE
EOF
  cat > /etc/systemd/system/haunt-hub.service <<EOF
[Unit]
Description=haunt hub
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
User=$USER_NAME
EnvironmentFile=/etc/default/haunt
ExecStart=$REPO/.venv/bin/haunt run \${HAUNT_PIECE} --broker localhost:1883 --port 8080
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now haunt-hub
  systemctl restart haunt-hub

  if [[ -n "$HUB_NODE" ]]; then
    echo "== service haunt-node: this Pi is also '$HUB_NODE' (fullscreen video, audio)"
    MEDIA_DIR="$(sudo -u "$USER_NAME" "$REPO/.venv/bin/python" -c \
      "from haunt.piece import Piece; print(Piece('$REPO/pieces/$PIECE').media)")"
    usermod -aG video,render,audio "$USER_NAME"
    mkdir -p /etc/haunt-mpv
    printf 'vo=gpu\ngpu-context=drm\n' > /etc/haunt-mpv/mpv.conf   # straight to the screen, no desktop
    cat > /etc/systemd/system/haunt-node.service <<EOF
[Unit]
Description=haunt node $HUB_NODE (on the hub)
After=haunt-hub.service sound.target

[Service]
User=$USER_NAME
Environment=MPV_HOME=/etc/haunt-mpv
ExecStart=$REPO/.venv/bin/haunt node --name $HUB_NODE --broker localhost:1883 --video --media-dir $MEDIA_DIR
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now haunt-node
    systemctl restart haunt-node
  fi
  if [[ "$NET" == ap ]]; then
    echo "done: join Wi-Fi '$SSID' and open http://haunt.local:8080/ (or http://10.77.0.1:8080/)"
  else
    echo "done: join '$UPLINK_SSID' (or the router's Wi-Fi) and open http://haunt.local:8080/"
    echo "      (if haunt.local does not resolve on the hotspot: http://<the hub's hotspot IP>:8080/)"
  fi
else
  echo "== join the haunt AP"
  if ! nmcli -t -f NAME connection show | grep -qx haunt; then
    nmcli connection add type wifi ifname wlan0 con-name haunt autoconnect yes ssid "$SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" >/dev/null
  fi
  nmcli connection modify haunt connection.autoconnect-priority 100 802-11-wireless.powersave 2

  NODE_NAME="${2:-$(hostname)}"
  echo "== service haunt-node ($NODE_NAME)"
  cat > /etc/systemd/system/haunt-node.service <<EOF
[Unit]
Description=haunt node
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
ExecStart=$REPO/.venv/bin/haunt node --name $NODE_NAME --broker haunt.local:1883
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now haunt-node
  systemctl restart haunt-node
  echo "done: this node is '$NODE_NAME'; it must be a node of that name in devices.yaml"
fi
