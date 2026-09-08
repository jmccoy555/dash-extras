#!/bin/bash
# Installs the Claude Code remote-control terminal autostart entry, without
# needing to redo this by hand after every clone/reinstall.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/.config/autostart"
DEST="$DEST_DIR/claude-remote.desktop"

mkdir -p "$DEST_DIR"
cp "$SCRIPT_DIR/claude-remote.desktop" "$DEST"
sed -i "s#^Exec=.*#Exec=/usr/bin/konsole --hide-menubar --nofork -e $SCRIPT_DIR/claude-remote-terminal.sh#" "$DEST"
chmod +x "$SCRIPT_DIR/claude-remote-terminal.sh"

echo "Installed $DEST"
grep '^Exec=' "$DEST"
echo "Takes effect on the next desktop session login (or run $SCRIPT_DIR/claude-remote-terminal.sh directly to test now)."
