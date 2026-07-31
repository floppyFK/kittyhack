"""Process-level runtime flags (not persisted in config.ini)."""

from __future__ import annotations

import os

# Set by kittyhack_control --simulate before other imports that read the flag.
_FORCE_SIMULATE: bool | None = None

_ENV_TRUE = {"1", "true", "yes", "on"}


def set_simulate_mode(enabled: bool) -> None:
    """Force simulate mode for this process (also sets KITTYHACK_SIMULATE)."""
    global _FORCE_SIMULATE
    _FORCE_SIMULATE = bool(enabled)
    os.environ["KITTYHACK_SIMULATE"] = "1" if enabled else "0"


def is_simulate_mode() -> bool:
    """True when hardware/system actions should be simulated (no real GPIO/reboot).

    Priority: explicit ``set_simulate_mode`` / ``--simulate`` → env ``KITTYHACK_SIMULATE`` → False.
    Never reads config.ini.
    """
    if _FORCE_SIMULATE is not None:
        return bool(_FORCE_SIMULATE)
    env = os.environ.get("KITTYHACK_SIMULATE", "").strip().lower()
    return env in _ENV_TRUE
