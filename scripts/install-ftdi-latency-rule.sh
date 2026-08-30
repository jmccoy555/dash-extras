#!/bin/bash
# Installs a udev rule that sets the FTDI USB-serial adapter's
# latency_timer to 1ms (the Linux default is 16ms). Modbus RTU relies on
# a few milliseconds of bus silence to detect frame boundaries, so the
# default latency is enough to corrupt framing on a busy RS485 bus -
# without this rule you'd need to re-apply it by hand after every reboot
# (echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer).
#
# Matches vendor 0403 / product 6001 (FTDI FT232). If your adapter uses a
# different chip, find its IDs with `lsusb` and edit the rule below.
set -euo pipefail

RULE_FILE="/etc/udev/rules.d/52-ftdi-latency.rules"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo." >&2
  exit 1
fi

cat > "$RULE_FILE" <<'EOF'
SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTR{latency_timer}="1"
EOF

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb-serial

echo "Installed $RULE_FILE and reloaded udev."
echo "Current latency_timer values:"
for dev in /sys/bus/usb-serial/devices/*/latency_timer; do
  echo "  $dev: $(cat "$dev")"
done
