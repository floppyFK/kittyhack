"""Small JSON / process helpers shared by YoloModel and RemoteModelTrainer."""
import json
import os
from typing import Any


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Write JSON atomically via a sibling `.tmp` file and `os.replace`."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path: str) -> dict[str, Any]:
    """Load a JSON object from disk; return `{}` if missing or invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    """Return True if `pid` refers to a living process (signal 0 probe)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _default_download_state() -> dict[str, Any]:
    """Return the idle default dict for model-download worker state."""
    return {
        "status": "idle",  # idle|downloading|extracting|done|error
        "training_job_id": "",
        "result_id": "",
        "model_name": "",
        "bytes_downloaded": 0,
        "total_bytes": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "error": "",
        "target_dir": "",
        "pid": 0,
        "finalized": False,
    }
