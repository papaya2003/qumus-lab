"""Microscope image analysis used by the stacking L4 operations.

Two kinds of functions live here:

* PORTED from v1 (same thresholds, same steps): pixel sizes and unit conversion
  (stacking/molecule_flows/utils/tools.py), the Newton-ring first-touch detector and the
  "target covered by the contact front" check (utils/touching_detection.py), and the focus score
  (common/hardware/camera_controller.py blur_score_bgr_laplacian).
* PLACEHOLDER (not v1): ``find_flakes``. v1 searched with an ML segmentation observer that is not
  in this repo. The contrast detector below is only good enough for the --demo renderer. Edward's
  detector replaces it through ``set_detector`` (see docs/vision.md); nothing else changes.

All pixel numbers in v1 are for the full 3840 x 2160 frame. Functions here take the actual image and
scale v1's pixel thresholds by ``width / 3840`` (areas by the square), so a smaller demo frame behaves
like v1's.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

REF_WIDTH = 3840          # v1 frames: target_pt (1920, 1080) is the centre of 3840 x 2160
REF_HEIGHT = 2160

# v1 stacking/molecule_flows/utils/tools.py:3-6 (code value; docs say 0.4557, see docs/l3-measurements.csv)
UM_PER_PX_5X = 0.730
_PX_DIVISOR = {5: 1.0, 10: 2.0, 20: 4.0, 100: 20.0}   # 20x = 5x / 4, 100x = 5x / 20 (tools.py:5-6)

# v1 touching_detection.py:9-16
NEWTON_THRESH = 30
AREA_MIN_NEWTON = 5000
TOUCH_THRESH = 40
AREA_MIN = 5000
BLUR_KERNEL = 9


def _np():
    import numpy as np

    return np


def _cv2():
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - lab extra
        raise RuntimeError("vision needs opencv-python and numpy (pip install -e .[lab])") from exc
    return cv2


def um_per_px(objective: int, width_px: int = REF_WIDTH) -> float:
    """Micrometres per pixel for an objective and an image width (v1 values for the 3840 px frame)."""
    if int(objective) not in _PX_DIVISOR:
        raise ValueError(f"no pixel size for {objective}x")
    return UM_PER_PX_5X / _PX_DIVISOR[int(objective)] * (REF_WIDTH / float(width_px))


def scale(width_px: int) -> float:
    return float(width_px) / REF_WIDTH


def ref_point(pt: tuple[float, float], width_px: int) -> tuple[int, int]:
    """A v1 pixel (3840-wide frame) in this image."""
    s = scale(width_px)
    return int(round(pt[0] * s)), int(round(pt[1] * s))


def load(path) -> object:
    img = _cv2().imread(str(path), _cv2().IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"cannot read image {path}")
    return img


def gray(img):
    cv2 = _cv2()
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


# --------------------------------------------------------------------------- focus (v1)
def focus_score(img, shrink: int = 2) -> float:
    """v1 blur_score_bgr_laplacian: log variance of the Laplacian on a 2x shrunk grey image."""
    cv2, np = _cv2(), _np()
    g = gray(img)
    if shrink > 1:
        h, w = g.shape[:2]
        g = cv2.resize(g, (max(1, w // shrink), max(1, h // shrink)), interpolation=cv2.INTER_AREA)
    return float(np.log(float(cv2.Laplacian(g, cv2.CV_32F).var()) + 1e-12))


# --------------------------------------------------------------------------- touching (v1 port)
def _diff_mask(ref, cur, thresh: int, blur: int):
    cv2 = _cv2()
    r, c = cv2.convertScaleAbs(gray(ref).astype("float32")), cv2.convertScaleAbs(gray(cur).astype("float32"))
    if blur > 0:
        r, c = cv2.GaussianBlur(r, (blur, blur), 0), cv2.GaussianBlur(c, (blur, blur), 0)
    diff = cv2.absdiff(r, c)
    diff_norm = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)
    _, mask = cv2.threshold(diff_norm, thresh, 255, cv2.THRESH_BINARY)
    return mask.astype("uint8")


def _filtered(mask, area_min: float, ar_lo: float, ar_hi: float):
    cv2, np = _cv2(), _np()
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    out = np.zeros_like(mask)
    for i in range(1, n):
        w, h = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        if w == 0 or h == 0:
            continue
        if stats[i, cv2.CC_STAT_AREA] > area_min and ar_lo <= w / h <= ar_hi:
            out[labels == i] = 255
    return out


def first_touch(ref, cur) -> tuple[int, int] | None:
    """v1 detect_first_touching_point, one step: the largest round (aspect 0.8-1.25) changed region of
    area > 5000 px (v1 frame) between the reference and the current frame. Returns its centre or None."""
    cv2, np = _cv2(), _np()
    s2 = scale(cur.shape[1]) ** 2
    m = _filtered(_diff_mask(ref, cur, NEWTON_THRESH, 0), AREA_MIN_NEWTON * s2, 0.8, 1.25)
    if not np.count_nonzero(m):
        return None
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=4)
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    cx, cy = (int(v) for v in np.round(cents[best]))
    return cx, cy


def contact_contour(ref, cur):
    """v1 stake_target_flake, one step: contour of the largest changed region (blur 9, threshold 40,
    area > 5000 px, aspect 0.1-10). None when nothing changed."""
    cv2, np = _cv2(), _np()
    s2 = scale(cur.shape[1]) ** 2
    m = _filtered(_diff_mask(ref, cur, TOUCH_THRESH, BLUR_KERNEL), AREA_MIN * s2, 0.1, 10.0)
    if not np.count_nonzero(m):
        return None
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    largest = (labels == best).astype("uint8") * 255
    contours, _ = cv2.findContours(largest, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return max(contours, key=cv2.contourArea) if contours else None


def inside_distance(pt: tuple[float, float], contour) -> int:
    """v1 point_to_edge_distance_inside: distance (px) from pt to the contour edge, 0 when outside."""
    if contour is None:
        return 0
    d = _cv2().pointPolygonTest(contour, (float(pt[0]), float(pt[1])), True)
    return int(d) if d > 0 else 0


# --------------------------------------------------------------------------- flakes (placeholder)
@dataclass
class Flake:
    x_px: float
    y_px: float
    area_px: float
    contrast: float           # mean (substrate - flake) grey level, positive = darker than substrate
    bbox: tuple[int, int, int, int]
    label: str                # thickness class guess, e.g. "thin", "medium", "thick"


class FlakeDetector(Protocol):
    def __call__(self, img, *, objective: int, material: str | None) -> list[Flake]: ...


def contrast_flakes(img, *, objective: int, material: str | None, min_area_um2: float = 50.0) -> list[Flake]:
    """PLACEHOLDER detector (not v1): regions that differ from the substrate (median colour) by more
    than a few grey levels. Tuned on the --demo renderer only; untested on real chips."""
    cv2, np = _cv2(), _np()
    g = cv2.GaussianBlur(gray(img), (5, 5), 0).astype("int16")
    bg = int(np.median(g))
    diff = np.abs(g - bg)
    mask = (diff >= 6).astype("uint8") * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    upp = um_per_px(objective, img.shape[1])
    min_px = min_area_um2 / (upp * upp)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    h, w = mask.shape
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < min_px or bw >= w * 0.9 or bh >= h * 0.9:  # skip tiny specks and chip edges
            continue
        c = float(bg - g[labels == i].mean())
        label = contrast_label(c, material)
        out.append(Flake(float(cents[i][0]), float(cents[i][1]), float(area), c, (x, y, bw, bh), label))
    return sorted(out, key=lambda f: -f.area_px)


def contrast_label(contrast: float, material: str | None) -> str:
    """Thickness class names of the simulated lab / v1 search (hBN: nm bins; graphene: layers). The grey
    level thresholds are PLACEHOLDERS for the demo renderer, not a calibration."""
    a = abs(contrast)
    m = (material or "").lower()
    if m.startswith("h") or "bn" in m:
        return "0-10" if a < 10 else "10-20" if a < 20 else "20-30" if a < 40 else "30-40"
    if m.startswith("g"):
        return "monolayer" if a < 16 else "bilayer" if a < 40 else "manylayer"
    return "thin" if a < 14 else "medium" if a < 40 else "thick"


_detector: Callable[..., list[Flake]] = contrast_flakes


def set_detector(fn: Callable[..., list[Flake]]) -> None:
    """Replace the flake detector (Edward's). Same call signature as ``contrast_flakes``."""
    global _detector
    _detector = fn


def find_flakes(img, *, objective: int, material: str | None = None, min_area_um2: float = 50.0) -> list[Flake]:
    return _detector(img, objective=objective, material=material, min_area_um2=min_area_um2)


def px_to_mm(dx_px: float, dy_px: float, objective: int, width_px: int) -> tuple[float, float]:
    upp = um_per_px(objective, width_px) / 1000.0
    return dx_px * upp, dy_px * upp


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
