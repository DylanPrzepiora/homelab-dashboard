#!/bin/bash
# Installs the host-side SMART collector. Run once as root from the repo, and again after you edit
# the smart.disks list in config.yaml:
#     sudo bash host/install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
d="$(dirname "$(readlink -f "$0")")"
cfg="$d/../config.yaml"

command -v smartctl >/dev/null || { echo "smartmontools is not installed: sudo apt install smartmontools"; exit 1; }
python3 -c 'import yaml' 2>/dev/null || { echo "python3-yaml is not installed: sudo apt install python3-yaml"; exit 1; }
[ -f "$cfg" ] || { echo "$cfg not found: copy config.example.yaml to config.yaml and list your disks first"; exit 1; }

install -d -m 755 /var/lib/dashboard /etc/dashboard
install -m 644 -o root -g root "$cfg" /etc/dashboard/config.yaml          # root-owned: the collector runs as root
install -m 755 -o root -g root "$d/dashboard-smart-collect" /usr/local/sbin/dashboard-smart-collect
install -m 644 "$d/dashboard-smart.service" "$d/dashboard-smart.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dashboard-smart.timer
systemctl start dashboard-smart.service      # collect once now instead of waiting for the timer
systemctl --no-pager status dashboard-smart.timer | head -8
echo; python3 -m json.tool /var/lib/dashboard/smart.json | head -60
