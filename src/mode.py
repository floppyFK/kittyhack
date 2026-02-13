import os

from src.paths import kittyhack_root


def remote_mode_marker_path() -> str:
    # Marker file: if it exists, kittyhack runs in remote-mode.
    return os.path.join(kittyhack_root(), ".remote-mode")


def is_remote_mode() -> bool:
    env = os.environ.get("KITTYHACK_MODE", "").strip().lower()
    if env in {"remote", "remote-mode"}:
        return True
    if env in {"target", "target-mode"}:
        return False

    return os.path.exists(remote_mode_marker_path())
