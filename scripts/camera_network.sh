#!/usr/bin/env bash
# Dedicated camera Ethernet profile; never replaces the Internet default route.
set -euo pipefail
iface="${1:-eno1}"
[[ "$iface" =~ ^[a-zA-Z0-9_.:-]+$ ]] || { echo 'Invalid network interface'; exit 1; }
[[ -d "/sys/class/net/$iface" ]] || { echo "No interface: $iface"; exit 1; }
if nmcli -g connection.id connection show uav-g3p >/dev/null 2>&1; then
  echo 'uav-g3p already exists; inspect with: nmcli connection show uav-g3p'
  exit 0
fi
nmcli connection add type ethernet ifname "$iface" con-name uav-g3p \
  ipv4.method manual ipv4.addresses 192.168.144.10/24 ipv4.never-default yes \
  ipv6.method disabled connection.autoconnect yes connection.autoconnect-priority 50
echo 'Camera Ethernet profile ready; it will activate when the cable is connected.'
echo 'Camera: 192.168.144.135 · host: 192.168.144.10 · Internet routing remains on the Internet interface.'
