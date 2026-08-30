# dash-extras

Vehicle dashboard project: a PyQt5 GUI running on a Rock5B ("dash"), talking
Modbus RTU over RS485 to an ESP32-S3 (ESPHome) that masters the vehicle's
relays, sensors, and dash itself as slave devices.

## Layout

- `dash-gui/` - the PyQt5 application that runs on dash.
- `esp-config/` - the ESPHome YAML for the vehicle's ESP32-S3 controller.
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

On dash itself, also run `sudo scripts/install-ftdi-latency-rule.sh` once.
It installs a udev rule setting the FTDI USB-serial adapter's
`latency_timer` to 1ms instead of the 16ms Linux default - Modbus RTU
needs a few milliseconds of bus silence to detect frame boundaries, so the
default latency is enough to corrupt framing on a busy RS485 bus. Without
this rule you'd need to re-apply it by hand after every reboot.
