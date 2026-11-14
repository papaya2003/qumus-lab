"""docs/l3-measurements.csv stays in step with config/ and the v1 lines it cites."""

from __future__ import annotations

import csv
import fnmatch
import re

import pytest

from qumus_lab.config import REPO_ROOT, load_config

from .v1_harness import V1_ROOT, v1_tree_present

SHEET = REPO_ROOT / "docs" / "l3-measurements.csv"
CITE_RE = re.compile(r"^([\w/.]+):(\d+)$")


def _rows():
    with open(SHEET, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_every_named_point_is_in_the_sheet_and_every_point_row_exists():
    points = load_config().points["points"]
    keys = {r["写回哪里（qumus-lab）"].split()[1] for r in _rows()
            if r["写回哪里（qumus-lab）"].startswith("points.yaml ") and "新增" not in r["写回哪里（qumus-lab）"]}
    named = {f"{dev}.{name}" for dev, pts in points.items() for name in pts}
    # a key may be a glob ("arm_left.left_*") for a family of board slots
    assert all(any(fnmatch.fnmatchcase(n, k) for k in keys) for n in named), \
        sorted(n for n in named if not any(fnmatch.fnmatchcase(n, k) for k in keys))
    for k in keys:
        dev, _, name = k.partition(".")
        assert dev in points and (not name or any(fnmatch.fnmatchcase(p, name) for p in points[dev])), k


XYZ_RE = re.compile(r"^X(-?[\d.]+) Y(-?[\d.]+) Z(-?[\d.]+)$")


def test_ids_are_unique():
    rows = _rows()
    assert len({r["编号"] for r in rows}) == len(rows)


def test_adopted_arm_points_are_written_back_as_measured():
    """An arm-point row with an adopted value ("采用值", X.. Y.. Z..) must match points.yaml, which then says
    source: measured; a slot angle in the sheet must match the point's stage_nozzle_deg / holder_nozzle_deg."""
    points = load_config().points["points"]
    checked = 0
    for r in _rows():
        target = r["写回哪里（qumus-lab）"]
        m = XYZ_RE.match(r["采用值"].strip())
        if not (m and target.startswith("points.yaml ")):
            continue
        dev, _, name = target.split()[1].partition(".")
        pt = points[dev][name]
        assert [float(v) for v in m.groups()] == [float(v) for v in pt["xyz"]], r["编号"]
        assert pt.get("source") == "measured", r["编号"]
        deg = r["喷嘴角度（度，槽位属性）"].strip()
        key = "holder_nozzle_deg" if "holder_nozzle_deg" in target else "stage_nozzle_deg"
        assert (float(deg) if deg else None) == (None if pt.get(key) is None else float(pt[key])), r["编号"]
        checked += 1
    named_measured = [f"{d}.{n}" for d, pts in points.items() for n, p in pts.items()
                      if p.get("source") == "measured" and "xyz" in p]
    assert checked == len(named_measured), named_measured


@pytest.mark.skipif(not v1_tree_present(), reason="v1 source tree not present")
def test_v1_citations_point_at_existing_lines():
    for r in _rows():
        m = CITE_RE.match(r["v1 出处（文件:行）"])
        if not m:
            continue
        lines = (V1_ROOT / m.group(1)).read_text(encoding="utf-8", errors="replace").splitlines()
        assert int(m.group(2)) <= len(lines) and lines[int(m.group(2)) - 1].strip(), r["编号"]
