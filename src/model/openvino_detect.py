"""Torch-free YOLOv8 OpenVINO inference (cv2 + numpy + openvino).

Ultralytics loads the exported IR through PyTorch. Intel GPU inference only
needs the OpenVINO runtime. Preprocess and NMS match ``ncnn_detect``.
"""
from __future__ import annotations

import logging
import os

import numpy as np

from .detection import build_detection_payload
from .ncnn_detect import (
    _CONF_THRES,
    _IOU_THRES,
    decode_yolo_ncnn,
    letterbox_bgr,
    nms_xyxy,
    read_ncnn_meta,
    scale_xyxy_to_original,
)


def openvino_device_name(inference_device: str, available_devices: list[str]) -> str:
    """Map a Kittyhack device string to an OpenVINO device, or AUTO."""
    device = str(inference_device or "").strip().lower()
    if device in {"gpu", "intel:gpu"}:
        wanted = "GPU"
    elif device == "intel:npu":
        wanted = "NPU"
    elif device in {"cpu", "intel:cpu"}:
        wanted = "CPU"
    else:
        wanted = "AUTO"
    if wanted == "AUTO":
        return "AUTO"
    available = [str(item) for item in available_devices]
    if any(item == wanted or item.startswith(wanted + ".") for item in available):
        return wanted
    logging.warning(
        f"[MODEL] OpenVINO device '{wanted}' is not available ({available or 'none'}). Using AUTO."
    )
    return "AUTO"


def _find_openvino_xml(model_dir: str) -> str:
    if os.path.isfile(model_dir) and model_dir.endswith(".xml"):
        return model_dir
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"OpenVINO model path not found: {model_dir}")
    xmls = sorted(
        os.path.join(model_dir, name)
        for name in os.listdir(model_dir)
        if name.endswith(".xml")
    )
    if not xmls:
        raise FileNotFoundError(
            "OpenVINO model is missing (.xml). Export it once with "
            "tools/export_openvino.py after installing requirements_openvino_export.txt "
            f"(CPU PyTorch wheels). Expected the IR in {model_dir}"
        )
    return xmls[0]


class OpenVinoYoloDetector:
    """Load an Ultralytics-exported OpenVINO detect model and run one-frame inference."""

    def __init__(self, model_dir: str, inference_device: str = "intel:gpu", imgsz: int = 320):
        import openvino as ov

        xml_path = _find_openvino_xml(model_dir)
        bin_path = os.path.splitext(xml_path)[0] + ".bin"
        if not os.path.isfile(bin_path):
            raise FileNotFoundError(f"OpenVINO .bin missing next to {xml_path}")

        meta_dir = os.path.dirname(xml_path) or model_dir
        self.imgsz, self.end2end = read_ncnn_meta(meta_dir, imgsz)
        core = ov.Core()
        device_name = openvino_device_name(inference_device, list(core.available_devices))
        ov_model = core.read_model(model=xml_path, weights=bin_path)
        try:
            if ov_model.inputs and ov_model.inputs[0].get_partial_shape().rank.is_static:
                layout = ov.Layout("NCHW")
                if ov_model.inputs[0].get_layout().empty:
                    ov_model.inputs[0].set_layout(layout)
        except Exception:
            pass
        self.compiled = core.compile_model(
            ov_model,
            device_name=device_name,
            config={"PERFORMANCE_HINT": "LATENCY"},
        )
        self._device_name = device_name
        logging.info(
            f"[MODEL] OpenVINO native detector loaded {xml_path} "
            f"(imgsz={self.imgsz}, device={device_name}, end2end={self.end2end}, torch bypassed)"
        )

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
                f"[MODEL] Ignoring request imgsz={input_size}; OpenVINO graph is fixed at {imgsz}"
            )

        letterboxed, ratio, pad = letterbox_bgr(frame, imgsz)
        rgb = np.ascontiguousarray(letterboxed[:, :, ::-1])
        blob = np.transpose(rgb, (2, 0, 1))[None].astype(np.float32) / 255.0
        result = self.compiled(blob)
        output = np.array(result[self.compiled.output(0)])

        num_classes = max(1, len(labels) if labels else 1)
        xyxy, scores, classes = decode_yolo_ncnn(
            output, num_classes, imgsz, _CONF_THRES, end2end=self.end2end
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
