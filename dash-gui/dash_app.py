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
import asyncio
import threading
import logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel
from PyQt5.QtCore import Qt, QTimer, QThread
from gui import Ui_MainWindow
from victron_ble.scanner import Scanner
from victron_ble.exceptions import AdvertisementKeyMissingError, UnknownDeviceError
import victron_secrets

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartSerialServer
from pymodbus import FramerType

SLAVE_ADDRESS = 5  # provisional - must match the ESP's client entry for dash
SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600

# Holding registers (function code 3) - the ESP writes these, dash only reads.
REG_OUTSIDE_TEMP = 1   # plain int, no scaling
REG_INSIDE_TEMP = 2    # *10 scaled, same convention as the master version
REG_INSIDE_HUMID = 3   # *10 scaled

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


class ModbusSlaveServerThread(QThread):
    # Runs the RTU slave server for the life of the app. No graceful stop
    # implemented yet (see conversation) - this is still an experimental
    # first draft, and the process exiting is enough to close the port.
    def __init__(self, server_context):
        super().__init__()
        self.server_context = server_context

    def run(self):
        try:
            StartSerialServer(
                context=self.server_context,
                framer=FramerType.RTU,
                port=SERIAL_PORT,
                baudrate=BAUD_RATE,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1,
                # RS485 is multi-drop - dash's port sees every frame on the bus,
                # not just ones addressed to it (address 5). Without this,
                # pymodbus's default behaviour is to actively transmit a
                # GatewayNoResponse exception for every request meant for
                # someone else (the relay boards at 10/20) - garbage bytes
                # injected onto the wire on top of their real responses (see
                # conversation - a likely major contributor to the RTU framing
                # corruption diagnosed earlier tonight).
                ignore_missing_slaves=True,
            )
        except Exception as e:
            print(f"[modbus-slave] server failed: {e}", flush=True)


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
            zero_mode=True,
        )
        self.server_context = ModbusServerContext(slaves={SLAVE_ADDRESS: slave_ctx}, single=False)

        self.server_thread = ModbusSlaveServerThread(self.server_context)
        self.server_thread.start()

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

        self.button_states = {button: False for button, _ in self.relay_buttons}
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
                    if coil not in MOMENTARY_COILS:
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
        self._last_battery_update = time.monotonic()
        self.ui.batteryVoltage.setText(f"Voltage: {voltage:.2f} V")
        self.ui.batteryCurrent.setText(f"Current: {current:.2f} A")
        self.ui.batterySOC.setText(f"SOC: {soc:.0f} %")
        self.ui.batteryConsumed.setText(f"Consumed: {consumed_ah:.1f} Ah")
        self.ui.batteryPower.setText(f"Power: {voltage * current:.0f} W")
        if remaining_mins is None:
            self.ui.batteryTTG.setText("TTG: N/A")
        else:
            hours, mins = divmod(int(remaining_mins), 60)
            self.ui.batteryTTG.setText(f"TTG: {hours}h {mins}m")
        if main_battery_voltage is None:
            self.ui.mainBatteryVoltage.setText("Voltage: N/A")
        else:
            self.ui.mainBatteryVoltage.setText(f"Voltage: {main_battery_voltage:.2f} V")

    def handle_charger_aux_update(self, voltage, current):
        self._last_charger_aux_update = time.monotonic()
        self.ui.chargerVoltage.setText(f"Voltage: {voltage:.2f} V")
        self.ui.chargerCurrent.setText(f"Current: {current:.2f} A")
        self.ui.chargerConsumed.setText("Consumed: N/A")  # device has no Ah counter (see conversation)
        self.ui.chargerPower.setText(f"Power: {voltage * current:.0f} W")

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
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
