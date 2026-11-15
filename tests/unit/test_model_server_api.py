"""Unit tests for ``src.model.model_server_api``."""

from src.model.model_server_api import (
    HEADER_CLIENT,
    HEADER_JOB_TOKEN,
    HEADER_VERSION,
    KITTYHACK_CLIENT_TOKEN,
    model_server_headers,
)


def test_model_server_headers_includes_client_and_version():
    headers = model_server_headers(version="3.0.0")
    assert HEADER_CLIENT in headers
    assert HEADER_VERSION in headers
    assert headers[HEADER_CLIENT] == KITTYHACK_CLIENT_TOKEN
    assert headers[HEADER_VERSION] == "3.0.0"
    assert HEADER_JOB_TOKEN not in headers


def test_model_server_headers_job_token():
    headers = model_server_headers(job_token="job-secret", version="3.1.0")
    assert headers[HEADER_JOB_TOKEN] == "job-secret"


def test_kittyhack_client_token_non_empty():
    assert KITTYHACK_CLIENT_TOKEN
    assert isinstance(KITTYHACK_CLIENT_TOKEN, str)
