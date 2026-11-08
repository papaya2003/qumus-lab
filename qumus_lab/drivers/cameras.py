"""Cameras: two desk cameras (OpenCV, v1 indices left 0 / right 2) and the microscope camera (Uvcham SDK).

v1 sources: agents/lab_manager_agent/tools/cv/config.py (desk cameras 1920x1080, left index 0,
right index 2), common/hardware/camera_controller.py (Uvcham: enum, open(id), put(FORMAT, 0) = BGR,
pull into a stride-aligned buffer; OpenCV fallback with CAP_DSHOW, MJPG, buffer size 1).
cv2, numpy and the Uvcham module are imported lazily, only when a real camera is opened.
A capture is saved to ``runs/real-lab/captures/<image_id>.<ext>`` and returns its id.
"""

from __future__ import annotations

import os
import struct
import threading
import time
import uuid
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .base import Device, NoReply


class CameraBackend(Protocol):
    ext: str

    def open(self) -> None: ...
    def grab(self) -> tuple[bytes, int, int]: ...
    def close(self) -> None: ...


class OpenCVBackend:
    ext = "jpg"

    def __init__(self, index: int, width: int = 1920, height: int = 1080, jpeg_quality: int = 95):
        self.index, self.width, self.height, self.q = int(index), int(width), int(height), int(jpeg_quality)
        self.cap = None

    def open(self) -> None:
        import cv2  # lazy

        if os.name == "nt":
            cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap = cv2.VideoCapture(self.index, cv2.CAP_MSMF)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise NoReply(f"OpenCV camera index {self.index} did not open")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap = cap

    def grab(self) -> tuple[bytes, int, int]:
        import cv2

        frame = None
        for _ in range(3):  # drop stale buffered frames
            ok, frame = self.cap.read()
        if frame is None:
            raise NoReply(f"camera {self.index}: no frame")
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.q])
        if not ok:
            raise NoReply("JPEG encode failed")
        return buf.tobytes(), frame.shape[1], frame.shape[0]

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class UvchamBackend:
    """Microscope camera through the vendor ``uvcham`` module (lab PC only)."""

    ext = "jpg"

    def __init__(self, index: int, sdk_dir: str, jpeg_quality: int = 95):
        self.index, self.sdk_dir, self.q = int(index), sdk_dir, int(jpeg_quality)
        self.cam = None

    def open(self) -> None:
        import sys

        if os.name != "nt":
            raise NoReply("Uvcham SDK is Windows-only")
        if self.sdk_dir not in sys.path:
            sys.path.insert(0, self.sdk_dir)
        os.add_dll_directory(self.sdk_dir)
        import uvcham  # type: ignore  # vendor module, lives in sdk_dir (not in git)

        cams = uvcham.Uvcham.enum()
        if self.index >= len(cams):
            raise NoReply(f"Uvcham index {self.index} not found ({len(cams)} cameras)")
        self._uv = uvcham
        self.cam = uvcham.Uvcham.open(cams[self.index].id)
        if self.cam is None:
            raise NoReply("Uvcham open failed")
        self.cam.put(uvcham.UVCHAM_FORMAT, 0)  # BGR (v1)
        res = self.cam.get(uvcham.UVCHAM_RES)
        self.w = self.cam.get(uvcham.UVCHAM_WIDTH | res)
        self.h = self.cam.get(uvcham.UVCHAM_HEIGHT | res)
        self.stride = uvcham.TDIBWIDTHBYTES(self.w * 24)
        self.buf = bytes(self.stride * self.h)
        self.cam.start(None, lambda *_: None, self)
        time.sleep(0.3)

    def grab(self) -> tuple[bytes, int, int]:
        import cv2
        import numpy as np

        self.cam.pull(self.buf)
        raw = np.frombuffer(self.buf, np.uint8).reshape(self.h, self.stride)[:, : self.w * 3]
        img = raw.reshape(self.h, self.w, 3)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), self.q])
        if not ok:
            raise NoReply("JPEG encode failed")
        return buf.tobytes(), self.w, self.h

    def close(self) -> None:
        if self.cam is not None:
            self.cam.close()
            self.cam = None


# --------------------------------------------------------------------------- fake (tests / --demo)
def png_bytes(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")


class FakeCameraBackend:
    """Draws a simple synthetic frame. ``scene()`` returns marker points (u, v in 0..1) to draw."""

    ext = "png"

    def __init__(self, width: int = 320, height: int = 180, scene: Callable[[], list[tuple[float, float]]] | None = None,
                 tint: tuple[int, int, int] = (60, 70, 80)):
        self.width, self.height, self.scene, self.tint = width, height, scene or (lambda: []), tint
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def grab(self) -> tuple[bytes, int, int]:
        marks = [(int(u * self.width), int(v * self.height)) for u, v in self.scene()]
        w, h, (r0, g0, b0) = self.width, self.height, self.tint

        def px(x: int, y: int) -> tuple[int, int, int]:
            for mx, my in marks:
                if abs(x - mx) <= 6 and abs(y - my) <= 6:
                    return (240, 190, 60)
            if x % 40 == 0 or y % 40 == 0:
                return (r0 + 25, g0 + 25, b0 + 25)
            return (r0 + (x * 30) // w, g0 + (y * 30) // h, b0)

        return png_bytes(w, h, px), w, h

    def close(self) -> None:
        self.opened = False


class Camera(Device):
    kind = "camera"
    fields = {"last_image_id": "id", "width_px": "px", "height_px": "px", "last_capture_unix": "s"}

    def __init__(self, device_id: str, cfg: dict, backend: CameraBackend, capture_dir: Path):
        super().__init__(device_id, cfg)
        self.backend = backend
        self.capture_dir = Path(capture_dir)
        self.last_image_id: str | None = None
        self.size = (None, None)
        self.last_capture_unix: float | None = None
        self._cam_lock = threading.Lock()

    def connect(self) -> None:
        self.backend.open()
        self.connected = True

    def close(self) -> None:
        self.backend.close()
        self.connected = False

    def capture(self) -> dict:
        self.require_connected()
        with self._cam_lock:
            data, w, h = self.backend.grab()
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        image_id = f"{self.id}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        (self.capture_dir / f"{image_id}.{self.backend.ext}").write_bytes(data)
        self.last_image_id, self.size, self.last_capture_unix = image_id, (w, h), time.time()
        return {"image_id": image_id, "width_px": w, "height_px": h, "ext": self.backend.ext}

    def read(self) -> dict:
        self.require_connected()
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"last_image_id": self.last_image_id, "width_px": self.size[0], "height_px": self.size[1],
                "last_capture_unix": self.last_capture_unix}
