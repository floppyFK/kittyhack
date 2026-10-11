#!/usr/bin/env python3
"""Export a YOLO ``model.pt`` to OpenVINO IR.

This is a one-shot tool. The Kittyhack service venv does not include PyTorch.
Install the CPU export stack first (not the CUDA wheels):

    .venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu \\
        --extra-index-url https://pypi.org/simple \\
        -r requirements_openvino_export.txt
    .venv/bin/python tools/export_openvino.py /path/to/model.pt --imgsz 480
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _model_pt(path: Path) -> Path:
    if path.is_file() and path.suffix == ".pt":
        return path
    if path.is_dir():
        candidate = path / "model.pt"
        if candidate.is_file():
            return candidate
    raise SystemExit(f"model.pt not found at {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export model.pt to OpenVINO IR")
    parser.add_argument("model", help="Path to model.pt or the model directory that contains it")
    parser.add_argument("--imgsz", type=int, default=480, help="Export image size (square)")
    parser.add_argument(
        "--format",
        choices=("openvino", "ncnn"),
        default="openvino",
        help="Export format",
    )
    args = parser.parse_args(argv)

    pt_path = _model_pt(Path(args.model).expanduser().resolve())
    try:
        from ultralytics import YOLO
    except ImportError:
        raise SystemExit(
            "ultralytics is not installed. Install requirements_openvino_export.txt "
            "with the PyTorch CPU index, then run this tool again."
        )

    model = YOLO(str(pt_path), task="detect", verbose=False)
    exported = model.export(format=args.format, imgsz=int(args.imgsz))
    print(exported)
    return 0


if __name__ == "__main__":
    sys.exit(main())
