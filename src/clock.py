"""Central time helpers.

Use monotonic time for durations/timeouts so system clock changes (NTP sync, DST,
manual adjustments) cannot shorten or extend safety delays.

Use wall time (epoch seconds) only when you need a real timestamp for logging,
persistence, filenames, etc.
"""

from __future__ import annotations

import time as tm


def monotonic_time() -> float:
    """Return monotonic seconds (never goes backwards during a boot)."""
    return float(tm.monotonic())


def wall_time() -> float:
    """Return wall-clock seconds since epoch (can jump)."""
    return float(tm.time())


def sleep(seconds: float) -> None:
    """Sleep for seconds (wrapper for consistency/testability)."""
    tm.sleep(float(seconds))
