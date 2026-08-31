#!/bin/bash
# Installs the volume control autostart entry so the rotary encoder script
# launches with the desktop session, without needing to redo this by hand
# after every clone/reinstall.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/etc/xdg/autostart/volume.desktop"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo." >&2
  exit 1
fi

cp "$SCRIPT_DIR/volume.desktop" "$DEST"
sed -i "s#^Exec=.*#Exec=$SCRIPT_DIR/volume.py#" "$DEST"

echo "Installed $DEST"
echo "Exec=$SCRIPT_DIR/volume.py"
echo "Takes effect on the next desktop session login (or run volume.py directly to test now)."
