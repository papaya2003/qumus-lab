"""Heater = selection relay + shared TP3005P supply + Dracal thermometer.

v1 heating sequence (exfoliation, chip_exfoliation_molecule.py:150-158; drop-down, molecule_drop_down.py:113-148):
set 27 V, output on, own relay on, other heater relay off, wait until the thermometer reaches the target,
then set the hold voltage; at the end output off and relay off.

Changed on purpose:
- every heat has a mandatory timeout; at the timeout the output goes off (v1 could heat forever);
- the other heater's relay is switched off *before* the supply output goes on;
- above ``target + overshoot_c`` (and never above ``max_c``) the output is switched off until the temperature
  falls back;
- three failed thermometer reads in a row switch the heater off;
- mode ``until`` (used by the L2 probe): apply a fixed low voltage and switch off at the target;
- any error in the control thread switches the heater off (the thread never ends with the supply on);
- a switch-off that cannot be confirmed raises OutcomeUnknown (phase ``off_unconfirmed``), so the service
  locks instead of reporting "off";
- at connect, a supply found on with no heater driving it (a previous run died while heating) is switched off.
"""

from __future__ import annotations

import threading
import time

from .base import Device, DriverError, OutcomeUnknown, Refused
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
        # v1 used another start voltage for some targets (transfer stage 90 C: 20 V, set_initial_temp)
        self.boost_table = {float(k): float(v) for k, v in (cfg.get("boost_voltage_by_target") or {}).items()}
        self.max_c = float(cfg.get("max_c", 60.0))
        self.overshoot_c = float(cfg.get("overshoot_c", 15.0))
        self.poll_s = float(cfg.get("poll_s", 2.0))
        self.max_timeout_s = float(cfg.get("max_timeout_s", 1800))
        self._sleep = sleep
        self.phase = "off"
        self.auto_hold = True
        self._hold_v: float | None = None
        self.target_c: float | None = None
        self.deadline_unix: float | None = None
        self.last_off_reason: str | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        # Serialises the control thread's supply writes with heat() / off(), so an off can not be followed
        # by a late "output on" from a control step that was already running.
        self._state_lock = threading.RLock()

    # lifecycle -------------------------------------------------------------
    def connect(self) -> None:
        # The other heater's relay is needed too: heat() switches it off before the shared supply goes on.
        for dev in (self.relay, self.psu, self.thermo, *self.other_relays):
            if not dev.connected:
                dev.connect()
        self.connected = True
        self._switch_off_leftover_supply()

    def _switch_off_leftover_supply(self) -> None:
        """A supply that is on while no heater of this process drives it was left on by a run that died."""
        if self.psu.owner is not None:
            return
        try:
            out_v = self.psu.read().get("out_v")
        except DriverError as exc:
            self.last_error = str(exc)
            out_v = None
        if out_v is not None and out_v <= 0.5:
            return
        found = "no VOUT1? reply" if out_v is None else f"VOUT1? {out_v:.2f} V"
        if self._shutdown(f"supply found on at connect ({found}); switched off"):
            self.phase = "off"

    def close(self) -> None:
        try:
            self.off("close")
        finally:
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

    def control_lost(self) -> bool:
        """True when the heater should be under control (a heating phase) but its thread is gone."""
        with self._state_lock:
            return self.phase not in ("off", "reached", "off_unconfirmed") and not self.heating

    # commands ----------------------------------------------------------------
    def hold_voltage_for(self, target_c: float) -> float:
        if float(target_c) in self.hold_table:
            return self.hold_table[float(target_c)]
        raise Refused(f"{self.id}: no approved hold voltage for {target_c} C (have {sorted(self.hold_table)})")

    def heat(self, target_c: float, timeout_s: float, *, mode: str = "boost_hold", volts: float | None = None,
             auto_hold: bool = True) -> dict:
        """Start heating in a background thread. Returns at once; the phase shows progress.

        ``auto_hold=False`` (boost_hold only): stay at the boost voltage past the target until the caller
        calls ``hold_now()`` (v1 drop_down keeps 27 V until it has seen > 170 C). The overshoot cut-off and
        the timeout still apply."""
        self.require_connected()
        target_c = float(target_c)
        timeout_s = float(timeout_s)
        if target_c > self.max_c:
            raise Refused(f"{self.id}: target {target_c} C above max {self.max_c} C")
        if not 0 < timeout_s <= self.max_timeout_s:
            raise Refused(f"{self.id}: timeout_s must be in (0, {self.max_timeout_s}]")
        if mode == "boost_hold":
            hold_v = self.hold_voltage_for(target_c)
            start_v = min(self.boost_table.get(target_c, self.boost_v), self.boost_v)
        elif mode == "until":
            if volts is None:
                raise Refused("mode 'until' needs volts")
            hold_v = None
            start_v = float(volts)
        else:
            raise Refused(f"unknown heat mode {mode!r}")
        if self.heating:
            self.off("replaced by a new heat command")
        with self._state_lock:
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
            self.auto_hold = bool(auto_hold)
            self._hold_v = hold_v
            self.deadline_unix = time.time() + timeout_s
            self.phase = "boost" if mode == "boost_hold" else "heating"
            self.last_off_reason = None
            self._cancel.clear()
            self._thread = threading.Thread(target=self._loop, args=(mode, hold_v, start_v), daemon=True,
                                            name=f"heater-{self.id}")
            self._thread.start()
        return self.snapshot()

    def _loop(self, mode: str, hold_v: float | None, start_v: float) -> None:
        try:
            self._control(mode, hold_v, start_v)
        except Exception as exc:  # noqa: BLE001 - e.g. OSError from the Dracal exe, SerialException
            self.last_error = f"{type(exc).__name__}: {exc}"
            with self._state_lock:
                if not self._cancel.is_set():
                    self._shutdown(f"control error: {type(exc).__name__}: {exc}")

    def _control(self, mode: str, hold_v: float | None, start_v: float) -> None:
        fails = 0
        while not self._cancel.is_set():
            if time.time() >= (self.deadline_unix or 0):
                with self._state_lock:
                    if not self._cancel.is_set():
                        self._shutdown("timeout")
                return
            try:
                t = self.thermo.read()["temp_c"]
                fails = 0
            except DriverError as exc:
                fails += 1
                self.last_error = str(exc)
                if fails >= 3:
                    with self._state_lock:
                        if not self._cancel.is_set():
                            self._shutdown("thermometer failed 3 times")
                    return
                self._sleep(self.poll_s)
                continue
            with self._state_lock:
                if self._cancel.is_set():  # off() ran while the thermometer was being read
                    return
                if not self._step(mode, hold_v, t):
                    return
            self._cancel.wait(self.poll_s)

    def _step(self, mode: str, hold_v: float | None, t: float) -> bool:
        """One control step under the state lock. False = the heater was switched off."""
        output_on = self.phase != "overshoot_off"
        try:
            if mode == "until":
                if t >= self.target_c:
                    if self._shutdown("target reached"):
                        self.phase = "reached"
                    return False
            else:
                if self.phase == "boost" and t >= self.target_c and getattr(self, "auto_hold", True):
                    self.psu.set_voltage(hold_v)
                    self.phase = "hold"
                # Cut the output above target + overshoot_c, and never above the configured max_c (a 170 C
                # target with 15 C overshoot used to allow 185 C, over the 175 C limit).
                if t > min(self.target_c + self.overshoot_c, self.max_c) and output_on:
                    self.psu.output(False)
                    self.phase = "overshoot_off"
                elif not output_on and t < self.target_c:
                    self.psu.output(True)
                    self.phase = "hold"
        except DriverError as exc:
            self.last_error = str(exc)
            self._shutdown(f"supply error: {exc}")
            return False
        return True

    def _shutdown(self, reason: str) -> bool:
        """Output off (if this heater may drive the supply) and own relay off. True only if both were
        confirmed by the drivers; otherwise the phase is ``off_unconfirmed`` and the supply stays owned by
        this heater, so the other heater can not switch it on until an off here succeeds."""
        with self._state_lock:
            ok = True
            if self.psu.owner in (None, self.id):
                try:
                    self.psu.output(False)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"supply off failed: {type(exc).__name__}: {exc}"
                    ok = False
            try:
                self.relay.set(False)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"relay off failed: {type(exc).__name__}: {exc}"
                ok = False
            if ok:
                self.psu.release(self.id)
            self.phase = "off" if ok else "off_unconfirmed"
            self.last_off_reason = reason
            self.deadline_unix = None
            return ok

    def hold_now(self) -> dict:
        """Switch a running boost_hold heat to its approved hold voltage now (see ``auto_hold``)."""
        with self._state_lock:
            if not self.heating or self._hold_v is None:
                raise Refused(f"{self.id}: not heating in boost_hold mode")
            self.psu.set_voltage(self._hold_v)
            if self.phase == "boost":
                self.phase = "hold"
            return self.snapshot()

    def off(self, reason: str = "heat_off") -> dict:
        """Switch off. Raises OutcomeUnknown when the off could not be confirmed (the caller locks)."""
        self._cancel.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        if self.connected and not self._shutdown(reason):
            raise OutcomeUnknown(f"{self.id}: heater off not confirmed ({self.last_error})")
        return self.snapshot()

    def safe_state(self) -> bool | None:
        if not self.connected:
            return None
        try:
            self.off("safe_state")
        except OutcomeUnknown:
            return False
        return self.phase == "off"
