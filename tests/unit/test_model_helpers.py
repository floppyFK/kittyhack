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


def test_letterbox_bgr_800x600_to_320():
    from src.model.ncnn_detect import letterbox_bgr

    img = np.zeros((600, 800, 3), dtype=np.uint8)
    out, ratio, pad = letterbox_bgr(img, 320)
    assert out.shape == (320, 320, 3)
    assert ratio == pytest.approx(0.4)
    # min(320/600, 320/800)=0.4 → unpad (320, 240), vertical pad 80 → 40/side
    assert pad == (0, 40)


def test_decode_yolo_ncnn_two_class_is_not_end2end():
    from src.model.ncnn_detect import decode_yolo_ncnn

    # (4+2, 3 proposals): one real box in letterbox pixels, two below threshold
    raw = np.zeros((6, 3), dtype=np.float32)
    raw[0, 0] = 160.0  # cx
    raw[1, 0] = 160.0  # cy
    raw[2, 0] = 40.0   # w
    raw[3, 0] = 40.0   # h
    raw[4, 0] = 0.9    # class 0
    raw[5, 0] = 0.1    # class 1
    raw[4, 1] = 0.05
    raw[5, 2] = 0.02

    xyxy, scores, classes = decode_yolo_ncnn(raw, num_classes=2, imgsz=320, conf_thres=0.25)
    assert len(scores) == 1
    assert classes[0] == 0
    assert scores[0] == pytest.approx(0.9)
    assert xyxy[0, 0] == pytest.approx(140.0)
    assert xyxy[0, 2] == pytest.approx(180.0)


def test_nms_and_scale_xyxy():
    from src.model.ncnn_detect import nms_xyxy, scale_xyxy_to_original

    xyxy = np.array([[10.0, 10.0, 50.0, 50.0], [12.0, 12.0, 48.0, 48.0]], dtype=np.float32)
    scores = np.array([0.9, 0.8], dtype=np.float32)
    keep = nms_xyxy(xyxy, scores, iou_thres=0.5)
    assert keep == [0]

    scaled = scale_xyxy_to_original(
        np.array([[40.0, 40.0, 80.0, 80.0]], dtype=np.float32),
        ratio=0.4,
        pad=(0, 40),
        orig_w=800,
        orig_h=600,
    )
    # x: 40/0.4=100 → 100/800=0.125; y: (40-40)/0.4=0
    assert scaled[0, 0] == pytest.approx(0.125)
    assert scaled[0, 1] == pytest.approx(0.0)


def test_read_ncnn_meta(tmp_path):
    from src.model.ncnn_detect import read_ncnn_meta

    imgsz, end2end = read_ncnn_meta(str(tmp_path), 320)
    assert imgsz == 320 and end2end is False

    (tmp_path / "metadata.yaml").write_text(
        "imgsz: [416, 416]\nargs:\n  nms: true\n",
        encoding="utf-8",
    )
    imgsz, end2end = read_ncnn_meta(str(tmp_path), 320)
    assert imgsz == 416 and end2end is True

