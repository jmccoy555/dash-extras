# Template for victron_secrets.py - copy this file to victron_secrets.py
# and fill in your own Victron devices' MAC address and encryption bindkey
# (VictronConnect app -> device -> Instant Readout -> Share this data).
# victron_secrets.py itself is gitignored and never committed.

BATTERY_MAC = "XX:XX:XX:XX:XX:XX"
BATTERY_KEY = "REPLACE_WITH_YOUR_BINDKEY"

SOLAR_MAC = "XX:XX:XX:XX:XX:XX"
SOLAR_KEY = "REPLACE_WITH_YOUR_BINDKEY"

SOLAR2_MAC = "XX:XX:XX:XX:XX:XX"
SOLAR2_KEY = "REPLACE_WITH_YOUR_BINDKEY"

INVERTER_MAC = "XX:XX:XX:XX:XX:XX"
INVERTER_KEY = "REPLACE_WITH_YOUR_BINDKEY"

CHARGER_AUX_MAC = "XX:XX:XX:XX:XX:XX"
CHARGER_AUX_KEY = "REPLACE_WITH_YOUR_BINDKEY"

# Not a Victron device - the AUX/leisure battery's own built-in JBD BMS,
# read directly instead of via the Victron shunt. No bindkey needed, this
# protocol isn't encrypted.
AUX_BATTERY_BMS_MAC = "XX:XX:XX:XX:XX:XX"
