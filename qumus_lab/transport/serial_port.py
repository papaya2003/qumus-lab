"""Real serial ports.

pyserial is imported only when a real port is opened, so tests and --demo never touch a COM port.
Opening a port never sends a byte; drivers decide what (if anything) to send on connect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SerialLike(Protocol):
    """The subset of pyserial's Serial that the drivers use."""

    is_open: bool

    @property
    def in_waiting(self) -> int: ...

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int = 1) -> bytes: ...

    def readline(self) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class PortSpec:
    port: str
    baudrate: int = 9600
    timeout: float = 1.0
    write_timeout: float | None = 2.0
    dsrdtr: bool = False
    rtscts: bool = False

    @classmethod
    def from_config(cls, cfg: dict) -> PortSpec:
        return cls(
            port=str(cfg["port"]),
            baudrate=int(cfg.get("baudrate", 9600)),
            timeout=float(cfg.get("timeout", 1.0)),
            write_timeout=float(cfg["write_timeout"]) if cfg.get("write_timeout") is not None else 2.0,
            dsrdtr=bool(cfg.get("dsrdtr", False)),
            rtscts=bool(cfg.get("rtscts", False)),
        )


class RealSerialFactory:
    """Opens real COM ports. Only used by the lab launcher / probe without --demo."""

    demo = False

    def open(self, spec: PortSpec) -> SerialLike:
        import serial  # lazy: pyserial

        return serial.Serial(
            spec.port,
            baudrate=spec.baudrate,
            timeout=spec.timeout,
            write_timeout=spec.write_timeout,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=spec.rtscts,
            dsrdtr=spec.dsrdtr,
        )


def list_serial_ports() -> list[dict]:
    """List COM ports visible to the OS (read-only, opens nothing)."""
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        return []
    out = []
    for p in list_ports.comports():
        out.append(
            {
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "serial_number": getattr(p, "serial_number", None),
            }
        )
    return out
