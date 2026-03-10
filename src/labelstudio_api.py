"""Label Studio API integration for Kittyhack.

This module uses token-based authentication because current Label Studio API
endpoints are designed for API keys/tokens, not username/password login.
"""

import io
import logging
import os
import shutil
import tempfile
import time
import zipfile
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

# Import CONFIG to read token from config.ini
from src.baseconfig import CONFIG

class LabelStudioAPI:
    """Client for Label Studio API interactions."""
    
    # Default Label Studio instance on localhost
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8080
    DEFAULT_TOKEN_ENV_VARS = (
        "KITTYHACK_LABELSTUDIO_API_TOKEN",
        "LABEL_STUDIO_API_KEY",
    )
    DEFAULT_TOKEN_ENV_FILES = (
        "/etc/default/labelstudio",
        "/etc/default/kittyhack",
    )
    
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 10):
        """
        Initialize Label Studio API client.
        
        Args:
            host: Label Studio server host
            port: Label Studio server port
            timeout: Request timeout in seconds
        """
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.token: Optional[str] = None
        self.auth_scheme = "Token"
        self._access_token: Optional[str] = None
        self._access_token_expires: float = 0.0
        self.session = requests.Session()
    
    @staticmethod
    def _read_env_file(path: str) -> Dict[str, str]:
        values: Dict[str, str] = {}
        try:
            if not os.path.exists(path):
                return values
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if (not line) or line.startswith("#") or ("=" not in line):
                        continue
                    k, v = line.split("=", 1)
                    key = (k or "").strip()
                    val = (v or "").strip().strip('"').strip("'")
                    if key:
                        values[key] = val
        except Exception as e:
            logging.debug(f"[LABELSTUDIO] Failed reading env file '{path}': {e}")
        return values

    @classmethod
    def _resolve_token(cls, explicit_token: Optional[str] = None) -> Optional[str]:
        """Resolve API token from multiple sources in priority order:
        1. Explicit token parameter
        2. CONFIG["LABELSTUDIO_API_TOKEN"] (from config.ini)
        3. Environment variables (KITTYHACK_LABELSTUDIO_API_TOKEN, LABEL_STUDIO_API_KEY)
        4. Environment files (/etc/default/labelstudio, /etc/default/kittyhack)
        """
        if explicit_token and explicit_token.strip():
            return explicit_token.strip()

        # Check config.ini
        try:
            config_token = CONFIG.get("LABELSTUDIO_API_TOKEN", "").strip()
            if config_token:
                return config_token
        except Exception as e:
            logging.debug(f"[LABELSTUDIO] Failed to read token from CONFIG: {e}")

        for env_key in cls.DEFAULT_TOKEN_ENV_VARS:
            value = os.environ.get(env_key, "").strip()
            if value:
                return value

        for env_file in cls.DEFAULT_TOKEN_ENV_FILES:
            env_values = cls._read_env_file(env_file)
            for env_key in cls.DEFAULT_TOKEN_ENV_VARS:
                value = (env_values.get(env_key) or "").strip()
                if value:
                    return value

        return None

    def _auth_headers(self, scheme: Optional[str] = None) -> Dict[str, str]:
        """Build Authorization header.

        For PATs the short-lived access token (obtained via
        ``/api/token/refresh``) is used automatically.  It is refreshed
        transparently when it is about to expire.
        """
        use_scheme = (scheme or self.auth_scheme or "Token").strip()
        if use_scheme == "Bearer" and self.token:
            access = self._get_or_refresh_access_token()
            if access:
                return {"Authorization": f"Bearer {access}"}
        return {"Authorization": f"{use_scheme} {self.token}"}

    @staticmethod
    def _detect_token_type(token: str) -> str:
        """Detect whether a token is a JWT (Personal Access Token) or a Legacy Token.

        JWTs consist of three base64url segments separated by dots.
        Legacy tokens are typically hex strings.
        """
        if token and token.count(".") == 2 and token.startswith("eyJ"):
            return "Bearer"
        return "Token"

    # ------------------------------------------------------------------
    # PAT → short-lived access token exchange
    # ------------------------------------------------------------------

    def _get_or_refresh_access_token(self) -> Optional[str]:
        """Return a valid short-lived access token for a PAT.

        Personal Access Tokens are JWT *refresh* tokens.  They must be
        exchanged via ``POST /api/token/refresh`` for a short-lived access
        token (~5 min TTL) which is then used in the ``Authorization: Bearer``
        header.

        The access token is cached and refreshed automatically when it is
        about to expire (with a 30-second safety margin).
        """
        if self._access_token and time.time() < (self._access_token_expires - 30):
            return self._access_token

        refresh_url = f"{self.base_url}/api/token/refresh"
        try:
            resp = self.session.post(
                refresh_url,
                json={"refresh": self.token},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                access = data.get("access", "").strip()
                if access:
                    self._access_token = access
                    # Decode the exp claim if possible; otherwise default to 4 min
                    self._access_token_expires = self._extract_jwt_exp(access)
                    logging.debug("[LABELSTUDIO] Obtained short-lived access token via /api/token/refresh")
                    return self._access_token
            logging.warning(
                f"[LABELSTUDIO] /api/token/refresh returned HTTP {resp.status_code}"
            )
        except Exception as e:
            logging.warning(f"[LABELSTUDIO] Failed to refresh access token: {e}")

        return None

    @staticmethod
    def _extract_jwt_exp(token: str) -> float:
        """Best-effort extraction of the ``exp`` claim from a JWT.

        Falls back to *now + 4 minutes* if decoding fails.
        """
        import base64
        import json as _json

        try:
            payload_b64 = token.split(".")[1]
            # Add padding
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(padded))
            exp = payload.get("exp")
            if exp and isinstance(exp, (int, float)):
                return float(exp)
        except Exception:
            pass
        return time.time() + 240  # 4-minute fallback

    def authenticate(self, token: Optional[str] = None) -> bool:
        """Authenticate against Label Studio using an API token.

        Supports both:
        - **Legacy Tokens** – simple hex hash, used with ``Token`` scheme.
        - **Personal Access Tokens (PAT)** – JWT *refresh* token that is first
          exchanged for a short-lived access token via
          ``POST /api/token/refresh``, then used with ``Bearer`` scheme.

        The token format is auto-detected so the correct scheme is tried first.
        """
        self.token = self._resolve_token(token)
        self._access_token = None
        self._access_token_expires = 0.0

        if not self.token:
            logging.error(
                "[LABELSTUDIO] Missing API token. Please configure it in the KittyHack web UI "
                "(AI Training section) or set KITTYHACK_LABELSTUDIO_API_TOKEN environment variable."
            )
            return False

        # Auto-detect the likely scheme and try it first, then fall back.
        primary = self._detect_token_type(self.token)
        schemes = [primary, "Bearer" if primary == "Token" else "Token"]

        whoami_url = f"{self.base_url}/api/current-user/whoami"
        last_status = None
        try:
            for scheme in schemes:
                logging.debug(f"[LABELSTUDIO] Trying auth scheme '{scheme}' …")
                response = self.session.get(
                    whoami_url,
                    headers=self._auth_headers(scheme),
                    timeout=self.timeout,
                )
                last_status = response.status_code
                if response.status_code == 200:
                    self.auth_scheme = scheme
                    token_type = "Legacy Token" if scheme == "Token" else "Personal Access Token"
                    logging.info(f"[LABELSTUDIO] Authenticated successfully using {token_type}")
                    return True
                logging.debug(f"[LABELSTUDIO] Scheme '{scheme}' returned HTTP {response.status_code}")

            if last_status == 401:
                logging.error(
                    "[LABELSTUDIO] Invalid or expired API token (HTTP 401). "
                    "Please check your token in Label Studio → Account & Settings."
                )
            else:
                logging.warning(
                    f"[LABELSTUDIO] Token validation failed (status={last_status})"
                )
            return False
        except Exception as e:
            logging.error(f"[LABELSTUDIO] Authentication error: {e}")
            return False
    
    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return bool(self.token)
    
    def get_projects(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get list of all projects.
        
        Returns:
            List of projects on success, None on error
        """
        if not self.is_authenticated():
            logging.error("[LABELSTUDIO] Not authenticated")
            return None
        
        try:
            projects_url = f"{self.base_url}/api/projects/"
            
            response = self.session.get(
                projects_url,
                headers=self._auth_headers(),
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                data = response.json()
                projects = data.get("results", []) if isinstance(data, dict) else data
                logging.info(f"[LABELSTUDIO] Retrieved {len(projects)} projects")
                return projects
            else:
                logging.warning(f"[LABELSTUDIO] Failed to get projects: {response.status_code}")
                return None
                
        except Exception as e:
            logging.error(f"[LABELSTUDIO] Error getting projects: {e}")
            return None
    
    def export_project_as_yolo(self, project_id: int, output_path: str) -> bool:
        """Export a project as YOLO format with images and save to *output_path*.

        Downloads YOLO labels from Label Studio, then fetches every task image
        individually via the ``/data/`` proxy and packages everything into a
        single ZIP.
        """
        if not self.is_authenticated():
            logging.error("[LABELSTUDIO] Not authenticated")
            return False

        logging.info(f"[LABELSTUDIO] Starting YOLO export for project {project_id} …")

        # 1) Get YOLO labels
        labels_zip_bytes = self._download_yolo_labels(project_id)
        if labels_zip_bytes is None:
            logging.error("[LABELSTUDIO] Could not download YOLO labels")
            return False

        # 2) Get all tasks with their image references
        tasks = self._get_all_tasks(project_id)
        if not tasks:
            logging.error("[LABELSTUDIO] No tasks found for project")
            return False

        # 3) Build a new ZIP with labels + downloaded images
        tmp_dir = tempfile.mkdtemp(prefix="ls_yolo_")
        try:
            ok = self._assemble_yolo_zip(labels_zip_bytes, tasks, output_path, tmp_dir)
        except Exception as e:
            logging.error(f"[LABELSTUDIO] YOLO export failed: {e}")
            ok = False
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not ok:
            logging.error(
                "[LABELSTUDIO] Could not export project as YOLO with images. "
                "Verify the project contains annotated tasks with images."
            )
        return ok

    def _download_yolo_labels(self, project_id: int) -> Optional[bytes]:
        """Download YOLO-format labels only (without images).

        Tries the deprecated sync endpoint first, then falls back to the
        async snapshot → convert → download flow.
        """
        # Try deprecated sync endpoint first (simpler)
        export_url = f"{self.base_url}/api/projects/{project_id}/export"
        resp = self.session.get(
            export_url,
            headers=self._auth_headers(),
            params={"exportType": "YOLO"},
            timeout=max(self.timeout, 60),
        )
        if resp.status_code == 200 and resp.content:
            logging.info("[LABELSTUDIO] Downloaded YOLO labels via sync endpoint")
            return resp.content

        # Fallback: async snapshot → convert → download
        exports_url = f"{self.base_url}/api/projects/{project_id}/exports/"
        create_resp = self.session.post(
            exports_url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json={},
            timeout=self.timeout,
        )
        if create_resp.status_code not in (200, 201):
            logging.warning("[LABELSTUDIO] Could not create snapshot for labels export")
            return None

        export_pk = (create_resp.json() or {}).get("id")
        if not export_pk:
            return None

        status_url = f"{self.base_url}/api/projects/{project_id}/exports/{export_pk}"
        if not self._poll_export_status(status_url, "labels export"):
            return None

        convert_url = f"{self.base_url}/api/projects/{project_id}/exports/{export_pk}/convert"
        self.session.post(
            convert_url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json={"export_type": "YOLO"},
            timeout=self.timeout,
        )
        self._poll_conversion_status(status_url)

        download_url = f"{self.base_url}/api/projects/{project_id}/exports/{export_pk}/download"
        dl_resp = self.session.get(
            download_url,
            headers=self._auth_headers(),
            params={"exportType": "YOLO"},
            timeout=max(self.timeout, 60),
        )
        if dl_resp.status_code == 200 and dl_resp.content:
            logging.info("[LABELSTUDIO] Downloaded YOLO labels via async snapshot")
            return dl_resp.content
        return None

    def _get_all_tasks(self, project_id: int) -> List[Dict[str, Any]]:
        """Fetch all tasks for a project (paginated)."""
        tasks: List[Dict[str, Any]] = []
        page = 1
        page_size = 100
        while True:
            resp = self.session.get(
                f"{self.base_url}/api/tasks",
                headers=self._auth_headers(),
                params={"project": project_id, "page": page, "page_size": page_size},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logging.warning(f"[LABELSTUDIO] Tasks API returned HTTP {resp.status_code}")
                break
            body = resp.json() if resp.text else {}
            chunk = body if isinstance(body, list) else body.get("tasks", body.get("results", []))
            if not chunk:
                break
            tasks.extend(chunk)
            if isinstance(body, dict) and body.get("next"):
                page += 1
            else:
                break
        logging.debug(f"[LABELSTUDIO] Fetched {len(tasks)} tasks")
        return tasks

    def _resolve_image_url(self, task: Dict[str, Any]) -> Optional[str]:
        """Extract the image URL from a task's ``data`` dict."""
        data = task.get("data") or {}
        val = ""
        # Try common field names
        for key in ("image", "img", "url", "photo", "picture"):
            val = data.get(key, "")
            if val:
                break
        if not val:
            # Use first string value that looks like an image/path
            for v in data.values():
                if isinstance(v, str) and ("/" in v or v.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")
                )):
                    val = v
                    break
        if not val:
            return None
        if val.startswith("/"):
            return f"{self.base_url}{val}"
        if val.startswith("http://") or val.startswith("https://"):
            return val
        return None

    def _download_image(self, url: str) -> Optional[bytes]:
        """Download an image via the Label Studio proxy (authenticated)."""
        try:
            resp = self.session.get(
                url,
                headers=self._auth_headers(),
                timeout=max(self.timeout, 30),
            )
            if resp.status_code == 200 and resp.content:
                ct = resp.headers.get("content-type", "")
                if "image" in ct or "octet-stream" in ct or len(resp.content) > 1024:
                    return resp.content
            logging.debug(f"[LABELSTUDIO] Image download failed: HTTP {resp.status_code} for {url}")
        except Exception as e:
            logging.debug(f"[LABELSTUDIO] Image download error for {url}: {e}")
        return None

    def _assemble_yolo_zip(
        self,
        labels_zip_bytes: bytes,
        tasks: List[Dict[str, Any]],
        output_path: str,
        tmp_dir: str,
    ) -> bool:
        """Combine YOLO labels ZIP with individually downloaded images."""
        labels_zip = zipfile.ZipFile(io.BytesIO(labels_zip_bytes), "r")
        label_files = [n for n in labels_zip.namelist() if n.lower().endswith(".txt")
                       and os.path.basename(n).lower() != "classes.txt"]

        images_dir = os.path.join(tmp_dir, "images")
        labels_dir = os.path.join(tmp_dir, "labels")
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(labels_dir, exist_ok=True)

        # Extract all label files
        for entry_name in label_files:
            target = os.path.join(labels_dir, os.path.basename(entry_name))
            with open(target, "wb") as f:
                f.write(labels_zip.read(entry_name))

        # Also extract classes.txt and notes.json if present
        for name in labels_zip.namelist():
            basename = os.path.basename(name)
            if basename in ("classes.txt", "notes.json"):
                target = os.path.join(tmp_dir, basename)
                with open(target, "wb") as f:
                    f.write(labels_zip.read(name))

        labels_zip.close()

        # Download images for each task
        downloaded = 0
        skipped = 0
        for task in tasks:
            img_url = self._resolve_image_url(task)
            if not img_url:
                skipped += 1
                continue
            img_data = self._download_image(img_url)
            if not img_data:
                skipped += 1
                continue
            parsed = urlparse(img_url)
            img_basename = os.path.basename(parsed.path) or f"task_{task.get('id', 'unknown')}.jpg"
            img_basename = img_basename.replace("%20", "_")
            target = os.path.join(images_dir, img_basename)
            with open(target, "wb") as f:
                f.write(img_data)
            downloaded += 1

        if downloaded == 0:
            logging.warning("[LABELSTUDIO] No images could be downloaded")
            return False

        # Package everything into the output ZIP
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(tmp_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(abs_path, tmp_dir)
                    zf.write(abs_path, arc_name)

        logging.info(
            f"[LABELSTUDIO] YOLO export assembled: {len(label_files)} labels, "
            f"{downloaded} images downloaded, {skipped} skipped → {output_path}"
        )
        return True

    # ------------------------------------------------------------------
    # Polling helpers (used for label download fallback)
    # ------------------------------------------------------------------

    def _poll_export_status(self, status_url: str, label: str = "export") -> bool:
        """Poll an export snapshot until completed or failed (max 120 s)."""
        deadline = time.time() + 120
        while time.time() < deadline:
            resp = self.session.get(status_url, headers=self._auth_headers(), timeout=self.timeout)
            if resp.status_code != 200:
                return False
            status = str((resp.json() or {}).get("status", "")).strip().lower()
            if status == "completed":
                return True
            if status == "failed":
                logging.warning(f"[LABELSTUDIO] {label} failed")
                return False
            time.sleep(2)
        logging.warning(f"[LABELSTUDIO] Timed out waiting for {label}")
        return False

    def _poll_conversion_status(
        self, status_url: str, converted_format_id: Optional[int] = None
    ) -> bool:
        """Poll until a YOLO conversion is completed (max 300 s)."""
        deadline = time.time() + 300
        cf_status: Optional[str] = None
        while time.time() < deadline:
            resp = self.session.get(status_url, headers=self._auth_headers(), timeout=self.timeout)
            if resp.status_code != 200:
                return False
            export_data = resp.json() if resp.text else {}
            for cf in (export_data.get("converted_formats") or []):
                if (converted_format_id and cf.get("id") == converted_format_id) or \
                   cf.get("export_type") == "YOLO":
                    cf_status = str(cf.get("status", "")).strip().lower()
                    break
            if cf_status == "completed":
                return True
            if cf_status == "failed":
                logging.warning("[LABELSTUDIO] YOLO conversion failed")
                return False
            time.sleep(2)
        logging.warning("[LABELSTUDIO] Timed out waiting for YOLO conversion")
        return False
    
    def get_project_details(self, project_id: int) -> Optional[Dict[str, Any]]:
        """
        Get details of a specific project.
        
        Args:
            project_id: Project ID
            
        Returns:
            Project details on success, None on error
        """
        if not self.is_authenticated():
            logging.error("[LABELSTUDIO] Not authenticated")
            return None
        
        try:
            project_url = f"{self.base_url}/api/projects/{project_id}"
            
            response = self.session.get(
                project_url,
                headers=self._auth_headers(),
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                logging.warning(f"[LABELSTUDIO] Failed to get project details: {response.status_code}")
                return None
                
        except Exception as e:
            logging.error(f"[LABELSTUDIO] Error getting project details: {e}")
            return None
    
    @staticmethod
    def is_labelstudio_available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: int = 5) -> bool:
        """
        Check if Label Studio is available at the given host:port.
        
        Args:
            host: Label Studio server host
            port: Label Studio server port
            timeout: Request timeout in seconds
            
        Returns:
            True if Label Studio is available, False otherwise
        """
        try:
            # /api/version is generally available without auth and is a stable probe.
            url = f"http://{host}:{port}/api/version"
            response = requests.get(url, timeout=timeout)
            return response.status_code in (200, 401, 403)
        except Exception:
            return False
    
    def close(self):
        """Close the session."""
        if self.session:
            self.session.close()


def get_labelstudio_projects_list(
    host: str = LabelStudioAPI.DEFAULT_HOST,
    port: int = LabelStudioAPI.DEFAULT_PORT,
    token: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Helper function to get list of Label Studio projects.
    Handles authentication automatically.
    
    Args:
        host: Label Studio server host
        port: Label Studio server port
        
    Returns:
        List of projects or None on error
    """
    try:
        if not LabelStudioAPI.is_labelstudio_available(host, port):
            logging.warning("[LABELSTUDIO] Label Studio is not available")
            return None
        
        api = LabelStudioAPI(host=host, port=port)
        if not api.authenticate(token=token):
            logging.error("[LABELSTUDIO] Failed to authenticate with Label Studio")
            return None
        
        projects = api.get_projects()
        api.close()
        return projects
        
    except Exception as e:
        logging.error(f"[LABELSTUDIO] Error getting projects list: {e}")
        return None


def export_labelstudio_project_as_zip(
    project_id: int,
    output_path: str,
    host: str = LabelStudioAPI.DEFAULT_HOST,
    port: int = LabelStudioAPI.DEFAULT_PORT,
    token: Optional[str] = None,
) -> bool:
    """
    Helper function to export a Label Studio project as YOLO format ZIP.
    
    Args:
        project_id: Project ID to export
        output_path: Path where to save the ZIP file
        host: Label Studio server host
        port: Label Studio server port
        
    Returns:
        True if successful, False otherwise
    """
    try:
        if not LabelStudioAPI.is_labelstudio_available(host, port):
            logging.warning("[LABELSTUDIO] Label Studio is not available")
            return False
        
        api = LabelStudioAPI(host=host, port=port)
        if not api.authenticate(token=token):
            logging.error("[LABELSTUDIO] Failed to authenticate with Label Studio")
            return False
        
        success = api.export_project_as_yolo(project_id, output_path)
        api.close()
        return success
        
    except Exception as e:
        logging.error(f"[LABELSTUDIO] Error exporting project: {e}")
        return False


def get_labelstudio_project_task_summary(
    project_id: int,
    host: str = LabelStudioAPI.DEFAULT_HOST,
    port: int = LabelStudioAPI.DEFAULT_PORT,
    token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return task statistics for a Label Studio project.

    Returns a dict with keys:
    - ``total_tasks``: total number of tasks
    - ``annotated_tasks``: number of tasks with at least one annotation
    - ``unannotated_tasks``: tasks without annotations
    - ``ready``: True if all tasks are annotated and total > 0
    """
    try:
        if not LabelStudioAPI.is_labelstudio_available(host, port):
            return None
        api = LabelStudioAPI(host=host, port=port)
        if not api.authenticate(token=token):
            return None
        details = api.get_project_details(project_id)
        api.close()
        if details is None:
            return None

        total = int(details.get("task_number", 0))
        annotated = int(details.get("num_tasks_with_annotations", 0))
        return {
            "total_tasks": total,
            "annotated_tasks": annotated,
            "unannotated_tasks": total - annotated,
            "ready": total > 0 and annotated == total,
        }
    except Exception as e:
        logging.error(f"[LABELSTUDIO] Error getting project task summary: {e}")
        return None
