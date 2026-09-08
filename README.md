# dash-extras

Vehicle dashboard project: a PyQt5 GUI running on a Rock5B ("dash"), talking
Modbus RTU over RS485 to an ESP32-S3 (ESPHome) that masters the vehicle's
relays, sensors, and dash itself as slave devices.

## Layout

- `dash-gui/` - the PyQt5 application that runs on dash.
- `esp-config/` - the ESPHome YAML for the vehicle's ESP32-S3 controller.
- `volume-control/` - rotary encoder volume control, autostarts with the
  desktop session.
- `claude-remote/` - Claude Code Remote Control session in a visible
  Konsole window, autostarts with the desktop session and auto-resumes
  across restarts.
- `dashcam/` - continuous loop-recording dashcam using the Anker webcam,
  runs as a systemd service from boot.
- `gpio-buttons/` - reads the physical GPIO push buttons and emulates the
  corresponding keyboard keypresses that the dash app listens for. Not
  currently autostarted - was archived in favour of a device-tree
  `gpio-keys` overlay (`rock5bextras/gpiokeys.dts`) doing the same job at
  the kernel level, kept here for reference/fallback.
- `scripts/` - one-off setup scripts for dash itself.

## Setup

Both sides need a local secrets file copied from its `.example`, filled in,
and left untracked (see `.gitignore`):

- `dash-gui/victron_secrets.py` from `victron_secrets.example.py` - Victron
  BLE device MAC addresses and bindkeys (VictronConnect app -> device ->
  Instant Readout -> Share this data).
- `esp-config/secrets.yaml` from `secrets.yaml.example` - WiFi and OTA
  passwords.

Run the GUI with `./run.sh` (regenerates `gui.py` from `gui.ui` via
`convert_gui.sh`, then launches `dash_app.py`).

On dash itself, also run these once (each is idempotent, safe to re-run):

- `sudo scripts/install-ftdi-latency-rule.sh` - sets the FTDI USB-serial
  adapter's `latency_timer` to 1ms instead of the 16ms Linux default.
  Modbus RTU needs a few milliseconds of bus silence to detect frame
  boundaries, so the default latency is enough to corrupt framing on a
  busy RS485 bus. Without this you'd need to re-apply it by hand after
  every reboot (`echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer`).
- `sudo scripts/install-wifi-regdomain-rule.sh` - sets the wifi
  regulatory domain to GB via udev whenever the wifi phy appears.
  Without a country code, the kernel's default world regdomain marks the
  whole 5GHz band passive-only (no beaconing), which silently breaks the
  `Dash` AP hotspot (5GHz channel 48). Without this you'd need to
  re-apply it by hand after every reboot (`sudo iw reg set GB`).
- `sudo volume-control/install.sh` - autostarts the rotary encoder volume
  control with the desktop session.
- `sudo dashcam/install.sh` - installs and starts the dashcam systemd
  service so it records from boot.
- `claude-remote/install.sh` - autostarts a visible Konsole running
  `claude --continue --remote-control dash`, so this session comes back
  automatically after a reboot or logout. No sudo needed (only writes to
  `~/.config/autostart`).

### Dashcam

Loop-records from the Anker webcam (`dashcam/dashcam.py`) into
`~/dashcam-footage/`, deleting the oldest segment once total footage
exceeds 20GB (`MAX_TOTAL_BYTES` in the script) - a fixed disk budget
rather than a fixed segment count, so it adapts to whatever's actually in
the files. Records at 720p/15fps rather than the camera's native
1080p/30fps: this system's ffmpeg has no hardware encoder available (the
Rockchip MPP library is installed, but the stock Debian ffmpeg build
isn't compiled with `--enable-rkmpp`, and getting that needs a custom
ffmpeg build like the community `ffmpeg-rockchip` fork), and 1080p30
software libx264 encoding costs ~275% CPU running forever in the
background - too much for an always-on service. 720p15 costs ~100% (one
core), which a dashcam doesn't need to beat.

The camera is referenced by its stable `/dev/v4l/by-id/...` udev path
rather than `/dev/videoN`, since device numbers reshuffle across reboots
depending on USB enumeration order. If the camera is ever replaced, find
the new path with `ls /dev/v4l/by-id/` and update `DEVICE` in
`dashcam.py`.

Note: the Anker webcam can only be used by one consumer at a time. Don't
point the dash app's own Camera page at it while the dashcam service is
running (or vice versa) - they'll fight over the device.

### AUX battery BMS

The AUX/leisure battery has its own built-in JBD BMS, read over BLE
directly (not via the Victron shunt) - see `AUX_BATTERY_BMS_MAC` in
`dash-gui/victron_secrets.py` and `JbdBmsPoller` in `dash_app.py`. This
protocol isn't encrypted, so no bindkey is needed, just the MAC.

`esp-config/common/defender/battery-bms.yaml` is a **not-yet-deployed**
alternative: reading the same BMS from the ESP's own BLE radio instead,
and pushing it to dash over the already-reliable Modbus link - would
sidestep the Rock5B-side issue of its one Bluetooth adapter having to
share time between Android Auto's phone connection and BLE scanning,
which has proven unreliable running both at once. Don't enable it without
reading the warning at the top of that file first - it's the same class
of BLE workload as the Victron decryption that previously corrupted the
Modbus bus on this ESP.
