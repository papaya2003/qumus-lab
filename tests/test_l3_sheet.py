"""docs/l3-measurements.csv stays in step with config/ and the v1 lines it cites."""

from __future__ import annotations

import csv
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
    assert named <= keys  # every named point has a row
    for k in keys:
        dev, _, name = k.partition(".")
        assert dev in points and (not name or name in points[dev]), k


def test_measured_columns_start_empty_and_ids_are_unique():
    rows = _rows()
    assert len({r["编号"] for r in rows}) == len(rows)
    for r in rows:
        assert not any(r[c] for c in ("测量 1", "测量 2", "测量 3", "采用值", "测量人", "日期")), r["编号"]


@pytest.mark.skipif(not v1_tree_present(), reason="v1 source tree not present")
def test_v1_citations_point_at_existing_lines():
    for r in _rows():
        m = CITE_RE.match(r["v1 出处（文件:行）"])
        if not m:
            continue
        lines = (V1_ROOT / m.group(1)).read_text(encoding="utf-8", errors="replace").splitlines()
        assert int(m.group(2)) <= len(lines) and lines[int(m.group(2)) - 1].strip(), r["编号"]
