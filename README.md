# dash-extras

Vehicle dashboard project: a PyQt5 GUI running on a Rock5B ("dash"), talking
Modbus RTU over RS485 to an ESP32-S3 (ESPHome) that masters the vehicle's
relays, sensors, and dash itself as slave devices.

## Layout

- `dash-gui/` - the PyQt5 application that runs on dash.
- `esp-config/` - the ESPHome YAML for the vehicle's ESP32-S3 controller.

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
