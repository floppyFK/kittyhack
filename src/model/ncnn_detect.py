"""Torch-free YOLOv8 NCNN inference (cv2 + numpy + ncnn only).

Ultralytics still letterboxes and runs NMS through PyTorch even for NCNN
models. On the Kittyflap (Python 3.14 / torch 2.9 / 1 pinned core) that
preprocess+NMS path is ~4× slower than the Python 3.12 / torch 2.7 stack.
This module talks to the exported ``best_ncnn_model`` files directly.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import cv2
import numpy as np

from .detection import build_detection_payload

_CONF_THRES = 0.25  # Ultralytics predict() default
_IOU_THRES = 0.70  # Ultralytics predict() default


def letterbox_bgr(
    image: np.ndarray,
    imgsz: int,
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize/pad a BGR image to ``imgsz×imgsz`` (Ultralytics LetterBox)."""
    shape = image.shape[:2]
    r = min(imgsz / shape[0], imgsz / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw = imgsz - new_unpad[0]
    dh = imgsz - new_unpad[1]
    dw /= 2.0
    dh /= 2.0
    if shape[::-1] != new_unpad:
        image = cv2.resize(image, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    image = cv2.copyMakeBorder(
        image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114)
    )
    return image, r, (left, top)


def decode_yolo_ncnn(
    raw: np.ndarray,
    num_classes: int,
    imgsz: int,
    conf_thres: float = _CONF_THRES,
    end2end: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decode a YOLOv8 NCNN output tensor to ``(xyxy, scores, classes)`` in letterbox pixels."""
    arr = np.array(raw, dtype=np.float32, copy=False)
    arr = np.squeeze(arr)
    if arr.ndim != 2:
        raise ValueError(f"Unexpected NCNN output shape {arr.shape}")

    # Ultralytics export is typically (4+nc, num_boxes).
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T

    n_cols = arr.shape[1]
    # A 2-class YOLOv8 head is also 6 columns (xywh + 2 scores). Only treat 6-col
    # output as end-to-end (xyxy, conf, cls) when the export metadata says so,
    # or when the class count cannot explain the width.
    if n_cols == 6 and (end2end or num_classes not in (0, 2)):
        xyxy = arr[:, :4].copy()
        scores = arr[:, 4]
        classes = arr[:, 5].astype(np.int32)
        if xyxy.size and float(xyxy.max()) <= 1.5:
            xyxy *= float(imgsz)
        keep = scores >= conf_thres
        return xyxy[keep], scores[keep], classes[keep]

    if n_cols == 5 + num_classes:
        xywh = arr[:, :4]
        obj = arr[:, 4:5]
        cls_scores = arr[:, 5 : 5 + num_classes] * obj
    else:
        xywh = arr[:, :4]
        cls_end = 4 + max(1, num_classes)
        cls_scores = arr[:, 4:cls_end]

    if cls_scores.size == 0:
        return (
            np.zeros((0, 4), dtype=np.float32),
            np.zeros((0,), dtype=np.float32),
            np.zeros((0,), dtype=np.int32),
        )

    classes = cls_scores.argmax(axis=1).astype(np.int32)
    scores = cls_scores.max(axis=1)
    if xywh.size and float(xywh.max()) <= 1.5:
        xywh = xywh * float(imgsz)

    xyxy = np.empty_like(xywh)
    xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2.0
    xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2.0
    xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2.0
    xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2.0

    keep = scores >= conf_thres
    return xyxy[keep], scores[keep], classes[keep]


def nms_xyxy(
    xyxy: np.ndarray,
    scores: np.ndarray,
    iou_thres: float = _IOU_THRES,
) -> list[int]:
    """Return kept indices after IoU NMS (OpenCV)."""
    if xyxy.size == 0:
        return []
    boxes_xywh = [
        [float(x1), float(y1), float(x2 - x1), float(y2 - y1)]
        for x1, y1, x2, y2 in xyxy
    ]
    idxs = cv2.dnn.NMSBoxes(boxes_xywh, [float(s) for s in scores], 0.0, float(iou_thres))
    if idxs is None or len(idxs) == 0:
        return []
    return [int(i) for i in np.asarray(idxs).reshape(-1)]


def scale_xyxy_to_original(
    xyxy: np.ndarray,
    ratio: float,
    pad: tuple[int, int],
    orig_w: int,
    orig_h: int,
) -> np.ndarray:
    """Map letterbox-pixel xyxy boxes back to the original frame, normalized 0–1."""
    if xyxy.size == 0:
        return xyxy
    left, top = pad
    out = xyxy.astype(np.float32, copy=True)
    out[:, [0, 2]] = (out[:, [0, 2]] - left) / ratio
    out[:, [1, 3]] = (out[:, [1, 3]] - top) / ratio
    out[:, [0, 2]] = np.clip(out[:, [0, 2]], 0, orig_w)
    out[:, [1, 3]] = np.clip(out[:, [1, 3]], 0, orig_h)
    if orig_w > 0:
        out[:, [0, 2]] /= float(orig_w)
    if orig_h > 0:
        out[:, [1, 3]] /= float(orig_h)
    return out


def _find_ncnn_param(model_dir: str) -> str:
    if os.path.isfile(model_dir) and model_dir.endswith(".param"):
        return model_dir
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"NCNN model path not found: {model_dir}")
    params = sorted(
        os.path.join(model_dir, name)
        for name in os.listdir(model_dir)
        if name.endswith(".param")
    )
    if not params:
        raise FileNotFoundError(f"No .param file in NCNN model directory: {model_dir}")
    return params[0]


def read_ncnn_meta(model_dir: str, fallback_imgsz: int) -> tuple[int, bool]:
    """Return ``(imgsz, end2end)`` from metadata.yaml, else ``(fallback, False)``."""
    imgsz = int(fallback_imgsz)
    end2end = False
    meta_path = os.path.join(model_dir, "metadata.yaml") if os.path.isdir(model_dir) else ""
    if meta_path and os.path.isfile(meta_path):
        try:
            import yaml

            with open(meta_path, encoding="utf-8") as f:
                meta: dict[str, Any] = yaml.safe_load(f) or {}
            raw_imgsz = meta.get("imgsz")
            if isinstance(raw_imgsz, (list, tuple)) and raw_imgsz:
                imgsz = int(raw_imgsz[0])
            elif raw_imgsz:
                imgsz = int(raw_imgsz)
            args = meta.get("args") if isinstance(meta.get("args"), dict) else {}
            end2end = bool(meta.get("end2end") or args.get("nms"))
        except Exception as e:
            logging.warning(f"[MODEL] Failed to read NCNN metadata.yaml: {e}")
    return imgsz, end2end


def read_ncnn_imgsz(model_dir: str, fallback: int) -> int:
    """Read exported ``imgsz`` from metadata.yaml, else ``fallback``."""
    imgsz, _end2end = read_ncnn_meta(model_dir, fallback)
    return imgsz


class NcnnYoloDetector:
    """Load an Ultralytics-exported NCNN detect model and run one-frame inference."""

    def __init__(self, model_dir: str, num_threads: int = 1, imgsz: int = 320):
        import ncnn

        self._ncnn = ncnn
        n = max(1, int(num_threads) if num_threads else 1)
        if hasattr(ncnn, "set_omp_num_threads"):
            ncnn.set_omp_num_threads(n)

        param_path = _find_ncnn_param(model_dir)
        bin_path = os.path.splitext(param_path)[0] + ".bin"
        if not os.path.isfile(bin_path):
            raise FileNotFoundError(f"NCNN .bin missing next to {param_path}")

        self.imgsz, self.end2end = read_ncnn_meta(os.path.dirname(param_path) or model_dir, imgsz)
        self.net = ncnn.Net()
        self.net.opt.num_threads = n
        self.net.opt.use_vulkan_compute = False
        if self.net.load_param(param_path) != 0:
            raise RuntimeError(f"Failed to load NCNN param: {param_path}")
        if self.net.load_model(bin_path) != 0:
            raise RuntimeError(f"Failed to load NCNN bin: {bin_path}")
        self._num_threads = n
        logging.info(
            f"[MODEL] NCNN native detector loaded {param_path} "
            f"(imgsz={self.imgsz}, threads={n}, end2end={self.end2end}, torch bypassed)"
        )

    def _mat_from_letterbox(self, letterboxed_bgr: np.ndarray):
        rgb = np.ascontiguousarray(letterboxed_bgr[:, :, ::-1], dtype=np.uint8)
        h, w = rgb.shape[:2]
        pixel_type = getattr(getattr(self._ncnn.Mat, "PixelType", None), "PIXEL_RGB", 1)
        mat_in = self._ncnn.Mat.from_pixels(rgb, pixel_type, w, h)
        mat_in.substract_mean_normalize([0.0, 0.0, 0.0], [1.0 / 255.0, 1.0 / 255.0, 1.0 / 255.0])
        return mat_in

    def predict(
        self,
        frame: np.ndarray,
        labels: list[str],
        cat_names: list[str],
        min_threshold: float,
        input_size: int | None = None,
    ) -> tuple[int, int, list[dict]]:
        """Run detection on a BGR frame; return kittyhack (mouse, cat, objects)."""
        orig_h, orig_w = frame.shape[:2]
        imgsz = self.imgsz
        if input_size and int(input_size) != imgsz:
            logging.debug(
                f"[MODEL] Ignoring request imgsz={input_size}; NCNN graph is fixed at {imgsz}"
            )

        letterboxed, ratio, pad = letterbox_bgr(frame, imgsz)
        mat_in = self._mat_from_letterbox(letterboxed)

        outputs: list[np.ndarray] = []
        with self.net.create_extractor() as ex:
            if hasattr(ex, "set_num_threads"):
                try:
                    ex.set_num_threads(self._num_threads)
                except Exception:
                    pass
            ex.input(self.net.input_names()[0], mat_in)
            for name in sorted(self.net.output_names()):
                ret, mat_out = ex.extract(name)
                if ret != 0 or mat_out is None:
                    continue
                outputs.append(np.array(mat_out))

        if not outputs:
            return 0, 0, []
        if len(outputs) > 1:
            logging.debug(f"[MODEL] NCNN model has {len(outputs)} outputs; using the first")

        num_classes = max(1, len(labels) if labels else 1)
        xyxy, scores, classes = decode_yolo_ncnn(
            outputs[0], num_classes, imgsz, _CONF_THRES, end2end=self.end2end
        )
        keep = nms_xyxy(xyxy, scores, _IOU_THRES)
        if keep:
            xyxy = xyxy[keep]
            scores = scores[keep]
            classes = classes[keep]
        else:
            xyxy = np.zeros((0, 4), dtype=np.float32)
            scores = np.zeros((0,), dtype=np.float32)
            classes = np.zeros((0,), dtype=np.int32)

        xyxyn = scale_xyxy_to_original(xyxy, ratio, pad, orig_w, orig_h)
        items = [
            (float(box[0]), float(box[1]), float(box[2]), float(box[3]), float(score), int(cls))
            for box, score, cls in zip(xyxyn, scores, classes)
        ]
        return build_detection_payload(items, labels, cat_names, min_threshold)
