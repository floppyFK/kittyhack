"""Prepare a YOLO dataset, train, export NCNN, and pack the result zip.

The zip Kittyhack extracts must contain model.pt, labels.txt, info.json, and
best_ncnn_model/model.ncnn.bin plus model.ncnn.param.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import ServerSettings

log = logging.getLogger("kittyhack.model_server")

_INVALID_ZIP = (
    "Invalid ZIP structure. The archive must contain classes.txt, images/, and labels/."
)
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_VARIANT_RE = re.compile(r"^yolov8([nsmxl])\.pt$")


class Cancelled(Exception):
    """The user cancelled the running training job."""


@dataclass
class TrainRequest:
    job_id: str
    upload_zip: Path
    work_dir: Path
    model_zip: Path
    model_name: str
    username: str
    pretrained: str
    image_size: int
    settings: ServerSettings
    cancel_event: object
    progress: object | None = None


def normalize_pretrained(variant: str, pretrained: str) -> str:
    """Return yolov8{n,s,m,l,x}.pt. Unknown values become yolov8n.pt."""
    raw = (variant or "").strip().lower()
    if raw in {"n", "s", "m", "l", "x"}:
        return f"yolov8{raw}.pt"
    for candidate in (pretrained, raw):
        match = _VARIANT_RE.fullmatch((candidate or "").strip().lower())
        if match:
            return f"yolov8{match.group(1)}.pt"
    return "yolov8n.pt"


def resolve_image_size(image_size: str, imgsz: str, fallback: int) -> int:
    """Use the client's image size when it is a positive multiple of 32."""
    for raw in (image_size, imgsz):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            continue
        if value > 0 and value % 32 == 0:
            return value
    return fallback


def _zip_members(path: Path) -> list[str]:
    if not zipfile.is_zipfile(path):
        raise ValueError("not a zip")
    names: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/").lstrip("/")
            if not name or name.endswith("/"):
                continue
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts):
                raise ValueError("unsafe path")
            names.append("/".join(parts))
    return names


def _dataset_prefix(names: list[str]) -> str | None:
    """Return '' for a root dataset, or the single top folder that holds one."""

    def has(prefix: str) -> bool:
        base = f"{prefix}/" if prefix else ""
        classes_name = f"{base}classes.txt"
        return (
            classes_name in names
            and any(name.startswith(f"{base}images/") for name in names)
            and any(name.startswith(f"{base}labels/") for name in names)
        )

    useful = [name for name in names if not name.startswith("__MACOSX/")]
    if has(""):
        return ""
    roots = {name.split("/", 1)[0] for name in useful if "/" in name}
    if len(roots) == 1:
        root = next(iter(roots))
        if has(root):
            return root
    return None


def validate_training_zip(path: Path) -> str | None:
    """Return an error sentence, or None when the zip can be trained."""
    try:
        names = _zip_members(path)
    except Exception:
        return _INVALID_ZIP
    if _dataset_prefix(names) is None:
        return _INVALID_ZIP
    return None


def safe_extract(archive_path: Path, dest: Path) -> None:
    """Extract a training zip, rejecting paths that escape `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/").lstrip("/")
            if not name:
                continue
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts):
                raise ValueError(f"unsafe path: {info.filename}")
            target = (root.joinpath(*parts)).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe path: {info.filename}")
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    _hoist_single_root(root)


def _hoist_single_root(work_dir: Path) -> None:
    if (work_dir / "classes.txt").is_file():
        return
    entries = [path for path in work_dir.iterdir() if path.name != "__MACOSX"]
    if len(entries) != 1 or not entries[0].is_dir():
        return
    nested = entries[0]
    if not (nested / "classes.txt").is_file():
        return
    for child in nested.iterdir():
        shutil.move(str(child), str(work_dir / child.name))
    nested.rmdir()


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()


def split_dataset(work_dir: Path) -> None:
    """Copy images into train/ and val/. One image is used for both."""
    image_dir = work_dir / "images"
    label_dir = work_dir / "labels"
    if not image_dir.is_dir() or not label_dir.is_dir():
        raise RuntimeError("images/ or labels/ is missing after unpacking the zip")

    images = [
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
    ]
    if not images:
        raise RuntimeError("no jpg or png images found in images/")

    random.shuffle(images)
    if len(images) == 1:
        train_images = images
        val_images = images
    else:
        split_at = int(len(images) * 0.8)
        split_at = min(max(split_at, 1), len(images) - 1)
        train_images = images[:split_at]
        val_images = images[split_at:]

    for bucket, chosen in (("train", train_images), ("val", val_images)):
        (work_dir / bucket / "images").mkdir(parents=True, exist_ok=True)
        (work_dir / bucket / "labels").mkdir(parents=True, exist_ok=True)
        for image in chosen:
            shutil.copy2(image, work_dir / bucket / "images" / image.name)
            label = label_dir / f"{image.stem}.txt"
            if label.is_file():
                shutil.copy2(label, work_dir / bucket / "labels" / label.name)

    log.info(
        "Split %s images into %s train and %s val",
        len(images),
        len(train_images),
        len(val_images),
    )


def _yaml_scalar(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_. -]+", value or ""):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_data_yaml(work_dir: Path, class_names: list[str]) -> Path:
    data_yaml = work_dir / "data.yaml"
    lines = [
        f"path: {work_dir}",
        "train: train",
        "val: val",
        f"nc: {len(class_names)}",
        "names:",
    ]
    lines.extend(f"  - {_yaml_scalar(name)}" for name in class_names)
    data_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return data_yaml


def _read_class_names(work_dir: Path) -> list[str]:
    classes_file = work_dir / "classes.txt"
    if not classes_file.is_file():
        raise RuntimeError("classes.txt is missing")
    names = [line.strip() for line in classes_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not names:
        raise RuntimeError("classes.txt is empty")
    return names


def describe_runtime_device(device) -> str:
    """Return ``CPU`` or ``GPU``, with the GPU name when PyTorch can provide it."""
    kind = getattr(device, "type", None)
    if kind is None:
        kind = str(device or "")
    kind = str(kind).lower()
    if "cuda" in kind or kind == "mps":
        name = ""
        try:
            import torch

            index = getattr(device, "index", None)
            if not isinstance(index, int):
                index = 0
            name = str(torch.cuda.get_device_name(index) or "")
        except Exception:
            name = ""
        return f"GPU ({name})" if name else "GPU"
    return "CPU"


def _report_progress(request: TrainRequest, **fields) -> None:
    progress = getattr(request, "progress", None)
    update = getattr(progress, "update", None)
    if update is None:
        return
    update(**fields)


def _stop_if_cancelled(trainer, cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        if hasattr(trainer, "stop"):
            trainer.stop = True
        raise Cancelled()


def _export_ncnn(best_pt: Path, dest_dir: Path) -> None:
    from ultralytics import YOLO

    exported = Path(str(YOLO(str(best_pt)).export(format="ncnn")))
    search_root = exported if exported.is_dir() else exported.parent
    param_files = sorted(search_root.rglob("*.param"))
    bin_files = sorted(search_root.rglob("*.bin"))
    if not param_files or not bin_files:
        raise RuntimeError(f"NCNN export did not produce param and bin files under {search_root}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(param_files[0], dest_dir / "model.ncnn.param")
    shutil.copy2(bin_files[0], dest_dir / "model.ncnn.bin")


def package_model(
    *,
    best_pt: Path,
    classes_file: Path,
    ncnn_dir: Path,
    model_zip: Path,
    info: dict,
) -> None:
    """Write the zip Kittyhack's downloader extracts."""
    model_zip.parent.mkdir(parents=True, exist_ok=True)
    info_path = model_zip.parent / f".{model_zip.stem}.info.json"
    info_path.write_text(json.dumps(info, indent=4), encoding="utf-8")
    try:
        with zipfile.ZipFile(model_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(best_pt, "model.pt")
            archive.write(classes_file, "labels.txt")
            archive.write(info_path, "info.json")
            archive.write(ncnn_dir / "model.ncnn.bin", "best_ncnn_model/model.ncnn.bin")
            archive.write(ncnn_dir / "model.ncnn.param", "best_ncnn_model/model.ncnn.param")
    finally:
        info_path.unlink(missing_ok=True)


def execute_training(request: TrainRequest) -> None:
    """Unpack, train, export NCNN, and write `request.model_zip`."""
    _check_cancel(request.cancel_event)
    _report_progress(request, phase="preparing", message="Preparing the dataset")
    request.work_dir.mkdir(parents=True, exist_ok=True)
    safe_extract(request.upload_zip, request.work_dir)
    _check_cancel(request.cancel_event)

    class_names = _read_class_names(request.work_dir)
    split_dataset(request.work_dir)
    data_yaml = write_data_yaml(request.work_dir, class_names)
    _check_cancel(request.cancel_event)

    from ultralytics import YOLO

    model = YOLO(request.pretrained)
    cancel_event = request.cancel_event

    def on_train_end_check(trainer):
        _stop_if_cancelled(trainer, cancel_event)

    def on_train_start(trainer):
        label = describe_runtime_device(getattr(trainer, "device", None))
        epochs = int(getattr(trainer, "epochs", 0) or request.settings.epochs)
        _report_progress(
            request,
            phase="training",
            device=label,
            epochs=epochs,
            message=f"Training on {label}",
        )

    def on_train_epoch_end(trainer):
        on_train_end_check(trainer)
        epoch = int(getattr(trainer, "epoch", 0)) + 1
        epochs = int(getattr(trainer, "epochs", 0) or request.settings.epochs)
        _report_progress(
            request,
            phase="training",
            epoch=epoch,
            epochs=epochs,
            message=f"Finished epoch {epoch} of {epochs}",
        )

    model.add_callback("on_train_start", on_train_start)
    model.add_callback("on_train_epoch_end", on_train_epoch_end)
    model.add_callback("on_train_batch_end", on_train_end_check)

    train_kwargs = {
        "data": str(data_yaml),
        "epochs": request.settings.epochs,
        "imgsz": request.image_size,
        "batch": request.settings.batch,
        "patience": request.settings.patience,
        "workers": request.settings.workers,
        "project": str(request.work_dir),
        "name": "train_results",
        "exist_ok": True,
        "plots": False,
    }
    if request.settings.device != "auto":
        train_kwargs["device"] = request.settings.device

    log.info(
        "Training %s job %s imgsz=%s epochs=%s device=%s",
        request.pretrained,
        request.job_id,
        request.image_size,
        request.settings.epochs,
        request.settings.device,
    )
    _report_progress(
        request,
        phase="training",
        epochs=request.settings.epochs,
        message="Starting training",
    )
    try:
        model.train(**train_kwargs)
    except Cancelled:
        raise
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled() from exc
        raise
    _check_cancel(cancel_event)

    best_pt = request.work_dir / "train_results" / "weights" / "best.pt"
    if not best_pt.is_file():
        found = sorted(request.work_dir.rglob("best.pt"))
        if not found:
            raise RuntimeError("training finished without best.pt")
        best_pt = found[0]

    ncnn_dir = request.work_dir / "best_ncnn_model"
    _report_progress(request, phase="exporting", message="Exporting the NCNN model")
    _export_ncnn(best_pt, ncnn_dir)
    _check_cancel(cancel_event)

    info = {
        "JOB_ID": request.job_id,
        "USERNAME": request.username,
        "MODEL_NAME": request.model_name,
        "TIMESTAMP_UTC": datetime.now(timezone.utc).isoformat(),
        "VERSION": "1.0.0",
        "MODEL_IMAGE_SIZE": str(request.image_size),
        "YOLO_MODEL_VARIANT": request.pretrained,
    }
    _report_progress(request, phase="packaging", message="Packaging the model")
    package_model(
        best_pt=best_pt,
        classes_file=request.work_dir / "classes.txt",
        ncnn_dir=ncnn_dir,
        model_zip=request.model_zip,
        info=info,
    )
    log.info("Packaged %s", request.model_zip)
