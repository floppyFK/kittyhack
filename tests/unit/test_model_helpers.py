"""Coverage for model JSON helpers and YOLO detection parsing (no torch)."""

from types import SimpleNamespace

import numpy as np
import pytest

from src.model.detection import _parse_yolo_detection_results
from src.model.json_util import (
    _atomic_write_json,
    _default_download_state,
    _pid_alive,
    _read_json,
)


def test_atomic_write_and_read_json(tmp_path):
    path = tmp_path / "state.json"
    _atomic_write_json(str(path), {"a": 1})
    assert _read_json(str(path)) == {"a": 1}
    assert _read_json(str(tmp_path / "missing.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert _read_json(str(bad)) == {}


def test_pid_alive_and_default_state():
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False
    assert _pid_alive(1) in (True, False)  # host-dependent
    state = _default_download_state()
    assert state["status"] == "idle"
    assert state["pid"] == 0


class _Boxes:
    def __init__(self, xyxyn, conf, cls):
        self.xyxyn = xyxyn
        self.conf = conf
        self.cls = cls

    def __len__(self):
        return len(self.conf)


def test_parse_yolo_detection_results_prey_and_cat():
    boxes = _Boxes(
        xyxyn=[np.array([0.1, 0.2, 0.3, 0.4])],
        conf=[np.float32(0.9)],
        cls=[np.int32(0)],
    )
    results = [SimpleNamespace(boxes=boxes)]
    mouse, cat, objs = _parse_yolo_detection_results(
        results,
        labels=["prey", "mia"],
        cat_names=["mia"],
        min_threshold=50.0,
    )
    assert mouse in (89, 90)  # np.float32(0.9)*100 truncates
    assert cat == 0
    assert len(objs) == 1
    assert objs[0]["name"] == "prey"
    assert objs[0]["probability"] == pytest.approx(90.0, abs=0.1)

    boxes2 = _Boxes(
        xyxyn=[np.array([0.0, 0.0, 0.5, 0.5])],
        conf=[np.float32(0.8)],
        cls=[np.int32(1)],
    )
    mouse, cat, objs = _parse_yolo_detection_results(
        [SimpleNamespace(boxes=boxes2)],
        labels=["prey", "mia"],
        cat_names=["mia"],
        min_threshold=50.0,
    )
    assert mouse == 0
    assert cat in (79, 80)


def test_parse_yolo_empty_results():
    mouse, cat, objs = _parse_yolo_detection_results(
        [SimpleNamespace(boxes=None)],
        labels=["prey"],
        cat_names=[],
        min_threshold=30.0,
    )
    assert mouse == 0 and cat == 0 and objs == []
