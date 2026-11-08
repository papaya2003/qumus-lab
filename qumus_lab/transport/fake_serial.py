"""Fake serial ports for tests and --demo.

A FakeSerial records every byte written (``tx``) and hands the bytes to an emulator, which may answer
by calling ``emit``. Replies can be delayed (to emulate a move that takes time) with ``emit(..., delay)``.
No real port is ever opened.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .serial_port import PortSpec


class Emulator:
    """Base class for instrument emulators. Subclasses override ``on_bytes``."""

    port: FakeSerial | None = None

    def attach(self, port: FakeSerial) -> None:
        self.port = port

    def on_bytes(self, data: bytes) -> None:  # pragma: no cover - overridden
        pass

    def emit(self, data: bytes | str, delay: float = 0.0) -> None:
        assert self.port is not None
        if isinstance(data, str):
            data = data.encode()
        self.port.push_rx(data, delay=delay)


class LineEmulator(Emulator):
    """Splits incoming bytes into lines on ``terminator`` and calls ``on_line``."""

    terminator = b"\n"

    def __init__(self) -> None:
        self._buf = b""

    def on_bytes(self, data: bytes) -> None:
        self._buf += data
        while self.terminator in self._buf:
            line, self._buf = self._buf.split(self.terminator, 1)
            self.on_line(line.decode(errors="replace").strip())

    def on_line(self, line: str) -> None:  # pragma: no cover - overridden
        pass


class FakeSerial:
    def __init__(self, spec: PortSpec, emulator: Emulator | None = None, *, silent: bool = False):
        self.spec = spec
        self.port = spec.port
        self.timeout = spec.timeout
        self.is_open = True
        self.tx = bytearray()
        self.writes: list[bytes] = []
        self._rx = bytearray()
        self._cv = threading.Condition()
        self._timers: list[threading.Timer] = []
        self.silent = silent
        self.emulator = emulator
        if emulator is not None:
            emulator.attach(self)

    # --- emulator side ---
    def push_rx(self, data: bytes, delay: float = 0.0) -> None:
        if self.silent:
            return
        if delay > 0:
            t = threading.Timer(delay, self.push_rx, args=(data, 0.0))
            t.daemon = True
            self._timers.append(t)
            t.start()
            return
        with self._cv:
            self._rx.extend(data)
            self._cv.notify_all()

    # --- pyserial-like side ---
    @property
    def in_waiting(self) -> int:
        with self._cv:
            return len(self._rx)

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError(f"{self.port} is closed")
        data = bytes(data)
        self.tx.extend(data)
        self.writes.append(data)
        if self.emulator is not None:
            self.emulator.on_bytes(data)
        return len(data)

    def _wait(self, pred: Callable[[], bool], timeout: float | None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while not pred():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return
                self._cv.wait(timeout=remaining if remaining is not None else 0.5)

    def read(self, size: int = 1) -> bytes:
        self._wait(lambda: len(self._rx) >= size, self.timeout)
        with self._cv:
            n = min(size, len(self._rx))
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def readline(self) -> bytes:
        self._wait(lambda: b"\n" in self._rx, self.timeout)
        with self._cv:
            idx = self._rx.find(b"\n")
            n = idx + 1 if idx >= 0 else len(self._rx)
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def read_all(self) -> bytes:
        with self._cv:
            out = bytes(self._rx)
            self._rx.clear()
            return out

    def reset_input_buffer(self) -> None:
        with self._cv:
            self._rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False
        for t in self._timers:
            t.cancel()


class FakeSerialFactory:
    """Maps port names to emulators. Unknown ports raise like a missing COM port would."""

    demo = True

    def __init__(self, emulators: dict[str, Emulator] | None = None, *, silent_ports: set[str] | None = None):
        self.emulators = dict(emulators or {})
        self.silent_ports = set(silent_ports or ())
        self.opened: dict[str, FakeSerial] = {}

    def open(self, spec: PortSpec) -> FakeSerial:
        if spec.port not in self.emulators and spec.port not in self.silent_ports:
            raise OSError(f"could not open port '{spec.port}': not present (fake)")
        port = FakeSerial(spec, self.emulators.get(spec.port), silent=spec.port in self.silent_ports)
        self.opened[spec.port] = port
        return port
