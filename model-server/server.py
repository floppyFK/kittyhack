"""Local Kittyhack model server"""

from __future__ import annotations

import hashlib
import html
import logging
import shutil
import sqlite3
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from config import ServerSettings, load_settings
from train import Cancelled, TrainRequest, execute_training, normalize_pretrained, resolve_image_size, validate_training_zip

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

log = logging.getLogger("kittyhack.model_server")

_ACTIVE = ("pending", "training")
_RETENTION_SECONDS = 7 * 24 * 3600
_INVALID_ZIP = (
    "Invalid ZIP structure. The archive must contain classes.txt, images/, and labels/."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class JobStore:
    """SQLite job rows. Methods take the store lock."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_token TEXT,
                    result_id TEXT,
                    delete_token TEXT,
                    status TEXT,
                    model_name TEXT,
                    username TEXT,
                    yolo_variant TEXT,
                    image_size INTEGER,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    deleted INTEGER DEFAULT 0,
                    error TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

    def has_active(self) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE status IN ('pending', 'training') LIMIT 1"
            ).fetchone()
        return row is not None

    def insert(
        self,
        *,
        job_id: str,
        job_token: str,
        model_name: str,
        username: str,
        yolo_variant: str,
        image_size: int,
    ) -> None:
        stamp = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, job_token, status, model_name, username,
                    yolo_variant, image_size, deleted, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?, 0, ?, ?)
                """,
                (job_id, job_token, model_name, username, yolo_variant, image_size, stamp, stamp),
            )

    def get(self, job_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_by_result(self, result_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE result_id = ?", (result_id,)).fetchone()
        return dict(row) if row else None

    def transition(self, job_id: str, status: str, *, only_from: tuple[str, ...], error: str = "") -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None or row["status"] not in only_from:
                return False
            conn.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (status, error, _now(), job_id),
            )
        return True

    def complete(self, job_id: str, *, result_id: str, delete_token: str, sha256: str, size_bytes: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None or row["status"] != "training":
                return False
            conn.execute(
                """
                UPDATE jobs
                SET status = 'completed', result_id = ?, delete_token = ?,
                    sha256 = ?, size_bytes = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (result_id, delete_token, sha256, size_bytes, _now(), job_id),
            )
        return True

    def mark_deleted(self, job_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET deleted = 1, updated_at = ? WHERE job_id = ?",
                (_now(), job_id),
            )

    def abort_stale(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'aborted', error = 'interrupted', updated_at = ?
                WHERE status IN ('pending', 'training')
                """,
                (_now(),),
            )

    def completed_jobs(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE status = 'completed'").fetchall()
        return [dict(row) for row in rows]

    def recent_jobs(self, limit: int = 8) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, model_name, yolo_variant, image_size, created_at, updated_at
                FROM jobs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class DownloadAck(BaseModel):
    delete_token: str
    sha256: str = ""
    size_bytes: int | None = None


def _parse_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid id") from None


_JOB_STATUS_LABELS = {
    "pending": "Waiting to start",
    "training": "Training",
    "completed": "Finished",
    "cancelled": "Cancelled",
    "aborted": "Stopped",
}

_PHASE_LABELS = {
    "preparing": "Preparing the dataset",
    "training": "Training",
    "exporting": "Exporting the NCNN model",
    "packaging": "Packaging the model",
}

_LOG_LIMIT = 80
_planned_device_cache: dict[str, str] = {}


class TrainingProgress:
    """Live epoch, device, and log lines for the status page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = False
        self.phase = ""
        self.epoch = 0
        self.epochs = 0
        self.device = ""
        self.lines: list[str] = []

    def begin(self) -> None:
        with self._lock:
            self.active = True
            self.phase = "preparing"
            self.epoch = 0
            self.epochs = 0
            self.device = ""
            self.lines = []

    def finish(self) -> None:
        with self._lock:
            self.active = False
            self.phase = ""

    def update(
        self,
        *,
        phase: str | None = None,
        epoch: int | None = None,
        epochs: int | None = None,
        device: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            if phase:
                self.phase = phase
            if epoch is not None:
                self.epoch = epoch
            if epochs is not None:
                self.epochs = epochs
            if device:
                self.device = device
            if message:
                self._append(message)

    def add_log_line(self, line: str) -> None:
        text = (line or "").rstrip()
        if not text:
            return
        with self._lock:
            self._append(text)

    def _append(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > _LOG_LIMIT:
            self.lines = self.lines[-_LOG_LIMIT:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active": self.active,
                "phase": self.phase,
                "epoch": self.epoch,
                "epochs": self.epochs,
                "device": self.device,
                "lines": list(self.lines),
            }


class _ProgressLogHandler(logging.Handler):
    """Copy training log records onto the status page."""

    def __init__(self, progress: TrainingProgress) -> None:
        super().__init__(level=logging.INFO)
        self.progress = progress
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.progress.add_log_line(self.format(record))
        except Exception:
            return


def _planned_device(settings: ServerSettings) -> str:
    """CPU or GPU the next job will use, from the config and this PyTorch build."""
    configured = (settings.device or "auto").strip().lower()
    if configured == "cpu":
        return "CPU"
    cached = _planned_device_cache.get(configured)
    if cached:
        return cached
    label = "CPU"
    try:
        import torch

        if configured.startswith("cuda") or (configured == "auto" and torch.cuda.is_available()):
            index = 0
            if ":" in configured:
                index = int(configured.split(":", 1)[1] or "0")
            name = ""
            try:
                name = str(torch.cuda.get_device_name(index) or "")
            except Exception:
                name = ""
            label = f"GPU ({name})" if name else "GPU"
    except Exception:
        if configured.startswith("cuda"):
            label = "GPU"
    _planned_device_cache[configured] = label
    return label


def _status_page(
    settings: ServerSettings,
    jobs: list[dict],
    progress: dict | None = None,
    device_text: str = "",
) -> str:
    """Small HTML page for a browser opened on the model server port."""
    progress = progress or {}
    active = next((job for job in jobs if job.get("status") in _ACTIVE), None)
    if active is None:
        state = "Idle"
        summary = "No training is running."
    else:
        state = _JOB_STATUS_LABELS.get(active.get("status") or "", "Busy")
        name = (active.get("model_name") or "").strip() or "Unnamed model"
        phase = _PHASE_LABELS.get(progress.get("phase") or "", "")
        summary = f"{name} — {phase}" if phase else name

    def row(label: str, value: object) -> str:
        return (
            "<tr><th>"
            + html.escape(label)
            + "</th><td>"
            + html.escape(str(value))
            + "</td></tr>"
        )

    settings_rows = "".join(
        [
            row("Epochs", settings.epochs),
            row("Image size", settings.imgsz),
            row("Batch", settings.batch),
            row("Patience", settings.patience),
            row("Workers", settings.workers),
            row("Device setting", settings.device),
            row("Port", settings.port),
        ]
    )
    running_device = (progress.get("device") or "").strip() or device_text
    device_line = ""
    if running_device:
        device_line = f"<p class='quiet'>Runs on {html.escape(running_device)}.</p>"
    epoch = int(progress.get("epoch") or 0)
    epochs = int(progress.get("epochs") or 0)
    progress_line = ""
    if epoch and epochs:
        progress_line = f"<p class='state'>Epoch {epoch} of {epochs}</p>"
    log_lines = progress.get("lines") or []
    if log_lines:
        log_block = (
            "<h2>Training log</h2><pre id='log' class='log'>"
            + html.escape("\n".join(str(line) for line in log_lines))
            + "</pre><script>var el=document.getElementById('log');"
            "if(el){el.scrollTop=el.scrollHeight;}</script>"
        )
    else:
        log_block = ""
    if jobs:
        job_rows = "".join(
            "<tr><td>"
            + html.escape(_JOB_STATUS_LABELS.get(job.get("status") or "", job.get("status") or ""))
            + "</td><td>"
            + html.escape((job.get("model_name") or "").strip() or "—")
            + "</td><td>"
            + html.escape(str(job.get("yolo_variant") or ""))
            + "</td><td>"
            + html.escape(str(job.get("image_size") or ""))
            + "</td><td>"
            + html.escape(str(job.get("updated_at") or ""))
            + "</td></tr>"
            for job in jobs
        )
        history = (
            "<h2>Recent jobs</h2>"
            "<table><thead><tr><th>Status</th><th>Model</th><th>Variant</th>"
            "<th>Image size</th><th>Updated</th></tr></thead><tbody>"
            + job_rows
            + "</tbody></table>"
        )
    else:
        history = "<p class='quiet'>No jobs yet.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>Kittyhack model server</title>
<style>
  body {{ margin: 0; font: 16px/1.45 system-ui, sans-serif; background: #f4f5f7; color: #1c1e21; }}
  main {{ max-width: 46rem; margin: 2.5rem auto; padding: 0 1rem 2rem; }}
  pre.log {{ max-height: 22rem; overflow: auto; background: #1c1e21; color: #f4f5f7; padding: 0.75rem; border-radius: 6px; font: 12px/1.45 ui-monospace, monospace; white-space: pre-wrap; word-break: break-word; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 0.25rem; }}
  h2 {{ font-size: 1rem; margin: 1.5rem 0 0.5rem; }}
  .state {{ font-size: 1.15rem; font-weight: 650; margin: 0.75rem 0 0.15rem; }}
  .quiet {{ color: #5c6370; margin: 0; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #e4e6eb; font-weight: 400; }}
  th {{ color: #5c6370; width: 9rem; }}
  thead th {{ font-weight: 650; color: #1c1e21; }}
</style>
</head>
<body>
<main>
  <h1>Kittyhack model server</h1>
  <p class="quiet">This computer trains models for Kittyhack. This page reloads every 5 seconds.</p>
  <p class="state">{html.escape(state)}</p>
  <p class="quiet">{html.escape(summary)}</p>
  {progress_line}
  {device_line}
  {log_block}
  <h2>Training settings</h2>
  <table>{settings_rows}</table>
  {history}
</main>
</body>
</html>
"""


def create_app(root: Path, trainer=None, settings: ServerSettings | None = None) -> FastAPI:
    """Build the API. `trainer` replaces real YOLO training in tests."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    uploads = root / "uploads"
    work_root = root / "work"
    models = root / "models"
    for directory in (uploads, work_root, models):
        directory.mkdir(parents=True, exist_ok=True)

    if settings is None:
        settings = load_settings(root / "config.ini")
    store = JobStore(root / "jobs.db")
    cancel_event = threading.Event()
    gate = threading.Lock()
    threads: list[threading.Thread] = []
    progress = TrainingProgress()
    run_training = trainer or execute_training

    def _cleanup_job_files(job_id: str, *, keep_model: bool) -> None:
        upload = uploads / f"{job_id}.zip"
        upload.unlink(missing_ok=True)
        work_dir = work_root / job_id
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        if not keep_model:
            (models / f"{job_id}.zip").unlink(missing_ok=True)

    def _worker(request: TrainRequest) -> None:
        job_id = request.job_id
        keep_model = False
        progress.begin()
        handler = _ProgressLogHandler(progress)
        watched = (
            logging.getLogger("kittyhack.model_server"),
            logging.getLogger("ultralytics"),
        )
        for watched_log in watched:
            watched_log.addHandler(handler)
        try:
            if cancel_event.is_set():
                store.transition(job_id, "cancelled", only_from=_ACTIVE)
                return
            if not store.transition(job_id, "training", only_from=("pending",)):
                return
            if cancel_event.is_set():
                store.transition(job_id, "cancelled", only_from=_ACTIVE)
                return
            run_training(request)
            if cancel_event.is_set():
                store.transition(job_id, "cancelled", only_from=_ACTIVE)
                return
            if not request.model_zip.is_file():
                store.transition(
                    job_id, "aborted", only_from=_ACTIVE, error="model package missing"
                )
                return
            completed = store.complete(
                job_id,
                result_id=str(uuid.uuid4()),
                delete_token=str(uuid.uuid4()),
                sha256=_sha256(request.model_zip),
                size_bytes=request.model_zip.stat().st_size,
            )
            keep_model = completed
            if not completed:
                store.transition(job_id, "cancelled", only_from=_ACTIVE)
        except Cancelled:
            store.transition(job_id, "cancelled", only_from=_ACTIVE)
        except Exception as exc:
            log.exception("Training job %s failed", job_id)
            store.transition(job_id, "aborted", only_from=_ACTIVE, error=str(exc))
        finally:
            for watched_log in watched:
                watched_log.removeHandler(handler)
            progress.finish()
            _cleanup_job_files(job_id, keep_model=keep_model)

    def _purge_old_models() -> None:
        import time
        cutoff = time.time() - _RETENTION_SECONDS
        for row in store.completed_jobs():
            if row["deleted"]:
                continue
            path = models / f"{row['job_id']}.zip"
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    store.mark_deleted(row["job_id"])
            except OSError as exc:
                log.warning("Could not delete old model %s: %s", path, exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.abort_stale()
        _purge_old_models()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root = root
    app.state.settings = settings
    app.state.store = store
    app.state.cancel_event = cancel_event
    app.state.gate = gate
    app.state.threads = threads
    app.state.progress = progress

    @app.get("/server_status")
    def server_status():
        return {
            "maintenance": False,
            "message": "",
            "min_client_version": "",
            "unauthenticated_until": "",
        }

    @app.get("/", response_class=HTMLResponse)
    def status_page():
        return HTMLResponse(
            _status_page(
                settings,
                store.recent_jobs(),
                progress.snapshot(),
                _planned_device(settings),
            )
        )

    @app.post("/upload")
    async def upload(
        file: UploadFile = File(...),
        username: str = Form(""),
        email: str = Form(""),
        model_name: str = Form(""),
        yolo_model_variant: str = Form("n"),
        pretrained_model: str = Form(""),
        image_size: str = Form(""),
        imgsz: str = Form(""),
    ):
        del email  # The local server does not send mail.
        job_id = str(uuid.uuid4())
        upload_path = uploads / f"{job_id}.zip"
        try:
            with upload_path.open("wb") as handle:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        except Exception as exc:
            upload_path.unlink(missing_ok=True)
            log.warning("Upload failed: %s", exc)
            raise HTTPException(status_code=400, detail=_INVALID_ZIP) from exc

        error = validate_training_zip(upload_path)
        if error:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=error)

        pretrained = normalize_pretrained(yolo_model_variant, pretrained_model)
        resolved_size = resolve_image_size(image_size, imgsz, settings.imgsz)
        job_token = str(uuid.uuid4())
        request = TrainRequest(
            job_id=job_id,
            upload_zip=upload_path,
            work_dir=work_root / job_id,
            model_zip=models / f"{job_id}.zip",
            model_name=(model_name or "").strip(),
            username=(username or "").strip(),
            pretrained=pretrained,
            image_size=resolved_size,
            settings=settings,
            cancel_event=cancel_event,
            progress=progress,
        )

        with gate:
            threads[:] = [thread for thread in threads if thread.is_alive()]
            if threads or store.has_active():
                upload_path.unlink(missing_ok=True)
                raise HTTPException(status_code=409, detail="A training job is already running.")
            cancel_event.clear()
            store.insert(
                job_id=job_id,
                job_token=job_token,
                model_name=request.model_name,
                username=request.username,
                yolo_variant=pretrained,
                image_size=resolved_size,
            )
            thread = threading.Thread(target=_worker, args=(request,), name=f"train-{job_id}", daemon=True)
            threads.append(thread)
            thread.start()

        return {
            "message": "File uploaded",
            "job_id": job_id,
            "job_token": job_token,
            "yolo_model_variant": pretrained,
            "image_size": resolved_size,
        }

    @app.get("/status/{job_id}")
    def status(job_id: str):
        job_id = _parse_uuid(job_id)
        row = store.get(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"job_id": job_id, "status": row["status"], "result_id": row["result_id"]}

    @app.post("/cancel/{job_id}")
    def cancel(job_id: str):
        job_id = _parse_uuid(job_id)
        row = store.get(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if row["status"] not in _ACTIVE:
            raise HTTPException(status_code=400, detail="Job cannot be cancelled")
        cancel_event.set()
        store.transition(job_id, "cancelled", only_from=_ACTIVE)
        return {"message": "Job cancelled", "job_id": job_id}

    @app.get("/download/{result_id}")
    def download(result_id: str):
        result_id = _parse_uuid(result_id)
        row = store.get_by_result(result_id)
        if row is None or row["status"] != "completed" or row["deleted"]:
            raise HTTPException(status_code=404, detail="Result not found")
        path = models / f"{row['job_id']}.zip"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Result not found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=f"{row['job_id']}.zip",
            headers={
                "X-Model-SHA256": row["sha256"] or "",
                "X-Model-Size": str(row["size_bytes"] or path.stat().st_size),
                "X-Delete-Token": row["delete_token"] or "",
                "X-Job-Id": row["job_id"],
            },
        )

    @app.post("/download/{result_id}/ack")
    def acknowledge(result_id: str, body: DownloadAck):
        result_id = _parse_uuid(result_id)
        row = store.get_by_result(result_id)
        if row is None or row["status"] != "completed":
            raise HTTPException(status_code=404, detail="Result not found")
        if (body.delete_token or "") != (row["delete_token"] or ""):
            raise HTTPException(status_code=403, detail="Invalid delete token")
        if row["deleted"]:
            return {"message": "already_deleted"}
        if body.sha256 and body.sha256.lower() != (row["sha256"] or "").lower():
            raise HTTPException(status_code=409, detail="checksum mismatch")
        if body.size_bytes is not None and row["size_bytes"] is not None and int(body.size_bytes) != int(row["size_bytes"]):
            raise HTTPException(status_code=409, detail="size mismatch")
        path = models / f"{row['job_id']}.zip"
        if not path.is_file():
            store.mark_deleted(row["job_id"])
            return {"message": "file_missing_marked_deleted"}
        path.unlink()
        store.mark_deleted(row["job_id"])
        return {"message": "deleted"}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parent
    settings = load_settings(root / "config.ini")
    app = create_app(root, settings=settings)
    import uvicorn

    log.info("Listening on 0.0.0.0:%s", settings.port)
    uvicorn.run(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
