"""Shared constants and headers for the kittyhack-model-server API."""

from __future__ import annotations

BASE_URL = "https://kittyhack-models.fk-cloud.de"

# Shared with the model-server (KITTYHACK_CLIENT_TOKEN). Open-source on purpose:
# it only raises the bar against unauthenticated scanners, not determined attackers.
KITTYHACK_CLIENT_TOKEN = "IWbhqLFO6Tvalh3KpQJMRKYfQTke_FsF7JLXZ8jIWJI"

HEADER_CLIENT = "X-Kittyhack-Client"
HEADER_VERSION = "X-Kittyhack-Version"
HEADER_JOB_TOKEN = "X-Job-Token"


def model_server_headers(*, job_token: str | None = None, version: str = "") -> dict[str, str]:
    """Return headers sent on every model-server call from kittyhack v3+."""
    headers = {
        HEADER_CLIENT: KITTYHACK_CLIENT_TOKEN,
        HEADER_VERSION: version or "3.0.0",
    }
    if job_token:
        headers[HEADER_JOB_TOKEN] = job_token
    return headers
