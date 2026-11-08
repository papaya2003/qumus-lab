"""Heater = selection relay + shared TP3005P supply + Dracal thermometer.

v1 heating sequence (exfoliation, chip_exfoliation_molecule.py:150-158; drop-down, molecule_drop_down.py:113-148):
set 27 V, output on, own relay on, other heater relay off, wait until the thermometer reaches the target,
then set the hold voltage; at the end output off and relay off.

Changed on purpose:
- every heat has a mandatory timeout; at the timeout the output goes off (v1 could heat forever);
- the other heater's relay is switched off *before* the supply output goes on;
- above ``target + overshoot_c`` the output is switched off until the temperature falls back;
- three failed thermometer reads in a row switch the heater off;
- mode ``until`` (used by the L2 probe): apply a fixed low voltage and switch off at the target.
"""

from __future__ import annotations

import threading
import time

from .base import Device, DriverError, Refused
from .power_supply import TP3005P
from .relay import RelaySwitch
from .thermometer import DracalThermometer


class Heater(Device):
    kind = "heater"
    fields = {"temp_c": "C", "target_c": "C", "set_v": "V", "phase": "text", "relay_on": "bool",
              "output_on": "bool", "deadline_unix": "s", "last_off_reason": "text"}

    def __init__(self, device_id: str, cfg: dict, relay: RelaySwitch, psu: TP3005P,
                 thermometer: DracalThermometer, other_relays: list[RelaySwitch] | None = None,
                 sleep=time.sleep):
        super().__init__(device_id, cfg)
        self.relay = relay
        self.psu = psu
        self.thermo = thermometer
        self.other_relays = list(other_relays or [])
        self.boost_v = float(cfg.get("boost_voltage", 27.0))
        self.hold_table = {float(k): float(v) for k, v in (cfg.get("hold_voltage_by_target") or {}).items()}
        self.max_c = float(cfg.get("max_c", 60.0))
        self.overshoot_c = float(cfg.get("overshoot_c", 15.0))
        self.poll_s = float(cfg.get("poll_s", 2.0))
        self.max_timeout_s = float(cfg.get("max_timeout_s", 1800))
        self._sleep = sleep
        self.phase = "off"
        self.target_c: float | None = None
        self.deadline_unix: float | None = None
        self.last_off_reason: str | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    # lifecycle -------------------------------------------------------------
    def connect(self) -> None:
        for dev in (self.relay, self.psu, self.thermo):
            if not dev.connected:
                dev.connect()
        self.connected = True

    def close(self) -> None:
        self.off("close")
        self.connected = False

    # reads -----------------------------------------------------------------
    def read(self) -> dict:
        self.thermo.read()
        self.stamp_read()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"temp_c": self.thermo.temp_c, "target_c": self.target_c, "set_v": self.psu.set_v,
                "phase": self.phase, "relay_on": self.relay.on,
                "output_on": self.psu.output_on if self.psu.owner == self.id else False,
                "deadline_unix": self.deadline_unix, "last_off_reason": self.last_off_reason}

    @property
    def heating(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # commands ----------------------------------------------------------------
    def hold_voltage_for(self, target_c: float) -> float:
        if float(target_c) in self.hold_table:
            return self.hold_table[float(target_c)]
        raise Refused(f"{self.id}: no approved hold voltage for {target_c} C (have {sorted(self.hold_table)})")

    def heat(self, target_c: float, timeout_s: float, *, mode: str = "boost_hold", volts: float | None = None) -> dict:
        """Start heating in a background thread. Returns at once; the phase shows progress."""
        self.require_connected()
        target_c = float(target_c)
        timeout_s = float(timeout_s)
        if target_c > self.max_c:
            raise Refused(f"{self.id}: target {target_c} C above max {self.max_c} C")
        if not 0 < timeout_s <= self.max_timeout_s:
            raise Refused(f"{self.id}: timeout_s must be in (0, {self.max_timeout_s}]")
        if mode == "boost_hold":
            hold_v = self.hold_voltage_for(target_c)
            start_v = self.boost_v
        elif mode == "until":
            if volts is None:
                raise Refused("mode 'until' needs volts")
            hold_v = None
            start_v = float(volts)
        else:
            raise Refused(f"unknown heat mode {mode!r}")
        if self.heating:
            self.off("replaced by a new heat command")
        self.psu.acquire(self.id)
        try:
            for other in self.other_relays:
                if other.on is not False:
                    other.set(False)
            self.relay.set(True)
            self.psu.set_voltage(start_v)
            self.psu.output(True)
        except Exception:
            self._shutdown("start failed")
            raise
        self.target_c = target_c
        self.deadline_unix = time.time() + timeout_s
        self.phase = "boost" if mode == "boost_hold" else "heating"
        self.last_off_reason = None
        self._cancel.clear()
        self._thread = threading.Thread(target=self._loop, args=(mode, hold_v, start_v), daemon=True,
                                        name=f"heater-{self.id}")
        self._thread.start()
        return self.snapshot()

    def _loop(self, mode: str, hold_v: float | None, start_v: float) -> None:
        fails = 0
        output_on = True
        while not self._cancel.is_set():
            if time.time() >= (self.deadline_unix or 0):
                self._shutdown("timeout")
                return
            try:
                t = self.thermo.read()["temp_c"]
                fails = 0
            except DriverError as exc:
                fails += 1
                self.last_error = str(exc)
                if fails >= 3:
                    self._shutdown("thermometer failed 3 times")
                    return
                self._sleep(self.poll_s)
                continue
            try:
                if mode == "until":
                    if t >= self.target_c:
                        self._shutdown("target reached")
                        self.phase = "reached"
                        return
                else:
                    if self.phase == "boost" and t >= self.target_c:
                        self.psu.set_voltage(hold_v)
                        self.phase = "hold"
                    if t > self.target_c + self.overshoot_c and output_on:
                        self.psu.output(False)
                        output_on = False
                        self.phase = "overshoot_off"
                    elif not output_on and t < self.target_c:
                        self.psu.output(True)
                        output_on = True
                        self.phase = "hold"
            except DriverError as exc:
                self.last_error = str(exc)
                self._shutdown(f"supply error: {exc}")
                return
            self._cancel.wait(self.poll_s)

    def _shutdown(self, reason: str) -> bool:
        ok = True
        if self.psu.owner in (None, self.id):
            try:
                self.psu.output(False)
            except Exception as exc:
                self.last_error = str(exc)
                ok = False
        try:
            self.relay.set(False)
        except Exception as exc:
            self.last_error = str(exc)
            ok = False
        self.psu.release(self.id)
        self.phase = "off"
        self.last_off_reason = reason
        self.deadline_unix = None
        return ok

    def off(self, reason: str = "heat_off") -> dict:
        self._cancel.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        if self.connected:
            self._shutdown(reason)
        return self.snapshot()

    def safe_state(self) -> bool | None:
        if not self.connected:
            return None
        self.off("safe_state")
        return self.phase == "off"
