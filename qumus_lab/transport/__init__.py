"""Byte transports: real serial ports (pyserial, imported lazily) and fake ports for tests and --demo."""

from .fake_serial import FakeSerial, FakeSerialFactory
from .serial_port import PortSpec, RealSerialFactory, SerialLike

__all__ = ["FakeSerial", "FakeSerialFactory", "PortSpec", "RealSerialFactory", "SerialLike"]
