# Vision (microscope pictures) in qumus-lab

`qumus_lab/vision/__init__.py`. All numbers in v1 are for the full 3840 x 2160 frame; the functions scale
v1's pixel thresholds by `width / 3840` (areas by the square), so the quarter-size demo frames behave the
same.

| Function | Source | Used by |
| --- | --- | --- |
| `um_per_px(objective, width)` | v1 `stacking/molecule_flows/utils/tools.py:3-6` (5x 0.730 um/px code value; 20x = /4, 100x = /20; 10x = /2 assumed) | all |
| `first_touch(ref, cur)` | v1 `touching_detection.py` `detect_first_touching_point`, one step (threshold 30, area > 5000 px, aspect 0.8-1.25) | `adjust_touching_point` |
| `contact_contour(ref, cur)`, `inside_distance` | v1 `stake_target_flake`, one step (blur 9, threshold 40, area > 5000 px, aspect 0.1-10; stop when the target is inside by > 240 px) | `pick_up` |
| `focus_score(img)` | v1 `camera_controller.blur_score_bgr_laplacian` | (available) |
| `find_flakes(img, objective, material)` | **placeholder**, not v1 | `scan_chip`, `revisit_flake`, `go_to_flake`, `check_pickup` |

## The placeholder flake detector

v1 searched with an ML segmentation observer that is not in this repository. `contrast_flakes` only
finds regions whose grey level differs from the substrate median by >= 6 levels and names the class from
that difference (`contrast_label`): hBN `0-10 / 10-20 / 20-30 / 30-40`, graphene `monolayer / bilayer /
manylayer`, the class names of the simulated lab. The thresholds are tuned to the demo renderer only and
are **untested on real chips**.

### Replacing it (Edward)

```python
from qumus_lab import vision

def my_detector(img, *, objective, material, min_area_um2=50.0):
    ...
    return [vision.Flake(x_px, y_px, area_px, contrast, (x, y, w, h), label), ...]

vision.set_detector(my_detector)
```

`img` is the BGR numpy frame; `x_px, y_px` the centroid; `label` one of the class names above (they go to
the ledger as `thickness_label` / `labels["10x"]`, and the L6 script and the brain choose flakes by them).
Nothing else needs to change.

## Camera and stage conventions (check at L3)

- The microscope camera's own one-shot autofocus is used as in v1 (`Camera.autofocus_once`, uvcham
  `UVCHAM_AFMODE = 2`).
- `ops/stacking.py IMAGE_SIGN`: a feature at pixel p is brought to pixel T by moving the stage
  `IMAGE_SIGN * (p - T) * mm_per_px`. (+1, +1) matches the demo; on the bench, jog the stage 0.5 mm and
  watch which way the picture moves.
- A stamp +x move moves the contact point left in the picture (v1 `adjust_touching_point` sign).
- The pick-up target pixel is v1's (1900, 1100); `go_to_flake` re-finds the flake at 20x and leaves it
  there for 5x. v1 had two optical centres in different files ((1780, 1180) and (1790, 1220)); L3 row
  `microscope_camera.centre` decides.

## Demo microscope

`qumus_lab/sim/optics.py` draws each frame from the simulated stage x/y/rail, turret, stamp x/y/z/rail and
transfer-stage temperature. Chips get 4-5 flakes (deterministic per chip id), the electrode chip a Hall-bar
lead pattern at its centre. Contact: stamp z below 5.2 mm, radius sqrt(2 R d) with R = 5.7 mm; lifting picks
up hBN fully inside the contact, graphene only when hBN on the stamp covers >= 80 % of it; lifting after
the stage was above 170 C during contact releases the whole stack onto the chip. The lab code never
reads this world; only the frames.
