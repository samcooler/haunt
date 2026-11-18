#!/usr/bin/env bash
# Set up a Raspberry Pi (Raspberry Pi OS Bookworm, NetworkManager) as a haunt hub or node.
#
#   sudo deploy/install.sh hub [piece]   the hub: broker, engine, web UI, Wi-Fi AP "haunt"
#   sudo deploy/install.sh node          a satellite: plays audio for the show
#
# Run it from the haunt checkout on the Pi. Safe to rerun.
#
# The hub's AP uses NetworkManager's "shared" mode: it hands out addresses, and if
# the Pi has another way out (Ethernet, a phone on USB tethering, a 4G dongle) it
# shares that internet with everything on the AP, laptop included.

set -euo pipefail

ROLE="${1:-}"
PIECE="${2:-hello}"
SSID="${HAUNT_SSID:-haunt}"
PSK="${HAUNT_PSK:-hauntnet}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"

if [[ "$ROLE" != hub && "$ROLE" != node ]]; then
  echo "usage: sudo $0 hub [piece] | node" >&2
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

  echo "== Wi-Fi access point '$SSID'"
  if ! nmcli -t -f NAME connection show | grep -qx haunt-ap; then
    nmcli connection add type wifi ifname wlan0 con-name haunt-ap autoconnect yes ssid "$SSID" \
      802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
      ipv4.method shared ipv4.addresses 10.77.0.1/24 \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" >/dev/null
  fi
  nmcli connection modify haunt-ap connection.autoconnect-priority 100 802-11-wireless.powersave 2
  nmcli connection up haunt-ap >/dev/null || echo "   (AP did not come up now; it will at boot)"

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
  echo "done: join Wi-Fi '$SSID' and open http://haunt.local:8080/ (or http://10.77.0.1:8080/)"
else
  echo "== join the haunt AP"
  if ! nmcli -t -f NAME connection show | grep -qx haunt; then
    nmcli connection add type wifi ifname wlan0 con-name haunt autoconnect yes ssid "$SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" >/dev/null
  fi
  nmcli connection modify haunt connection.autoconnect-priority 100 802-11-wireless.powersave 2

  echo "== service haunt-node"
  cat > /etc/systemd/system/haunt-node.service <<EOF
[Unit]
Description=haunt node
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
ExecStart=$REPO/.venv/bin/haunt node --broker haunt.local:1883
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now haunt-node
  systemctl restart haunt-node
  echo "done: this node is '$(hostname)'; add it to devices.yaml under that name"
fi
