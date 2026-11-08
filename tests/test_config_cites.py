"""Every `source: v1` value in config cites a v1 file:line; check that line really contains the value."""

from __future__ import annotations

import pytest

from qumus_lab.config import load_config

from .v1_harness import V1_ROOT, v1_tree_present


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).startswith("cite") and isinstance(v, dict) and "at" in v:
                yield f"{path}.{k}", v
            else:
                yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _all_cites():
    cfg = load_config()
    out = []
    for name, tree in (("devices", cfg.devices), ("points", cfg.points), ("limits", cfg.limits)):
        out.extend((f"{name}{p}", c) for p, c in _walk(tree))
    return out


CITES = _all_cites()


def test_there_are_many_cites():
    assert len(CITES) > 60


@pytest.mark.skipif(not v1_tree_present(), reason="v1 source tree not present")
@pytest.mark.parametrize("where,cite", CITES, ids=[w for w, _ in CITES])
def test_cite_points_at_v1_line(where, cite):
    rel, line = cite["at"].rsplit(":", 1)
    lines = (V1_ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    assert cite["check"] in lines[int(line) - 1], f"{where}: {cite['at']} is {lines[int(line) - 1]!r}"


def test_every_device_and_point_has_a_source():
    cfg = load_config()
    for mod in cfg.module_names:
        for dev_id, dcfg in cfg.module(mod)["devices"].items():
            assert dcfg.get("source") in ("v1", "lab", "measured"), dev_id
    for dev, pts in cfg.points["points"].items():
        for name, pt in pts.items():
            assert pt.get("source") in ("v1", "measured"), f"{dev}.{name}"
