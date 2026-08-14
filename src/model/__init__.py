"""Model package — inference, YOLO metadata, and remote training.

Imports are lazy so the YOLO forkserver worker can import
``src.model.yolo_inference_worker`` / ``src.model.detection`` without pulling
in cv2/numpy (which initialize OpenMP before CPU affinity is applied).
"""

__all__ = [
    "ModelHandler",
    "RemoteModelTrainer",
    "YoloModel",
]


def __getattr__(name: str):
    if name == "ModelHandler":
        from .model_handler import ModelHandler as attr
    elif name == "RemoteModelTrainer":
        from .remote_trainer import RemoteModelTrainer as attr
    elif name == "YoloModel":
        from .yolo_model import YoloModel as attr
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = attr
    return attr
