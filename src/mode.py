"""Deployment mode detection: target (on Kittyflap) vs remote UI host."""

import os

from src.paths import kittyhack_root


def remote_mode_marker_path() -> str:
    """Path to the `.remote-mode` marker file under the repo root."""
    return os.path.join(kittyhack_root(), ".remote-mode")


def is_remote_mode() -> bool:
    """True when UI/AI run off-device (env override or `.remote-mode` marker)."""
    env = os.environ.get("KITTYHACK_MODE", "").strip().lower()
    if env in {"remote", "remote-mode"}:
        return True
    if env in {"target", "target-mode"}:
        return False

    return os.path.exists(remote_mode_marker_path())
