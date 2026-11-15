"""A failed device/model switch on the configuration page must not keep the new model."""

from __future__ import annotations

from src.server_ui.helpers import restore_model_selection, snapshot_model_selection


def test_failed_switch_restores_previous_model_and_device():
    config = {
        "YOLO_MODEL": "old-model",
        "TFLITE_MODEL_VERSION": "",
        "INFERENCE_DEVICE": "intel:gpu",
        "CAT_THRESHOLD": 80.0,
    }
    snapshot = snapshot_model_selection(config)

    # The save handler writes the form values into CONFIG before it exports the model.
    config["YOLO_MODEL"] = "new-model-without-ir"
    config["TFLITE_MODEL_VERSION"] = ""
    config["INFERENCE_DEVICE"] = "intel:gpu"

    restore_model_selection(config, snapshot)

    assert config["YOLO_MODEL"] == "old-model"
    assert config["TFLITE_MODEL_VERSION"] == ""
    assert config["INFERENCE_DEVICE"] == "intel:gpu"
    assert config["CAT_THRESHOLD"] == 80.0


def test_restore_switches_back_from_yolo_to_tflite():
    config = {"YOLO_MODEL": "", "TFLITE_MODEL_VERSION": "v1", "INFERENCE_DEVICE": "cpu"}
    snapshot = snapshot_model_selection(config)
    config.update(YOLO_MODEL="new", TFLITE_MODEL_VERSION="", INFERENCE_DEVICE="intel:cpu")

    restore_model_selection(config, snapshot)

    assert config == {"YOLO_MODEL": "", "TFLITE_MODEL_VERSION": "v1", "INFERENCE_DEVICE": "cpu"}
