#!/usr/bin/env python3
# The dash GUI, running as a genuine Modbus RTU slave - the ESP is the
# bus's only master, including of dash. Pairs with the ESP-side
# modbus-dash.yaml (in esp-config/) which masters this as a slave device.
#
# Architecture: dash no longer initiates any Modbus transaction at all. It
# just holds a local register/coil table (the "slave context" below) that
# the ESP - now the bus's only master, including of dash - reads from and
# writes to whenever it wants. Button clicks and sensor display are just
# local memory reads/writes, no bus I/O, so there's no polling, no retries,
# no backoff logic needed on this side any more. The tradeoff: dash can
# only ever show what the ESP last wrote, and a click only take effect once
# the ESP notices the coil changed on its next read of dash - so
# responsiveness is bounded by the ESP's own poll interval of dash, not
# anything dash controls (see conversation).
#
# Address map mirrors the current address-2 layout on the ESP's own server,
# address-for-address, so the two versions are easy to reason about
# side-by-side. SLAVE_ADDRESS itself (5) is provisional - whatever the ESP
# side ends up using needs to match.

import sys
import time
import datetime
import asyncio
import threading
import logging

# When launched normally (via dash's own Launcher, not a terminal), the
# parent redirects our stdout/stderr fds to /dev/null before exec - every
# print()/logging call below was silently going nowhere, with no way to
# diagnose a live failure (e.g. the BLE pollers) short of manually running
# this script from a shell instead. Reassigning sys.stdout/stderr here binds
# fresh fds to a real file regardless of what the parent did to fd 1/2.
try:
    _log_file = open("/tmp/dash_app.log", "a", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file
except OSError:
    pass

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel
from PyQt5.QtCore import Qt, QTimer, QThread
from gui import Ui_MainWindow
from victron_ble.scanner import Scanner
from victron_ble.exceptions import AdvertisementKeyMissingError, UnknownDeviceError
from bleak import BleakScanner
from bleak.args.bluez import BlueZScannerArgs
import victron_secrets
import subprocess

# Second USB BT dongle (Cambridge Silicon Radio) dedicated to BLE scanning
# (Victron + JBD BMS) - hci0 (the original Realtek dongle, already paired
# with the phone) is left alone for Android Auto. Sharing one adapter
# between AA's phone connection and continuous BLE scanning proved
# unreliable (see conversation).
#
# hciN numbering is NOT stable across reboots - confirmed live: after one
# reboot the onboard (non-functional) UART Bluetooth chip enumerated as
# hci2, taking the slot this dongle previously had, which silently broke
# all BLE scanning (adapter existed but was DOWN). Resolve by this dongle's
# own fixed MAC instead of hardcoding a name, so it survives re-enumeration.
BLE_ADAPTER_MAC = "00:1a:7d:da:71:13"


def resolve_ble_adapter(mac, fallback="hci0"):
    try:
        output = subprocess.run(
            ["hciconfig", "-a"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception as e:
        print(f"[ble] hciconfig failed ({e}), falling back to {fallback}", flush=True)
        return fallback
    current = None
    for line in output.splitlines():
        if line and not line[0].isspace():
            current = line.split(":", 1)[0]
        elif "BD Address:" in line and current:
            found_mac = line.split("BD Address:")[1].split()[0]
            if found_mac.lower() == mac.lower():
                print(f"[ble] resolved {mac} -> {current}", flush=True)
                return current
    print(f"[ble] {mac} not found in hciconfig -a, falling back to {fallback}", flush=True)
    return fallback


BLE_ADAPTER = resolve_ble_adapter(BLE_ADAPTER_MAC)

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import ModbusSerialServer
from pymodbus import FramerType

SLAVE_ADDRESS = 5  # provisional - must match the ESP's client entry for dash
SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600

# Holding registers (function code 3) - the ESP writes these, dash only reads.
REG_OUTSIDE_TEMP = 1   # plain int, no scaling
REG_INSIDE_TEMP = 2    # *10 scaled, same convention as the master version
REG_INSIDE_HUMID = 3   # *10 scaled
# AUX/leisure battery's JBD BMS, read by the ESP's own BLE radio and pushed
# here instead of dash polling it directly - see battery-bms.yaml. All *100
# scaled; current is signed (negative = discharging), so must be decoded as
# int16, not the plain uint16 the other registers use.
REG_BMS_VOLTAGE = 4
REG_BMS_CURRENT = 5
REG_BMS_SOC = 6
REG_BMS_CAPACITY_REMAINING = 7

# Coils (function code 1) - dash writes on click (a request), the ESP reads
# it, actions the real device, and writes back the confirmed state (which
# might not be what dash asked for, if the real write failed - dash's next
# refresh just shows whatever's actually there). 46-49 and 45 are
# ESP-write-only (presence, shutdown request).
COIL_MAP = {
    # ui attribute name: coil address
    "worklightsPassangerSide": 10,
    "worklightsDriverSide": 11,
    "worklightsRear": 12,
    "worklightsUnderBonnet": 13,
    "headlightsAuto": 14,
    "sidelightsAuto": 15,
    "service": 16,
    "lightbarMain": 20,
    "lightbarInner": 21,
    "lightbarOuter": 22,
    "lightbarWhite": 23,
    "lightbarAmber": 24,
    "lightbarAutoMain": 25,
    "lightbarAutoAmber": 26,
    "lightbarAutoWhite": 27,
    "windowFrontPower": 30,
    "windowDriverUp": 31,
    "windowDriverDown": 32,
    "windowPassangerUp": 33,
    "windowPassangerDown": 34,
    "windowFrontUp": 35,
    "windowFrontDown": 36,
    # (windowFrontPower is a real on/off switch, not momentary)
    "inverterPower": 40,
    "charger1Power": 41,
    "charger2Power": 42,
    "suspensionPower": 44,
    "suspensionService": 50,
    "lowBox": 51,
    "diffLock": 52,
    "rearLeftUp": 53,
    "rearLeftDown": 54,
    "rearRightUp": 55,
    "rearRightDown": 56,
    "rearPower": 57,
    "rearUp": 58,
    "rearDown": 59,
    "sideLights": 60,
    "headlights": 61,
}
# These six are momentary presses on the ESP side (on_press fires
# window_*.press(), there's no persistent on/off state) - see
# modbus-dash.yaml. The ESP also never pushes a status readback for them
# (they're absent from the status-coil push entirely), so toggle_button's
# normal persistent on/off handling left them stuck green until a second
# click, and even then refresh_from_datastore had nothing real to correct
# against (see conversation).
MOMENTARY_COILS = {
    COIL_MAP["windowDriverUp"], COIL_MAP["windowDriverDown"],
    COIL_MAP["windowPassangerUp"], COIL_MAP["windowPassangerDown"],
    COIL_MAP["windowFrontUp"], COIL_MAP["windowFrontDown"],
    COIL_MAP["rearLeftUp"], COIL_MAP["rearLeftDown"],
    COIL_MAP["rearRightUp"], COIL_MAP["rearRightDown"],
    COIL_MAP["rearUp"], COIL_MAP["rearDown"],
    # (rearPower is a real on/off switch, not momentary)
}
MOMENTARY_PRESS_MS = 250
COIL_SHUTDOWN_REQUEST = 45  # ESP-write-only
COIL_JAMES_FOB = 46         # ESP-write-only
COIL_JAMES_PHONE = 47       # ESP-write-only
COIL_OLGA_FOB = 48          # ESP-write-only
COIL_OLGA_PHONE = 49        # ESP-write-only

# Request coils are a separate range, offset from the status coils, that
# only dash ever writes and only the ESP ever reads/clears. They CANNOT
# share addresses with the status coils above: the ESP pushes its own
# status into 10-52 every 500ms, which would silently clobber a button
# click before the ESP's own read of that same address ever noticed it
# changed - confirmed live (see conversation, "buttons don't control the
# ESP"). The display in refresh_from_datastore() always reads the status
# range; only toggle_button() touches the request range.
REQUEST_COIL_OFFSET = 100

COIL_NAMES = {addr: name for name, addr in COIL_MAP.items()}
AUTOMODE_COILS = {
    COIL_MAP["sidelightsAuto"], COIL_MAP["lightbarAutoMain"],
    COIL_MAP["lightbarAutoAmber"], COIL_MAP["lightbarAutoWhite"],
}
COIL_TABLE_SIZE = 164
REGISTER_TABLE_SIZE = 16
UI_REFRESH_MS = 200  # cheap local memory reads - no bus cost to going fast


class TrackedDataBlock(ModbusSequentialDataBlock):
    """A data block that remembers when it was last written, so the GUI can
    tell "the ESP hasn't touched this in a while" apart from "always been
    zero" - a rough equivalent of the master version's has_data/error
    debounce tracking, without needing to poll for it.

    status_cutoff splits writes into two separate timestamps when the coil
    table holds both ESP-written status coils and dash-written request
    coils sharing one block: a button click (a request-range write) must
    not look like fresh evidence that the ESP is alive and well, or a
    genuinely stale/disconnected ESP would never show as stale as long as
    someone kept clicking things (see conversation)."""

    def __init__(self, address, values, status_cutoff=None):
        super().__init__(address, values)
        self.last_write_time = 0.0
        self.status_cutoff = status_cutoff

    def setValues(self, address, values):
        super().setValues(address, values)
        now = time.monotonic()
        if self.status_cutoff is None or address < self.status_cutoff:
            self.last_write_time = now


class ModbusFloodWatchdog(logging.Handler):
    # pymodbus has a long-standing class of bug (confirmed live tonight on
    # both 3.7.4 and 3.9.2, different failure shapes each time - a stuck
    # framer buffer reprocessing/re-responding to the same data over and
    # over, or an outright crash on a malformed frame that leaves the
    # transport wedged) where it gets stuck logging errors far faster than
    # any real bus transaction could ever happen. At 9600 baud an ~8-byte
    # frame takes ~8ms even back-to-back, so more than a handful of
    # ERROR-level pymodbus log records inside 200ms is physically
    # impossible from real traffic - it can only mean the server is stuck,
    # not that the bus is unusually busy. "Restart dash and it goes away"
    # was confirmed live, twice, as a reliable recovery - this automates
    # that specific recovery without needing a full process restart (which
    # would require manually relaunching via the Launcher every time, see
    # conversation - EmbeddedApp has no way to notice and re-embed a fresh
    # window on its own).
    WINDOW_S = 0.2
    THRESHOLD = 15

    def __init__(self, on_flood):
        super().__init__(level=logging.ERROR)
        self.on_flood = on_flood
        self._timestamps = []

    def emit(self, record):
        now = time.monotonic()
        self._timestamps.append(now)
        cutoff = now - self.WINDOW_S
        self._timestamps = [t for t in self._timestamps if t >= cutoff]
        if len(self._timestamps) >= self.THRESHOLD:
            self._timestamps = []
            self.on_flood()


class ModbusSlaveServerThread(QThread):
    # Runs the RTU slave server for the life of the app, reopening the
    # serial port and building a fresh ModbusSerialServer whenever
    # request_restart() is called (see ModbusFloodWatchdog above) instead
    # of the previous single StartSerialServer() call with no way to
    # recover from a stuck server short of killing the whole process.
    def __init__(self, server_context):
        super().__init__()
        self.server_context = server_context
        self.server = None
        self.running = True
        self._restart_requested = threading.Event()

    def run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            print(f"[modbus-slave] server failed: {e}", flush=True)

    async def _main(self):
        while self.running:
            self._restart_requested.clear()
            self.server = ModbusSerialServer(
                self.server_context,
                FramerType.RTU,
                port=SERIAL_PORT,
                baudrate=BAUD_RATE,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1,
                # RS485 is multi-drop - dash's port sees every frame on the
                # bus, not just ones addressed to it (address 5). Without
                # this, pymodbus's default behaviour is to actively
                # transmit a GatewayNoResponse exception for every request
                # meant for someone else (the relay boards at 10/20) -
                # garbage bytes injected onto the wire on top of their real
                # responses (see conversation - a likely major contributor
                # to the RTU framing corruption diagnosed earlier tonight).
                ignore_missing_slaves=True,
            )
            watcher = asyncio.ensure_future(self._watch_for_restart())
            try:
                await self.server.serve_forever()
            except Exception as e:
                print(f"[modbus-slave] serve_forever raised: {e}", flush=True)
            finally:
                watcher.cancel()
            self.server = None
            if self.running:
                print("[modbus-slave] reopening port fresh", flush=True)
                await asyncio.sleep(0.5)

    async def _watch_for_restart(self):
        # Polls a plain threading.Event rather than needing a cross-thread
        # asyncio call - request_restart() (called from the watchdog's
        # logging callback, on this same thread) just needs to be safe to
        # call from anywhere, and Event.set() already is.
        while True:
            if self._restart_requested.is_set():
                print("[modbus-slave] flood watchdog triggered a restart", flush=True)
                await self.server.shutdown()
                return
            await asyncio.sleep(0.1)

    def request_restart(self):
        if self.server is not None:
            self._restart_requested.set()

    def stop(self):
        self.running = False
        self._restart_requested.set()
        self.wait()


class VictronBlePoller(QThread):
    # Identical to the master version - Victron telemetry comes straight off
    # Bluetooth, nothing to do with Modbus at all either way.
    from PyQt5.QtCore import pyqtSignal
    battery_signal = pyqtSignal(float, float, float, float, object, object)
    solar_signal = pyqtSignal(str, float, float, float, float)
    inverter_signal = pyqtSignal(str, float, float, float)
    charger_aux_signal = pyqtSignal(float, float)  # voltage, current

    def __init__(self, battery_mac, battery_key, solar_mac, solar_key, inverter_mac, inverter_key,
                 solar2_mac=None, solar2_key=None, charger_aux_mac=None, charger_aux_key=None):
        super().__init__()
        self.running = True
        self.battery_mac = battery_mac.lower()
        self.solar_mac = solar_mac.lower()
        self.charger_aux_mac = charger_aux_mac.lower() if charger_aux_mac else None
        self.inverter_mac = inverter_mac.lower()
        self.device_keys = {
            battery_mac: battery_key,
            solar_mac: solar_key,
            inverter_mac: inverter_key,
        }
        # There are two independent solar charge controllers on two separate
        # panels - the GUI shows their combined output as one reading (see
        # conversation). solar_macs/_solar_data key by address so _handle
        # can update whichever one just reported and re-emit the combined
        # total of both every time either changes.
        self.solar_macs = [self.solar_mac]
        self._solar_data = {self.solar_mac: None}
        if solar2_mac and solar2_key:
            solar2_mac_lower = solar2_mac.lower()
            self.solar_macs.append(solar2_mac_lower)
            self._solar_data[solar2_mac_lower] = None
            self.device_keys[solar2_mac] = solar2_key
        if charger_aux_mac and charger_aux_key:
            self.device_keys[charger_aux_mac] = charger_aux_key
        self._warned_addresses = set()

    def run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            print(f"[victron-ble] scanner failed: {e}", flush=True)

    async def _main(self):
        outer = self

        class _InnerScanner(Scanner):
            def callback(self, ble_device, raw_data, advertisement):
                address = ble_device.address.lower()
                try:
                    device = self.get_device(ble_device, raw_data)
                except (AdvertisementKeyMissingError, UnknownDeviceError) as e:
                    if address not in outer._warned_addresses:
                        outer._warned_addresses.add(address)
                        print(f"[victron-ble] {address}: {e}", flush=True)
                    return
                try:
                    parsed = device.parse(raw_data)
                except Exception as e:
                    if address not in outer._warned_addresses:
                        outer._warned_addresses.add(address)
                        print(f"[victron-ble] {address} decode failed: {e}", flush=True)
                    return
                outer._handle(address, parsed)

        scanner = _InnerScanner(self.device_keys)
        # victron_ble's BaseScanner hardcodes its own BleakScanner with no
        # way to pass adapter= through the constructor - swap it out before
        # starting so this pins to the dedicated BLE dongle instead of
        # whichever adapter BlueZ treats as default.
        scanner._scanner = BleakScanner(
            detection_callback=scanner._detection_callback,
            bluez=BlueZScannerArgs(adapter=BLE_ADAPTER),
        )
        await scanner.start()
        try:
            while self.running:
                await asyncio.sleep(0.5)
        finally:
            await scanner.stop()

    def _handle(self, address, parsed):
        if address == self.battery_mac:
            voltage = parsed.get_voltage() or 0.0
            current = parsed.get_current() or 0.0
            soc = parsed.get_soc() or 0.0
            consumed_ah = parsed.get_consumed_ah() or 0.0
            remaining_mins = parsed.get_remaining_mins()
            # The shunt's own voltage/current/soc reading is actually for
            # the AUX/house battery bank (see conversation, hence "Battery
            # AUX" not "Battery") - its aux input terminal is configured
            # to monitor the vehicle's main/starter battery instead, which
            # is what Main Battery shows.
            main_battery_voltage = parsed.get_starter_voltage()
            self.battery_signal.emit(voltage, current, soc, consumed_ah, remaining_mins, main_battery_voltage)
        elif address in self._solar_data:
            state = parsed.get_charge_state()
            state_text = state.name.replace("_", " ").title() if state is not None else "Unknown"
            voltage = parsed.get_battery_voltage() or 0.0
            current = parsed.get_battery_charging_current() or 0.0
            power = parsed.get_solar_power() or 0.0
            yield_today = parsed.get_yield_today() or 0.0
            self._solar_data[address] = {
                "state_text": state_text, "voltage": voltage, "current": current,
                "power": power, "yield_today": yield_today,
            }
            self._emit_combined_solar()
        elif address == self.inverter_mac:
            state = parsed.get_device_state()
            state_text = state.name.replace("_", " ").title() if state is not None else "Unknown"
            voltage = parsed.get_battery_voltage() or 0.0
            current = parsed.get_ac_current() or 0.0
            power = parsed.get_ac_apparent_power() or 0.0
            self.inverter_signal.emit(state_text, voltage, current, power)
        elif self.charger_aux_mac and address == self.charger_aux_mac:
            # This one decodes as a DcEnergyMeterData, not a SmartShunt like
            # the main battery monitor - it only has current/voltage, no
            # consumed_ah tracking at all (confirmed live - calling
            # get_consumed_ah() on it raises AttributeError, see
            # conversation).
            voltage = parsed.get_voltage() or 0.0
            current = parsed.get_current() or 0.0
            self.charger_aux_signal.emit(voltage, current)

    def _emit_combined_solar(self):
        # Voltage is the same battery bank either controller measures, so
        # average rather than sum it; current/power/yield are each
        # controller's own independent contribution, so those sum. A
        # controller that hasn't reported yet just contributes nothing
        # rather than blocking the other's data from showing.
        known = [d for d in self._solar_data.values() if d is not None]
        if not known:
            return
        voltage = sum(d["voltage"] for d in known) / len(known)
        current = sum(d["current"] for d in known)
        power = sum(d["power"] for d in known)
        yield_today = sum(d["yield_today"] for d in known)
        states = sorted(set(d["state_text"] for d in known))
        state_text = states[0] if len(states) == 1 else " / ".join(states)
        self.solar_signal.emit(state_text, voltage, current, power, yield_today)

    def stop(self):
        self.running = False
        self.wait()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        # Added here rather than in gui.ui/gui.py - that generated pair has
        # drifted out of sync with each other (gui.ui is missing several
        # real widgets gui.py has, predating this session - see
        # conversation), so regenerating from gui.ui would delete working
        # UI. Cheapest safe way to add a widget until that's untangled.
        self.dateTimeLabel = QLabel(self.ui.climate)
        self.dateTimeLabel.setFont(self.ui.tempLabel.font())
        self.dateTimeLabel.setAlignment(Qt.AlignCenter)
        # Without matching the temp labels' Expanding/Expanding size policy,
        # this shrinks to fit its text instead of filling its slot in the
        # row - leaving the plain (undyed) parent widget background visible
        # around it.
        self.dateTimeLabel.setSizePolicy(self.ui.tempLabel.sizePolicy())
        # The generic QSS QLabel rule (#1e1e1e) isn't actually what the
        # sibling labels show - refresh_from_datastore calls set_label_ok()/
        # set_label_error() on them every tick, which sets an explicit
        # per-widget stylesheet (#444 grey, or #a00 red while stale) that
        # overrides the generic rule entirely. A clock has no stale/error
        # state, so it never gets that call - apply the same "ok" style
        # once here instead of leaving it on the generic (visibly darker)
        # fallback (see conversation - "needs the right background").
        self.set_label_ok(self.dateTimeLabel)
        self.ui.horizontalLayout.addWidget(self.dateTimeLabel)

        coils = TrackedDataBlock(0, [False] * COIL_TABLE_SIZE, status_cutoff=REQUEST_COIL_OFFSET)
        holding_regs = TrackedDataBlock(0, [0] * REGISTER_TABLE_SIZE)
        self.coils = coils
        self.holding_regs = holding_regs
        slave_ctx = ModbusSlaveContext(
            di=ModbusSequentialDataBlock(0, [False] * 8),
            co=coils,
            hr=holding_regs,
            ir=ModbusSequentialDataBlock(0, [0] * 8),
            # pymodbus defaults to the legacy "zero_mode=False", which shifts
            # every incoming request address by -1 before touching the data
            # block - garbled the temp/humidity registers and broke every
            # coil by one address (see conversation). The ESP sends plain
            # 0-based PDU addresses like everything modern does, so this
            # needs to be True to match.
            #
            # Tried upgrading to 3.9.2 to chase a stuck-retransmission bug
            # (see conversation) - zero_mode was removed there (compensated
            # by constructing blocks at address=1 instead, verified working
            # empirically), but 3.9.2's framer then crashed outright with an
            # unhandled struct.error on a malformed/truncated frame (very
            # plausible on this shared multi-drop bus) and got stuck in a
            # broken state - worse than the original bug, not better.
            # Reverted to 3.7.4 and this zero_mode=True line.
            zero_mode=True,
        )
        self.server_context = ModbusServerContext(slaves={SLAVE_ADDRESS: slave_ctx}, single=False)

        self.server_thread = ModbusSlaveServerThread(self.server_context)
        self.server_thread.start()

        self.modbus_flood_watchdog = ModbusFloodWatchdog(self.server_thread.request_restart)
        logging.getLogger("pymodbus.logging").addHandler(self.modbus_flood_watchdog)

        # Designer's label ("Coffee") for this button was never right.
        self.ui.inverterPower.setText("Inverter")

        # button widget -> coil address, mirroring relay_buttons in the
        # master version but without the (unused now) slave number - there's
        # only ever one slave context here, this one.
        self.relay_buttons = [(getattr(self.ui, name), coil) for name, coil in COIL_MAP.items()]

        for button, coil in self.relay_buttons:
            button.clicked.connect(lambda _, b=button, c=coil: self.toggle_button(b, c))

        # Same not-installed set as the master version.
        self.not_installed_buttons = [
            self.ui.controlComms,
            self.ui.controlRearPower,
            self.ui.controlRearHeater,
            self.ui.controlDieselHeater,
            self.ui.pushButton_9,
            self.ui.pushButton_10,
            self.ui.pushButton_13,
            self.ui.pushButton_15,
            self.ui.pushButton_18,
            self.ui.pushButton_19,
            self.ui.pushButton_20,
            self.ui.suspensionLow,
            self.ui.suspensionNormal,
            self.ui.suspensionHeigh,
            self.ui.suspensionLevel,
        ]
        for button in self.not_installed_buttons:
            button.setEnabled(False)
            button.setStyleSheet("background-color: #222; color: #777;")

        self.ui.bleCountLabel = QLabel("BLE: 0")
        self.ui.bleCountLabel.setAlignment(Qt.AlignCenter)
        self.set_label_ok(self.ui.bleCountLabel)
        self.ui.horizontalLayout.addWidget(self.ui.bleCountLabel, 0)

        self.victron_poller = VictronBlePoller(
            battery_mac=victron_secrets.BATTERY_MAC, battery_key=victron_secrets.BATTERY_KEY,
            solar_mac=victron_secrets.SOLAR_MAC, solar_key=victron_secrets.SOLAR_KEY,
            inverter_mac=victron_secrets.INVERTER_MAC, inverter_key=victron_secrets.INVERTER_KEY,
            solar2_mac=victron_secrets.SOLAR2_MAC, solar2_key=victron_secrets.SOLAR2_KEY,
            charger_aux_mac=victron_secrets.CHARGER_AUX_MAC, charger_aux_key=victron_secrets.CHARGER_AUX_KEY,
        )
        self.victron_poller.battery_signal.connect(self.handle_battery_update)
        self.victron_poller.charger_aux_signal.connect(self.handle_charger_aux_update)
        self.victron_poller.solar_signal.connect(self.handle_solar_update)
        self.victron_poller.inverter_signal.connect(self.handle_inverter_update)
        self.victron_poller.start()

        # No longer polled directly (JbdBmsPoller, removed) - the BMS only
        # accepts one BLE connection at a time, and the ESP now reads it
        # over its own separate BLE radio and pushes the numbers here over
        # Modbus (registers 4-7, see refresh_from_datastore and
        # battery-bms.yaml) instead of competing with the ESP for it.

        self.battery_labels = [self.ui.batterySOC, self.ui.batteryTTG, self.ui.batteryVoltage,
                                self.ui.batteryConsumed, self.ui.batteryCurrent, self.ui.batteryPower,
                                self.ui.mainBatteryVoltage]
        self.solar_labels = [self.ui.solarState, self.ui.solarCurrent,
                              self.ui.solarPower, self.ui.solarYield]
        self.inverter_labels = [self.ui.inverterState, self.ui.inverterVoltage,
                                 self.ui.inverterCurrent, self.ui.inverterPowerOut]
        self.charger_aux_labels = [self.ui.chargerVoltage, self.ui.chargerCurrent,
                                    self.ui.chargerConsumed, self.ui.chargerPower]
        self.victron_stale_text = {
            self.ui.batterySOC: "SOC: --", self.ui.batteryTTG: "TTG: --",
            self.ui.batteryVoltage: "Voltage: --", self.ui.batteryConsumed: "Consumed: --",
            self.ui.batteryCurrent: "Current: --", self.ui.batteryPower: "Power: --",
            self.ui.solarState: "State: --",
            self.ui.solarCurrent: "Current: --", self.ui.solarPower: "Power: --",
            self.ui.solarYield: "Yield: --",
            self.ui.inverterState: "State: --", self.ui.inverterVoltage: "Voltage: --",
            self.ui.inverterCurrent: "Current: --", self.ui.inverterPowerOut: "Power: --",
            self.ui.chargerVoltage: "Voltage: --", self.ui.chargerCurrent: "Current: --",
            self.ui.chargerConsumed: "Consumed: --", self.ui.chargerPower: "Power: --",
            self.ui.mainBatteryVoltage: "Voltage: --",
        }
        for label in self.battery_labels + self.solar_labels + self.inverter_labels + self.charger_aux_labels:
            label.setText(self.victron_stale_text[label])
            self.set_label_ok(label)
        # Plain coil-driven labels (not part of the Victron BLE staleness
        # system above) that were showing the app's ambient green default
        # instead of the standard grey - same root cause the other labels
        # already had fixed earlier tonight, just missed for these two
        # since they were only just wired up (see conversation).
        self.set_label_ok(self.ui.chargerState1)
        self.set_label_ok(self.ui.chargerState2)

        self.ui.solarVoltage.hide()
        for widget in (self.ui.solarState, self.ui.solarCurrent, self.ui.solarPower, self.ui.solarYield):
            self.ui.gridLayout_9.removeWidget(widget)
        self.ui.gridLayout_9.addWidget(self.ui.solarState, 0, 0)
        self.ui.gridLayout_9.addWidget(self.ui.solarCurrent, 0, 1)
        self.ui.gridLayout_9.addWidget(self.ui.solarPower, 1, 0)
        self.ui.gridLayout_9.addWidget(self.ui.solarYield, 1, 1)

        self._last_battery_update = 0.0
        self._last_solar_update = 0.0
        self._last_inverter_update = 0.0
        self._last_charger_aux_update = 0.0
        self.ble_stale_threshold = 60

        # No has_data/error-debounce machinery here - a real modbus read can
        # genuinely fail (timeout, CRC error), but a local memory read never
        # does, so "red because the bus is unhappy" isn't a concept in this
        # version. Staleness (the ESP hasn't written this in a while) is the
        # only failure mode, tracked via TrackedDataBlock.last_write_time.
        self.sensor_stale_after = 30  # seconds since the ESP last wrote any of 1-3
        self.relay_stale_after = 30   # seconds since the ESP last wrote any coil

        # Deliberately empty, not pre-seeded with False for every button -
        # refresh_from_datastore's first_observation check (button not in
        # button_states) relies on a real absence to tell "never seen this
        # button's actual state yet" apart from "genuinely observed False",
        # otherwise every button looks like a already-observed change on the
        # very first status read after startup (see conversation - this was
        # the actual reason the automode-echo fix there didn't work).
        self.button_states = {}
        # How long to trust an optimistic click over the status coil before
        # giving up on it (see toggle_button/refresh_from_datastore - the ESP
        # only notices a click on its next request-coil poll, then has to
        # action the real relay and push the confirmed status back, all of
        # which is visibly slower than the 200ms UI refresh, see conversation).
        self.button_pending_until = {}
        self.button_pending_grace = 6.0
        for button, _ in self.relay_buttons:
            self.set_error_style(button)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_from_datastore)
        self.refresh_timer.start(UI_REFRESH_MS)

        self.ble_staleness_timer = QTimer(self)
        self.ble_staleness_timer.timeout.connect(self._check_ble_staleness)
        self.ble_staleness_timer.start(5000)

        self.datetime_timer = QTimer(self)
        self.datetime_timer.timeout.connect(self._update_datetime)
        self.datetime_timer.start(1000)
        self._update_datetime()

    def _update_datetime(self):
        self.dateTimeLabel.setText(datetime.datetime.now().strftime("%a %d %b  %H:%M:%S"))

    # --- local datastore access (function-code 1 = coils, 3 = holding regs) ---
    def _read_coil(self, address):
        return bool(self.coils.getValues(address, 1)[0])

    def _write_coil(self, address, value):
        self.coils.setValues(address, [bool(value)])

    def _read_registers(self, address, count):
        return self.holding_regs.getValues(address, count)

    def toggle_button(self, button, coil):
        if coil in MOMENTARY_COILS:
            self._press_momentary(button, coil)
            return
        # Writes to the REQUEST range (offset), never the status coil
        # directly - see REQUEST_COIL_OFFSET. Writing locally is instant and
        # can't fail; the open question is only whether the ESP notices and
        # agrees, which shows up as the status range (read by
        # refresh_from_datastore) updating to match a moment later.
        new_state = not self.button_states.get(button, False)
        self._write_coil(coil + REQUEST_COIL_OFFSET, new_state)
        self.button_states[button] = new_state
        self.update_button_style(button, new_state)
        self.button_pending_until[button] = time.monotonic() + self.button_pending_grace

    def _press_momentary(self, button, coil):
        # Window up/down etc: a rising edge for the ESP to notice
        # (on_press), not a persistent state - see MOMENTARY_COILS. Flash
        # green then release the request coil back to False on a timer,
        # rather than leaving it (and the button) stuck "on" until a second
        # click (see conversation).
        self._write_coil(coil + REQUEST_COIL_OFFSET, True)
        self.button_states[button] = True
        self.update_button_style(button, True)
        QTimer.singleShot(
            MOMENTARY_PRESS_MS,
            lambda: self._release_momentary(button, coil),
        )

    def _release_momentary(self, button, coil):
        self._write_coil(coil + REQUEST_COIL_OFFSET, False)
        self.button_states[button] = False
        self.update_button_style(button, False)

    def refresh_from_datastore(self):
        now = time.monotonic()
        relay_stale = (now - self.coils.last_write_time) > self.relay_stale_after if self.coils.last_write_time else True
        for button, coil in self.relay_buttons:
            state = self._read_coil(coil)
            if coil in AUTOMODE_COILS and self.button_states.get(button) is True and state is False:
                print(f"[automode] {COIL_NAMES.get(coil, coil)} observed going OFF (was ON) - relay_stale={relay_stale}", flush=True)
            if relay_stale:
                if getattr(button, "_prev_error", None) is not True:
                    self.set_error_style(button)
                    button._prev_error = True
            else:
                pending = self.button_pending_until.get(button, 0) > now
                if pending and state != self.button_states.get(button):
                    # ESP hasn't caught up to the click yet - the status coil
                    # is still reporting the pre-click value. Keep showing the
                    # optimistic style rather than flickering back to it
                    # before flipping to the real (matching) state a moment
                    # later (see conversation - this is what click -> green
                    # -> grey -> [relay switches] -> green was).
                    continue
                if pending:
                    self.button_pending_until.pop(button, None)
                # button_states has no entry for this button yet on the very
                # first non-stale observation after startup - that's not a
                # "the ESP changed something behind our back" event, it's
                # just dash finding out what the ESP's state already is. The
                # echo-write below must not fire for it: on restart, every
                # currently-active coil (worklights left on, auto-modes
                # enabled, etc.) would otherwise get a request-coil write it
                # never asked for, and any ESP-side entity that isn't a pure
                # level switch (a toggle/automation-triggering one, as the
                # automode coils apparently are - see conversation, "auto
                # modes being cleared" on every dash restart) applies that as
                # a real command instead of the no-op it's meant to be.
                first_observation = button not in self.button_states
                if getattr(button, "_prev_error", None) is not False or self.button_states.get(button) != state:
                    self.update_button_style(button, state)
                    button._prev_error = False
                    # The real status just changed for a reason other than
                    # our own click (e.g. the ESP's white/amber interlock
                    # forcing the other one off) - our own request coil for
                    # this button is still sitting at whatever we last sent,
                    # so it's now out of sync with reality. Since the ESP
                    # only reacts to the request bit actually changing, a
                    # future click here would just re-send the same
                    # (already-stale) value and do nothing (see
                    # conversation - "press amber again and it fails").
                    # Re-sync it to match the real state we just observed so
                    # the next click is a genuine transition again.
                    # Automode coils are excluded outright, not just guarded
                    # by first_observation - logging (above) caught this
                    # echo-write actually firing mid-flap during a live
                    # automode-clearing episode, with the real ESP-pushed
                    # status for these bouncing True/False within
                    # milliseconds at the source (see conversation - ruled
                    # out a local read race, pymodbus's datastore is a plain
                    # list slice, atomic under the GIL). Whatever's flapping
                    # the source value, dash re-writing into it on every
                    # observed flip is at best pointless and at worst adds
                    # fuel - safest to never touch these four at all here.
                    if coil not in MOMENTARY_COILS and not first_observation and coil not in AUTOMODE_COILS:
                        self._write_coil(coil + REQUEST_COIL_OFFSET, state)
                self.button_states[button] = state

        # DC Charger / Aux widget's two state labels - separate plain-text
        # labels from the charger1Power/charger2Power buttons above, but
        # backed by the same coils, so just reuse relay_stale/_read_coil
        # rather than tracking anything new.
        if relay_stale:
            self.ui.chargerState1.setText("State1: Error")
            self.ui.chargerState2.setText("State2: Error")
        else:
            charger1_on = self._read_coil(COIL_MAP["charger1Power"])
            charger2_on = self._read_coil(COIL_MAP["charger2Power"])
            self.ui.chargerState1.setText(f"State1: {'ON' if charger1_on else 'OFF'}")
            self.ui.chargerState2.setText(f"State2: {'ON' if charger2_on else 'OFF'}")

        sensor_stale = (now - self.holding_regs.last_write_time) > self.sensor_stale_after if self.holding_regs.last_write_time else True
        if sensor_stale:
            self.ui.tempLabel.setText("Inside Temperature: Error")
            self.ui.humidityLabel.setText("Inside Humidity: Error")
            self.ui.outsideTempLabel.setText("Outside Temperature: Error")
            self.set_label_error(self.ui.tempLabel)
            self.set_label_error(self.ui.humidityLabel)
            self.set_label_error(self.ui.outsideTempLabel)
        else:
            out_temp, in_temp, in_humid = self._read_registers(REG_OUTSIDE_TEMP, 3)
            self.ui.tempLabel.setText(f"Inside Temperature: {in_temp / 10.0:.1f} °C")
            self.ui.humidityLabel.setText(f"Inside Humidity: {in_humid / 10.0:.1f} %")
            self.ui.outsideTempLabel.setText(f"Outside Temperature: {float(out_temp):.1f} °C")
            self.set_label_ok(self.ui.tempLabel)
            self.set_label_ok(self.ui.humidityLabel)
            self.set_label_ok(self.ui.outsideTempLabel)

            bms_voltage, bms_current, bms_soc, bms_remaining = self._read_registers(REG_BMS_VOLTAGE, 4)
            if bms_current >= 32768:  # signed int16 (see REG_BMS_CURRENT)
                bms_current -= 65536
            voltage = bms_voltage / 100.0
            current = bms_current / 100.0
            soc = bms_soc / 100.0
            remaining_ah = bms_remaining / 100.0
            # The ESP doesn't push a full-capacity register (see
            # conversation) - remaining_ah/soc reconstructs it from the
            # BMS's own numbers rather than needing another register/flash.
            full_ah = remaining_ah / (soc / 100.0) if soc > 0 else remaining_ah
            self.handle_jbd_battery_update(voltage, current, soc, remaining_ah, full_ah)

        count = sum(1 for c in (COIL_JAMES_FOB, COIL_JAMES_PHONE, COIL_OLGA_FOB, COIL_OLGA_PHONE) if self._read_coil(c))
        self.ui.bleCountLabel.setText(f"BLE: {count}")

        if self._read_coil(COIL_SHUTDOWN_REQUEST):
            print("[shutdown] ESP requested a clean shutdown - shutting down now", flush=True)
            self.refresh_timer.stop()
            import subprocess
            subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"])

    def _check_ble_staleness(self):
        now = time.monotonic()
        if now - self._last_battery_update > self.ble_stale_threshold:
            for label in self.battery_labels:
                label.setText(self.victron_stale_text[label])
        if now - self._last_solar_update > self.ble_stale_threshold:
            for label in self.solar_labels:
                label.setText(self.victron_stale_text[label])
        if now - self._last_inverter_update > self.ble_stale_threshold:
            for label in self.inverter_labels:
                label.setText(self.victron_stale_text[label])
        if now - self._last_charger_aux_update > self.ble_stale_threshold:
            for label in self.charger_aux_labels:
                label.setText(self.victron_stale_text[label])

    def handle_battery_update(self, voltage, current, soc, consumed_ah, remaining_mins, main_battery_voltage):
        # Victron's shunt still owns the starter/main battery reading (it's
        # on the same device's aux terminal) - the AUX/leisure battery
        # fields below are now fed by the JBD BMS instead (see
        # handle_jbd_battery_update and conversation).
        self._last_battery_update = time.monotonic()
        if main_battery_voltage is None:
            self.ui.mainBatteryVoltage.setText("Voltage: N/A")
        else:
            self.ui.mainBatteryVoltage.setText(f"Voltage: {main_battery_voltage:.2f} V")

    def handle_jbd_battery_update(self, voltage, current, soc, remaining_ah, full_ah):
        self._last_battery_update = time.monotonic()
        self.ui.batteryVoltage.setText(f"Voltage: {voltage:.2f} V")
        self.ui.batterySOC.setText(f"SOC: {soc:.0f} %")
        self.ui.batteryConsumed.setText(f"Consumed: {full_ah - remaining_ah:.1f} Ah")
        self.ui.batteryCurrent.setText(f"Current: {current:.2f} A")
        self.ui.batteryPower.setText(f"Power: {voltage * current:.0f} W")
        if current < 0:
            hours, mins = divmod(int(remaining_ah / -current * 60), 60)
            self.ui.batteryTTG.setText(f"TTG: {hours}h {mins}m")
        else:
            self.ui.batteryTTG.setText("TTG: N/A")

    def handle_charger_aux_update(self, voltage, current):
        self._last_charger_aux_update = time.monotonic()
        self.ui.chargerVoltage.setText(f"Voltage: {voltage:.2f} V")
        self.ui.chargerConsumed.setText("Consumed: N/A")  # device has no Ah counter (see conversation)
        # Same display-sign flip as the battery panel above - this device
        # reports negative while actively charging, but "charging" should
        # read positive here (see conversation).
        self.ui.chargerCurrent.setText(f"Current: {-current:.2f} A")
        self.ui.chargerPower.setText(f"Power: {-voltage * current:.0f} W")

    def handle_solar_update(self, state_text, voltage, current, power, yield_today):
        self._last_solar_update = time.monotonic()
        self.ui.solarState.setText(f"State: {state_text}")
        self.ui.solarCurrent.setText(f"Current: {current:.2f} A")
        self.ui.solarPower.setText(f"Power: {power:.0f} W")
        self.ui.solarYield.setText(f"Yield: {yield_today:.0f} Wh")

    def handle_inverter_update(self, state_text, voltage, current, power):
        self._last_inverter_update = time.monotonic()
        self.ui.inverterState.setText(f"State: {state_text}")
        self.ui.inverterVoltage.setText(f"Voltage: {voltage:.2f} V")
        self.ui.inverterCurrent.setText(f"Current: {current:.2f} A")
        self.ui.inverterPowerOut.setText(f"Power: {power:.0f} VA")

    def update_button_style(self, button, state):
        if state:
            button.setStyleSheet("background-color: #0a0; color: white;")
        else:
            button.setStyleSheet("background-color: #444; color: white;")

    def set_error_style(self, button):
        button.setStyleSheet("background-color: #a00; color: white;")

    def set_label_error(self, label):
        label.setStyleSheet("background-color: #a00; color: white;")

    def set_label_ok(self, label):
        label.setStyleSheet("background-color: #444; color: white;")

    def closeEvent(self, event):
        self.victron_poller.stop()
        self.server_thread.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
