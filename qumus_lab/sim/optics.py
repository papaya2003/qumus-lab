"""--demo microscope: a hidden "true" world of chips, flakes and stamps, rendered as microscope frames.

What the frame shows follows the simulated devices: sample stage x/y/rail, turret objective, stamp
x/y/z/rail and the transfer-stage temperature. Nothing here is read by the lab code: the L4 operations
only see camera frames and device readings, exactly as on the real bench. The ledger (LabStore) is read
only to learn which chip the arm put on the transfer stage and what it was exfoliated with.

Physics (demo only, from the v2 sim rules in qumus-agent):
- the stamp dome touches the chip when stamp z < Z_CONTACT; contact radius a = sqrt(2 R d);
- lifting after contact below the PC melt temperature picks up hBN flakes that were fully inside the
  contact disc; graphene comes up only when hBN already on the stamp covers >= 80 % of it;
- lifting after contact above the melt temperature releases everything on the stamp onto the chip.

Sign convention (to be checked at L3; docs/vision.md): the chip point P (mm, chip frame) is under the
optical axis when the stage is at (x0 + Px, y0 + Py) where (x0, y0) = points.yaml sample_stage.microscope.
In the image, +x chip is to the right and +y chip is down. A stamp +x move moves the contact point left
in the image (v1 adjust_touching_point: delta_x = -(aim - touch) * mm_per_px).
"""

from __future__ import annotations

import hashlib
import math
import random
import threading
import time
from dataclasses import dataclass, field

Z_CONTACT = 5.2           # stamp z where the dome first touches (sim rule; v1 initialize z 5.4 is above)
DOME_R_MM = 5.7           # stamp dome radius (sim rule)
MELT_C = 170.0            # v1 molecule_drop_down melt_temp_c
GR_COVER_MIN = 0.8        # graphene lifts only when hBN covers >= 80 % of it (pickup rule 11-05)
STAMP_HOME = (6.0, 7.0)   # stamp x/y after load_stamp (v1)
STAMP_OFFSET_MM = (0.55, -0.42)  # where the dome apex sits relative to the axis at STAMP_HOME (demo)
STAMP_RAIL_MICROSCOPE = 120.0    # v1 initialize_stamp L target: stamp over the transfer stage
STAGE_RAIL_MICROSCOPE = 80.0     # v1 stage rail at the microscope
CHIP_HALF_MM = (5.0, 4.0)        # demo chips are 10 x 8 mm
FRAME = (960, 540)               # demo frames: a quarter of v1's 3840 x 2160

SUBSTRATE = (150, 92, 122)       # BGR, 285 nm SiO2 look
OUTSIDE = (38, 38, 42)
GOLD = (70, 175, 215)
PC_FILM = (12, 10, 4)            # added inside a dropped PC film


@dataclass
class TrueFlake:
    id: str
    material: str                 # "hBN" | "graphene"
    centre: tuple[float, float]   # mm, chip frame (or stamp frame when on a stamp)
    poly: list[tuple[float, float]]  # um, relative to centre
    shade: tuple[int, int, int]   # BGR added to the substrate
    label: str                    # "mono" | "thin" | "medium" | "thick" (truth)

    def area_um2(self) -> float:
        a = 0.0
        for (x1, y1), (x2, y2) in zip(self.poly, self.poly[1:] + self.poly[:1]):
            a += x1 * y2 - x2 * y1
        return abs(a) / 2.0

    def radius_um(self) -> float:
        return max(math.hypot(x, y) for x, y in self.poly)


@dataclass
class TrueChip:
    id: str
    material: str | None
    flakes: list[TrueFlake] = field(default_factory=list)
    electrodes: bool = False
    dropped: list[TrueFlake] = field(default_factory=list)   # flakes released here from a stamp
    films: list[tuple[float, float, float]] = field(default_factory=list)  # PC films (x, y, r mm)


@dataclass
class TrueStamp:
    id: str
    flakes: list[TrueFlake] = field(default_factory=list)   # centre = stamp frame (mm from the apex)
    used: bool = False


def _rng(key: str) -> random.Random:
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def _poly(r: random.Random, size_um: float) -> list[tuple[float, float]]:
    n = r.randint(5, 7)
    angles = sorted(r.uniform(0, 2 * math.pi) for _ in range(n))
    return [(size_um * r.uniform(0.65, 1.0) * math.cos(a), size_um * r.uniform(0.65, 1.0) * math.sin(a))
            for a in angles]


def make_flakes(chip_id: str, material: str) -> list[TrueFlake]:
    """Deterministic flakes for a chip: 4-5 per chip, >= 2.6 mm apart, >= 1.2 mm from the edge."""
    r = _rng(f"{chip_id}:{material}")
    hbn = material.lower().startswith("h")
    kinds = (["medium", "medium", "thin", "thick"] if hbn else ["mono", "mono", "thin", "thick"]) + [
        r.choice(["thin", "thick"])]
    shades = {  # BGR deltas on the substrate; graphene mono is faint but above the detector's 6 levels
        "hBN": {"thin": (22, 10, -6), "medium": (48, 26, -18), "thick": (70, 70, 40)},
        "graphene": {"mono": (-10, -12, -10), "thin": (-26, -30, -24), "thick": (-70, -75, -70)},
    }["hBN" if hbn else "graphene"]
    sizes = {"mono": (22, 34), "thin": (25, 40), "medium": (45, 70), "thick": (40, 90)}
    out: list[TrueFlake] = []
    tries = 0
    while len(out) < len(kinds) and tries < 2000:
        tries += 1
        c = (r.uniform(-CHIP_HALF_MM[0] + 1.2, CHIP_HALF_MM[0] - 1.2), r.uniform(-CHIP_HALF_MM[1] + 1.2,
                                                                                CHIP_HALF_MM[1] - 1.2))
        if any(math.hypot(c[0] - f.centre[0], c[1] - f.centre[1]) < 2.6 for f in out):
            continue
        k = kinds[len(out)]
        out.append(TrueFlake(f"{chip_id}#t{len(out) + 1}", "hBN" if hbn else "graphene", c,
                             _poly(r, r.uniform(*sizes[k])), shades[k], k))
    return out


def _inside(poly, x, y) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def coverage(top: TrueFlake, bottom_list: list[TrueFlake], top_centre, bottom_offset=(0.0, 0.0)) -> float:
    """Fraction of ``top`` (a chip flake at top_centre, mm) covered by any of bottom_list (stamp flakes,
    centres in the same frame after adding bottom_offset)."""
    n = hit = 0
    rad = top.radius_um()
    step = max(rad / 12.0, 0.5)
    y = -rad
    while y <= rad:
        x = -rad
        while x <= rad:
            if _inside(top.poly, x, y):
                n += 1
                px, py = top_centre[0] + x / 1000.0, top_centre[1] + y / 1000.0
                for b in bottom_list:
                    bx, by = b.centre[0] + bottom_offset[0], b.centre[1] + bottom_offset[1]
                    if _inside(b.poly, (px - bx) * 1000.0, (py - by) * 1000.0):
                        hit += 1
                        break
            x += step
        y += step
    return hit / n if n else 0.0


class DemoOptics:
    def __init__(self, world, config):
        self.world = world
        mic = config.points.get("points", {}).get("sample_stage", {}).get("microscope", {}).get("pos", {})
        self.x0, self.y0 = float(mic.get("x", -12.0)), float(mic.get("y", 8.0))
        stamp_cfg = config.module("stacking")["devices"].get("stamp", {}) if "stacking" in config.module_names else {}
        kin = stamp_cfg.get("kinesis", {})
        self.kserial = {a: str(c["serial"]) for a, c in kin.items()}
        heater = config.module("stacking")["devices"].get("transfer_heater", {}) if "stacking" in config.module_names else {}
        self.heater_serial = str(heater.get("sensor_serial", ""))
        turret = config.module("stacking")["devices"].get("turret", {}) if "stacking" in config.module_names else {}
        self.objectives = {int(pos): int(mag) for mag, pos in (turret.get("objectives") or {5: 1}).items()}
        self.ledger = None            # set by the launcher: () -> LabStore | None
        self.chips: dict[str, TrueChip] = {}
        self.stamps: dict[str, TrueStamp] = {}
        self.lock = threading.RLock()
        self._contact: dict | None = None
        self.log: list[dict] = []     # what the physics did (tests, demo explanation)
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- hidden world
    def _store(self):
        return self.ledger() if callable(self.ledger) else None

    def _sample_at(self, place: str) -> dict | None:
        st = self._store()
        if st is None:
            return None
        for s in st.list_samples():
            if s.get("location") == place:
                return s
        return None

    def chip_on_stage(self) -> TrueChip | None:
        s = self._sample_at("transfer_stage")
        if s is None:
            return None
        return self.chip(s["id"], s)

    def chip(self, chip_id: str, row: dict | None = None) -> TrueChip:
        with self.lock:
            c = self.chips.get(chip_id)
            if c is None:
                props = (row or {}).get("props") or {}
                material = props.get("material")
                electrodes = bool(props.get("electrodes")) or (row or {}).get("kind") in ("electrode_chip",)
                c = TrueChip(chip_id, material, make_flakes(chip_id, material) if material and not electrodes else [],
                             electrodes)
                self.chips[chip_id] = c
            return c

    def stamp_loaded(self) -> TrueStamp | None:
        s = self._sample_at("stamp_holder")
        if s is None:
            return None
        with self.lock:
            return self.stamps.setdefault(s["id"], TrueStamp(s["id"]))

    # ---------------------------------------------------------------- device readings
    def _stage(self):
        emu = self.world.by_device.get("sample_stage")
        if emu is None:
            return 0.0, 0.0, 0.0
        return emu.steps("X") / 12800.0, emu.steps("Y") / 12800.0, emu.steps("L") / 3280.0

    def _stamp(self):
        k = self.world.kcubes
        x = k[self.kserial["x"]].position() if "x" in self.kserial else STAMP_HOME[0]
        y = k[self.kserial["y"]].position() if "y" in self.kserial else STAMP_HOME[1]
        z = k[self.kserial["z"]].position() if "z" in self.kserial else 6.5
        rail_emu = self.world.by_device.get("stamp.rail")
        rail = rail_emu.steps("L") / 3280.0 if rail_emu is not None else 0.0
        return x, y, z, rail

    def objective(self) -> int:
        emu = self.world.by_device.get("turret")
        return self.objectives.get(getattr(emu, "position", 1), 5)

    def temp(self) -> float:
        m = self.world.thermal_by_sensor.get(self.heater_serial)
        return m.read() if m is not None else 22.0

    def axis_chip(self) -> tuple[float, float] | None:
        sx, sy, rail = self._stage()
        if abs(rail - STAGE_RAIL_MICROSCOPE) > 1.0:
            return None
        return sx - self.x0, sy - self.y0

    def apex_chip(self) -> tuple[float, float] | None:
        """Chip-frame position under the stamp apex, or None when the stamp is not over the field."""
        ax = self.axis_chip()
        x, y, _z, rail = self._stamp()
        if ax is None or abs(rail - STAMP_RAIL_MICROSCOPE) > 1.0 or self.stamp_loaded() is None:
            return None
        return (ax[0] - (x - STAMP_HOME[0]) + STAMP_OFFSET_MM[0], ax[1] - (y - STAMP_HOME[1]) + STAMP_OFFSET_MM[1])

    def contact_radius_mm(self) -> float:
        z = self._stamp()[2]
        d = Z_CONTACT - z
        return math.sqrt(2 * DOME_R_MM * d) if d > 0 else 0.0

    # ---------------------------------------------------------------- physics
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="demo-optics")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self.update()
            except Exception:  # noqa: BLE001 - demo physics must never kill the service
                pass
            time.sleep(0.02)

    def update(self) -> None:
        with self.lock:
            apex = self.apex_chip()
            chip = self.chip_on_stage()
            stamp = self.stamp_loaded()
            a = self.contact_radius_mm()
            if apex is not None and chip is not None and stamp is not None and a > 0:
                c = self._contact
                t = self.temp()   # the PC film melts if it was above MELT_C at any time while in contact
                if c is None or a >= c["a"]:
                    self._contact = {"a": a, "apex": apex, "chip": chip.id, "stamp": stamp.id,
                                     "temp": max(t, c["temp"]) if c else t}
                else:
                    c["temp"] = max(c["temp"], t)
                return
            if self._contact is not None and a <= 0:
                self._release(self._contact)
                self._contact = None

    def _release(self, c: dict) -> None:
        chip, stamp = self.chips.get(c["chip"]), self.stamps.get(c["stamp"])
        if chip is None or stamp is None:
            return
        apex, a = c["apex"], c["a"]
        if c["temp"] >= MELT_C:
            for f in stamp.flakes:
                chip.dropped.append(TrueFlake(f.id, f.material, (apex[0] + f.centre[0], apex[1] + f.centre[1]),
                                              f.poly, f.shade, f.label))
            chip.films.append((apex[0], apex[1], a))
            self.log.append({"event": "drop", "chip": chip.id, "stamp": stamp.id,
                             "flakes": [f.id for f in stamp.flakes], "apex": apex})
            stamp.flakes, stamp.used = [], True
            return
        picked = []
        for f in list(chip.flakes):
            if math.hypot(f.centre[0] - apex[0], f.centre[1] - apex[1]) + f.radius_um() / 1000.0 > a:
                continue
            rel = (f.centre[0] - apex[0], f.centre[1] - apex[1])
            if f.material == "graphene":
                hbn = [s for s in stamp.flakes if s.material == "hBN"]
                cov = coverage(f, hbn, rel) if hbn else 0.0
                if cov < GR_COVER_MIN:
                    self.log.append({"event": "not_lifted", "flake": f.id, "coverage": round(cov, 3)})
                    continue
            chip.flakes.remove(f)
            stamp.flakes.append(TrueFlake(f.id, f.material, rel, f.poly, f.shade, f.label))
            picked.append(f.id)
        self.log.append({"event": "lift", "chip": chip.id, "stamp": stamp.id, "picked": picked, "apex": apex,
                         "radius_mm": round(a, 3), "temp_c": round(c["temp"], 1)})

    # ---------------------------------------------------------------- rendering
    def render(self):
        import cv2
        import numpy as np

        self.update()
        w, h = FRAME
        img = np.full((h, w, 3), OUTSIDE, np.uint8)
        ax = self.axis_chip()
        chip = self.chip_on_stage() if ax is not None else None
        if ax is None or chip is None:
            cv2.putText(img, "no chip under the objective", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (90, 90, 90), 2)
            return img
        from ..vision import um_per_px

        mag = self.objective()
        mm_px = um_per_px(mag, w) / 1000.0

        def to_px(p):
            return (w / 2 + (p[0] - ax[0]) / mm_px, h / 2 + (p[1] - ax[1]) / mm_px)

        # substrate inside the chip outline
        x1, y1 = to_px((-CHIP_HALF_MM[0], -CHIP_HALF_MM[1]))
        x2, y2 = to_px((CHIP_HALF_MM[0], CHIP_HALF_MM[1]))
        cv2.rectangle(img, (int(max(-5, x1)), int(max(-5, y1))), (int(min(w + 5, x2)), int(min(h + 5, y2))),
                      SUBSTRATE, -1)
        if chip.electrodes:
            self._draw_electrodes(img, to_px, mm_px)
        for fx, fy, r in chip.films:
            cx, cy = to_px((fx, fy))
            overlay = img.copy()
            cv2.circle(overlay, (int(cx), int(cy)), int(r / mm_px), (165, 120, 140), -1)
            img[:] = cv2.addWeighted(overlay, 0.35, img, 0.65, 0)
        for f in chip.flakes + chip.dropped:
            self._draw_flake(img, f, f.centre, to_px, mm_px)
        apex = self.apex_chip()
        stamp = self.stamp_loaded()
        if apex is not None and stamp is not None:
            for f in stamp.flakes:
                self._draw_flake(img, f, (apex[0] + f.centre[0], apex[1] + f.centre[1]), to_px, mm_px)
            a = self.contact_radius_mm()
            if a > 0:
                cx, cy = to_px(apex)
                r = a / mm_px
                mask = np.zeros((h, w), np.uint8)
                cv2.circle(mask, (int(cx), int(cy)), int(r), 255, -1)
                lit = cv2.add(img, np.full_like(img, (28, 40, 6)))
                img[mask > 0] = lit[mask > 0]
                for k in range(4):  # Newton rings at the contact edge
                    cv2.circle(img, (int(cx), int(cy)), int(r * (1.0 + 0.04 * k)), (60, 40, 70), 2)
        return img

    @staticmethod
    def _draw_flake(img, f: TrueFlake, centre, to_px, mm_px) -> None:
        import cv2
        import numpy as np

        pts = np.array([to_px((centre[0] + x / 1000.0, centre[1] + y / 1000.0)) for x, y in f.poly], np.int32)
        if (pts[:, 0].max() < 0 or pts[:, 1].max() < 0 or pts[:, 0].min() > img.shape[1]
                or pts[:, 1].min() > img.shape[0]):
            return
        mask = np.zeros(img.shape[:2], np.uint8)
        cv2.fillPoly(mask, [pts], 255)
        shaded = np.clip(img.astype(np.int16) + np.array(f.shade, np.int16), 0, 255).astype(np.uint8)
        img[mask > 0] = shaded[mask > 0]

    @staticmethod
    def _draw_electrodes(img, to_px, mm_px) -> None:
        """Hall-bar electrode chip: 8 leads around a 40 x 16 um channel at the chip centre (drop target)."""
        import cv2
        import numpy as np

        tips = [(-0.026, 0.0), (0.026, 0.0), (-0.012, -0.014), (0.0, -0.014), (0.012, -0.014),
                (-0.012, 0.014), (0.0, 0.014), (0.012, 0.014)]
        for tx, ty in tips:
            far = (tx * 40, math.copysign(1.2, ty)) if ty else (math.copysign(1.2, tx), 0.0)
            p1, p2 = np.array(to_px((tx, ty))), np.array(to_px(far))
            width = max(2, int(0.004 / mm_px))
            cv2.line(img, tuple(int(v) for v in p1), tuple(int(v) for v in p2), GOLD, width)
            cv2.circle(img, tuple(int(v) for v in p2), max(4, int(0.08 / mm_px)), GOLD, -1)


class RenderCameraBackend:
    """Camera backend for --demo whose frames come from DemoOptics.render()."""

    ext = "png"           # captures: lossless (the vision diff works on them)
    preview_ext = "jpg"   # live view: JPEG like the real cameras

    def __init__(self, optics: DemoOptics):
        self.optics = optics

    def open(self) -> None:
        self.optics.start()

    def grab(self) -> tuple[bytes, int, int]:
        import cv2

        img = self.optics.render()
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            raise RuntimeError("PNG encode failed")
        return buf.tobytes(), img.shape[1], img.shape[0]

    def grab_preview(self, max_width: int, quality: int) -> tuple[bytes, int, int]:
        from ..drivers.cameras import _encode_preview

        return _encode_preview(self.optics.render(), max_width, quality)

    def autofocus_once(self) -> None:
        """The demo frame is always sharp."""

    def close(self) -> None:
        pass
