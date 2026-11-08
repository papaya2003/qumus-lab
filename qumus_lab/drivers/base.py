"""Driver base classes and the error types every driver uses.

Rules shared by all drivers (Jialei 11-08, brief B):
- connect() opens the link and only *queries*; it never moves, homes, powers motors or switches anything.
- an actuating command whose reply does not arrive raises OutcomeUnknown; nothing is ever retried.
- safe_state() stops motion and turns heat off. It must be callable from any thread.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class DriverError(RuntimeError):
    """Base error for instrument drivers."""


class NotConnected(DriverError):
    pass


class FirmwareError(DriverError):
    """The instrument answered with an error."""


class NoReply(DriverError):
    """A query got no (or no valid) reply in time. Nothing moved because of it."""


class OutcomeUnknown(DriverError):
    """An actuating command was sent but its result is unknown. The service locks; never retry."""


class Refused(DriverError):
    """The command was refused before any byte was sent."""


def fmt_num(value: float | int) -> str:
    """Format a number the way v1's f-strings did (int stays int, float keeps Python repr)."""
    if isinstance(value, bool):
        raise TypeError("bool is not a coordinate")
    if isinstance(value, int):
        return str(value)
    return repr(round(float(value), 4))


class Device:
    """One instrument (or one composite instrument) as seen by a module."""

    kind = "device"
    #: field name -> unit, reported by describe
    fields: dict[str, str] = {}

    def __init__(self, device_id: str, cfg: dict | None = None):
        self.id = device_id
        self.cfg = dict(cfg or {})
        self.connected = False
        self.last_error: str | None = None
        self.last_read_unix: float | None = None
        self._lock = threading.RLock()

    # lifecycle -------------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def require_connected(self) -> None:
        if not self.connected:
            raise NotConnected(f"{self.id} is not connected")

    # reading ---------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """Query the instrument. Returns fields with units in their names (x_mm, temp_c ...)."""
        return {}

    def snapshot(self) -> dict[str, Any]:
        """Last known values without talking to the instrument (cheap; used by /api/state)."""
        return {}

    def status(self) -> dict[str, Any]:
        out = {"kind": self.kind, "connected": self.connected, "error": self.last_error}
        out.update(self.snapshot())
        return out

    def stamp_read(self) -> None:
        self.last_read_unix = time.time()

    # safety ----------------------------------------------------------
    def stop(self) -> bool | None:
        """Stop motion now. True = confirmed, False = could not confirm, None = nothing to stop."""
        return None

    def safe_state(self) -> bool | None:
        return self.stop()
