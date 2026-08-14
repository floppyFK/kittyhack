"""YOLO detection result parsing."""
import logging


def build_detection_payload(
    items: list[tuple[float, float, float, float, float, int]],
    labels: list[str],
    cat_names: list[str],
    min_threshold: float,
) -> tuple[int, int, list[dict]]:
    """Convert normalized xyxy detections into kittyhack object dicts.

    Each item is ``(x1, y1, x2, y2, conf, cls_idx)`` with coordinates in 0–1
    and ``conf`` in 0–1.
    """
    mouse_probability = 0
    own_cat_probability = 0
    detected_objects: list[dict] = []
    detected_info: list[str] = []
    probability_threshold_exceeded = False

    for x1, y1, x2, y2, conf, cls_idx in items:
        object_name = labels[cls_idx] if 0 <= cls_idx < len(labels) else str(cls_idx)
        probability = float(conf * 100)
        detected_info.append(f"{object_name} ({probability:.1f}%)")
        if probability >= min_threshold:
            probability_threshold_exceeded = True

        detected_objects.append({
            "x": float(x1) * 100.0,
            "y": float(y1) * 100.0,
            "w": float(x2 - x1) * 100.0,
            "h": float(y2 - y1) * 100.0,
            "name": object_name,
            "probability": probability,
        })

        if object_name.lower() in ["prey", "beute"]:
            mouse_probability = int(probability)
        elif object_name.lower() in cat_names:
            own_cat_probability = int(probability)

    if detected_info and probability_threshold_exceeded:
        logging.info(
            f"[MODEL] Detected {len(detected_info)} objects in image: "
            f"{', '.join(detected_info)} (MIN_THRESHOLD={min_threshold})"
        )

    return mouse_probability, own_cat_probability, detected_objects


def _parse_yolo_detection_results(
    results,
    labels: list[str],
    cat_names: list[str],
    min_threshold: float,
) -> tuple[int, int, list[dict]]:
    """Convert Ultralytics detection results to kittyhack object dicts (percent coords)."""
    items: list[tuple[float, float, float, float, float, int]] = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None or len(boxes) == 0:
            continue
        for box_xyxyn, conf, cls in zip(boxes.xyxyn, boxes.conf, boxes.cls):
            x1, y1, x2, y2 = (float(v) for v in box_xyxyn.tolist())
            items.append((x1, y1, x2, y2, float(conf), int(cls)))
    return build_detection_payload(items, labels, cat_names, min_threshold)
