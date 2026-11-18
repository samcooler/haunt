#!/usr/bin/env bash
# Router mode's fallback: run at boot. If the show's router is not on the Ethernet
# after a grace period, the hub becomes the show's Wi-Fi itself (giving up the hotspot
# uplink: the radio does one or the other). The boards join it by the same name.
#
#   haunt-netcheck [seconds]     wait, then check (default 45)
#   haunt-netcheck uplink        back from the AP to the hotspot, once the router is back
set -u

if [[ "${1:-}" == uplink ]]; then
  nmcli connection down haunt-ap 2>/dev/null
  exec nmcli connection up haunt-uplink
fi

sleep "${1:-45}"
if ip -4 -o addr show eth0 2>/dev/null | grep -q " inet "; then
  echo "haunt-netcheck: router present on eth0; staying on the uplink"
  exit 0
fi
echo "haunt-netcheck: no router on eth0; starting the haunt AP"
exec nmcli connection up haunt-ap
