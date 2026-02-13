import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime
from typing import Any

import requests

from src.paths import models_yolo_root


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _merge_write_json(path: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Read existing JSON state, merge updates, and write atomically.

    Important: This preserves fields written by the main process (e.g. training_job_id,
    finalized) while the worker updates progress/status.
    """
    current = _read_json(path)
    if not isinstance(current, dict):
        current = {}
    current.update(updates)
    _atomic_write_json(path, current)
    return current


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _sanitize_directory_name(name: str) -> str:
    name = (name or "").lower()
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    sanitized = re.sub(r"_{2,}", "_", sanitized)
    sanitized = sanitized.strip().strip(".")
    if not sanitized:
        sanitized = "model_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    return sanitized


def _determine_model_name_from_info(zip_path: str) -> tuple[str | None, str]:
    """Return (model_name_from_info, creation_date_fallback)."""
    creation_date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "info.json" in zf.namelist():
                with zf.open("info.json") as info_file:
                    info_data = json.load(info_file)
                    model_name = info_data.get("MODEL_NAME")
                    ts = info_data.get("TIMESTAMP_UTC")
                    if isinstance(ts, str) and ts:
                        try:
                            timestamp = datetime.fromisoformat(ts)
                            creation_date = timestamp.strftime("%Y-%m-%d_%H:%M:%S")
                        except Exception:
                            pass
                    return model_name, creation_date
    except Exception:
        pass
    return None, creation_date


def download_and_extract(*, base_url: str, result_id: str, model_name: str, token: str | None, state_path: str) -> None:
    started_at = time.time()

    state = {
        "status": "downloading",
        "result_id": result_id,
        "model_name": model_name or "",
        "bytes_downloaded": 0,
        "total_bytes": 0,
        "started_at": started_at,
        "finished_at": 0.0,
        "error": "",
        "target_dir": "",
        "pid": os.getpid(),
    }
    _merge_write_json(state_path, state)

    logging.info("[MODEL_DL_WORKER] Start download for result_id=%s", result_id)

    tmp_zip_path = os.path.join("/tmp", f"kittyhack_model_{result_id}.zip")
    try:
        if os.path.exists(tmp_zip_path):
            os.remove(tmp_zip_path)
    except Exception:
        pass

    url = f"{base_url}/download/{result_id}"
    headers: dict[str, str] = {}
    if token:
        headers["token"] = token

    try:
        resp = requests.get(url, headers=headers, stream=True, verify=True, timeout=(5, 60))
        if resp.status_code == 404:
            raise FileNotFoundError(f"Result {result_id} not found")
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", "0") or "0")
        state["total_bytes"] = total
        _merge_write_json(state_path, state)

        with open(tmp_zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                f.write(chunk)
                state["bytes_downloaded"] += len(chunk)
                _merge_write_json(state_path, state)

        # Extract
        state["status"] = "extracting"
        _merge_write_json(state_path, state)

        logging.info("[MODEL_DL_WORKER] Download complete (%s bytes). Extracting...", state.get("bytes_downloaded"))

        info_name, creation_date = _determine_model_name_from_info(tmp_zip_path)
        final_name = model_name or info_name or creation_date

        base_dir = models_yolo_root()
        os.makedirs(base_dir, exist_ok=True)

        dir_name = _sanitize_directory_name(final_name)
        target_dir = os.path.join(base_dir, dir_name)
        unique_dir = target_dir
        count = 1
        while os.path.exists(unique_dir):
            unique_dir = f"{target_dir}_{count}"
            count += 1
        target_dir = unique_dir

        os.makedirs(target_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_zip_path) as zf:
            zf.extractall(target_dir)

        required_files = [
            "model.pt",
            "labels.txt",
            "info.json",
            "best_ncnn_model/model.ncnn.bin",
            "best_ncnn_model/model.ncnn.param",
        ]
        missing_files = [f for f in required_files if not os.path.exists(os.path.join(target_dir, f))]
        if missing_files:
            raise RuntimeError(f"missing_files: {', '.join(missing_files)}")

        state["status"] = "done"
        state["target_dir"] = target_dir
        state["finished_at"] = time.time()
        _merge_write_json(state_path, state)

        logging.info("[MODEL_DL_WORKER] Done. Extracted to %s", target_dir)

    except Exception as e:
        # best-effort cleanup
        try:
            target_dir = state.get("target_dir")
            if target_dir and os.path.isdir(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:
            pass

        state["status"] = "error"
        state["error"] = str(e)
        state["finished_at"] = time.time()
        _merge_write_json(state_path, state)

        logging.exception("[MODEL_DL_WORKER] Failed")
        raise
    finally:
        try:
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)
        except Exception:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-id", required=True)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    token = args.token or None
    try:
        download_and_extract(
            base_url=args.base_url,
            result_id=args.result_id,
            model_name=args.model_name,
            token=token,
            state_path=args.state_path,
        )
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
