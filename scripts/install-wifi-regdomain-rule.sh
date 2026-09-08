#!/bin/bash
# Installs a udev rule that sets the wifi regulatory domain to GB whenever
# the wifi phy appears. Without a country code, the kernel's default world
# regulatory domain (00) marks the entire 5GHz band PASSIVE-SCAN (no
# transmit/beacon allowed), which silently breaks the "Dash" AP hotspot
# (5GHz channel 48, in dash-gui's NetworkManager profile) - AP mode needs
# to beacon, so it fails every time with wpa_supplicant logging just
# "Failed to start AP functionality" and no further detail. Without this
# rule you'd need to re-apply it by hand after every reboot
# (sudo iw reg set GB).
#
# Triggers on the `ieee80211` subsystem (the wifi phy, e.g. /sys/class/
# ieee80211/phy0) rather than at a fixed boot stage, so it also re-applies
# if the wifi driver is ever unloaded/reloaded live (e.g. after a firmware
# reset), not just on a fresh boot.
set -euo pipefail

RULE_FILE="/etc/udev/rules.d/53-wifi-regdomain.rules"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo." >&2
  exit 1
fi

cat > "$RULE_FILE" <<'EOF'
ACTION=="add", SUBSYSTEM=="ieee80211", RUN+="/usr/sbin/iw reg set GB"
EOF

udevadm control --reload-rules
iw reg set GB

echo "Installed $RULE_FILE and reloaded udev."
echo "Current regulatory domain:"
iw reg get | head -2
