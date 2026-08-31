#!/bin/bash
# Installs and enables the dashcam systemd service so recording starts on
# every boot, without needing to redo this by hand after every
# clone/reinstall.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/etc/systemd/system/dashcam.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo." >&2
  exit 1
fi

cp "$SCRIPT_DIR/dashcam.service" "$DEST"
sed -i "s#^ExecStart=.*#ExecStart=/usr/bin/python3 $SCRIPT_DIR/dashcam.py#" "$DEST"

systemctl daemon-reload
systemctl enable --now dashcam.service

echo "Installed and started $DEST"
systemctl status dashcam.service --no-pager | head -5
