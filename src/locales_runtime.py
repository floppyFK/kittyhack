import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone

try:
    import fcntl
except Exception:
    fcntl = None

from src.paths import kittyhack_root

_REQUIRED_TOOLS = ("xgettext", "msgmerge", "msgfmt")
# English strings are the source msgids. We only maintain a translated catalog for German.
_LANGUAGES = ("de",)
_STATE_FILE = ".runtime_locale_state.json"

_once_lock = threading.Lock()
_once_ran = False


def _lock_file_path(root: str) -> str:
    # Use a root-dependent lock file in /tmp so concurrent kittyhack processes
    # (server + control) cannot refresh locales at the same time.
    lock_id = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"kittyhack_i18n_{lock_id}.lock")


def _acquire_process_lock(root: str):
    lock_file = open(_lock_file_path(root), "w", encoding="utf-8")
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            logging.warning(f"[I18N] Failed to acquire locale refresh lock: {e}")
    return lock_file


def _release_process_lock(lock_file) -> None:
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            lock_file.close()
        except Exception:
            pass


def _run_command(command: list[str], timeout: float, cwd: str | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except Exception as e:
        return False, str(e)

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        return False, output
    return True, output


def _missing_tools() -> list[str]:
    return [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]


def _ensure_gettext_installed() -> bool:
    missing = _missing_tools()
    if not missing:
        return True

    apt_get = shutil.which("apt-get")
    if not apt_get:
        logging.warning("[I18N] gettext tools are missing and apt-get is not available.")
        return False

    install_prefix: list[str] = []
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            logging.warning("[I18N] gettext tools are missing and sudo is not available.")
            return False
        install_prefix = [sudo]

    logging.warning(
        f"[I18N] Missing gettext tools ({', '.join(missing)}). Attempting to install package 'gettext'."
    )

    update_ok, update_out = _run_command([*install_prefix, apt_get, "update"], timeout=300)
    if not update_ok:
        logging.warning(f"[I18N] Failed to run apt-get update for gettext install: {update_out}")
        return False

    install_ok, install_out = _run_command(
        [*install_prefix, apt_get, "install", "-y", "gettext"],
        timeout=900,
    )
    if not install_ok:
        logging.warning(f"[I18N] Failed to install gettext package: {install_out}")
        return False

    missing_after = _missing_tools()
    if missing_after:
        logging.warning(f"[I18N] gettext install finished but tools are still missing: {', '.join(missing_after)}")
        return False

    logging.info("[I18N] gettext package installed successfully.")
    return True


def _collect_source_files(root: str) -> list[str]:
    source_files: set[str] = set()

    app_entry = os.path.join(root, "app.py")
    if os.path.isfile(app_entry):
        source_files.add(os.path.normpath(app_entry))

    for path in glob.glob(os.path.join(root, "src", "**", "*.py"), recursive=True):
        if os.path.isfile(path):
            source_files.add(os.path.normpath(path))

    return sorted(source_files)


def _collect_po_sources(locales_root: str) -> list[str]:
    return [
        os.path.join(locales_root, language, "LC_MESSAGES", "messages.po")
        for language in _LANGUAGES
    ]


def _collect_mo_outputs(locales_root: str) -> list[str]:
    return [
        os.path.join(locales_root, language, "LC_MESSAGES", "messages.mo")
        for language in _LANGUAGES
    ]


def _compute_input_hash(root: str, source_files: list[str], po_paths: list[str]) -> str:
    # Include source files and tracked PO files as canonical inputs.
    entries = ["schema=2"]

    for path in sorted(set(source_files + po_paths)):
        rel = os.path.relpath(path, root)
        if not os.path.exists(path):
            entries.append(f"{rel}|missing")
            continue
        stat = os.stat(path)
        entries.append(f"{rel}|{stat.st_size}|{stat.st_mtime_ns}")

    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return digest


def _read_state_hash(state_path: str) -> str | None:
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        value = payload.get("input_hash")
        return value if isinstance(value, str) else None
    except Exception:
        return None


def _write_state_hash(state_path: str, input_hash: str) -> None:
    payload = {
        "input_hash": input_hash,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _build_pot_file(pot_path: str, source_files: list[str]) -> bool:
    if not source_files:
        logging.warning("[I18N] No Python source files found for gettext extraction.")
        return False

    command = ["xgettext", "-d", "messages", "-o", pot_path, "--from-code", "UTF-8", *source_files]
    success, output = _run_command(command, timeout=180)
    if not success:
        logging.warning(f"[I18N] Failed to build POT file: {output}")
        return False
    return True


def _refresh_language_catalog(language: str, locales_root: str, pot_path: str, temp_dir: str) -> bool:
    lang_dir = os.path.join(locales_root, language, "LC_MESSAGES")
    os.makedirs(lang_dir, exist_ok=True)

    po_path = os.path.join(lang_dir, "messages.po")
    mo_path = os.path.join(lang_dir, "messages.mo")
    merged_po_path = os.path.join(temp_dir, f"messages.{language}.merged.po")

    if not os.path.exists(po_path):
        logging.warning(f"[I18N] Missing tracked PO source file: {po_path}")
        return False

    # Merge into a temporary PO to avoid mutating tracked source files at runtime.
    merge_cmd = [
        "msgmerge",
        "--quiet",
        "--output-file",
        merged_po_path,
        po_path,
        pot_path,
    ]
    merged, merge_out = _run_command(merge_cmd, timeout=120)
    if not merged:
        logging.warning(f"[I18N] Failed to merge PO file ({po_path}): {merge_out}")
        return False

    compile_cmd = ["msgfmt", "-o", mo_path, merged_po_path]
    compiled, compile_out = _run_command(compile_cmd, timeout=120)
    if not compiled:
        logging.warning(f"[I18N] Failed to compile MO file ({mo_path}): {compile_out}")
        return False

    return True


def refresh_locales_if_needed(force: bool = False) -> bool:
    root = kittyhack_root()
    locales_root = os.path.join(root, "locales")
    state_path = os.path.join(locales_root, _STATE_FILE)

    lock_file = _acquire_process_lock(root)
    try:
        source_files = _collect_source_files(root)
        po_paths = _collect_po_sources(locales_root)
        mo_paths = _collect_mo_outputs(locales_root)

        input_hash = _compute_input_hash(root, source_files, po_paths)
        existing_hash = _read_state_hash(state_path)

        generated_files = mo_paths

        if not force and existing_hash == input_hash and all(os.path.exists(path) for path in generated_files):
            return True

        if not _ensure_gettext_installed():
            return False

        missing = _missing_tools()
        if missing:
            logging.warning(f"[I18N] gettext tools are unavailable: {', '.join(missing)}")
            return False

        os.makedirs(locales_root, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="kittyhack_i18n_") as temp_dir:
            pot_path = os.path.join(temp_dir, "messages.pot")
            if not _build_pot_file(pot_path, source_files):
                return False

            for language in _LANGUAGES:
                if not _refresh_language_catalog(language, locales_root, pot_path, temp_dir):
                    return False

        final_input_hash = _compute_input_hash(root, source_files, po_paths)

        try:
            _write_state_hash(state_path, final_input_hash)
        except Exception as e:
            logging.warning(f"[I18N] Failed to write locale state file: {e}")

        logging.info("[I18N] Locale catalogs refreshed from tracked PO sources.")
        return True
    finally:
        _release_process_lock(lock_file)


def ensure_runtime_locales_ready(force: bool = False) -> bool:
    global _once_ran

    if force:
        return refresh_locales_if_needed(force=True)

    with _once_lock:
        if _once_ran:
            return True
        _once_ran = True

    return refresh_locales_if_needed(force=False)
