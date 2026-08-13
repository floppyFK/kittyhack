"""Unit tests for IP-camera FFmpeg pipeline helpers (no live camera)."""

from __future__ import annotations

from types import SimpleNamespace

from src.camera import (
    build_ip_camera_ffmpeg_cmd,
    drm_render_node_supports_vaapi_decode,
    ip_camera_hw_modes_to_try,
    redact_camera_url,
    resolve_ip_camera_hw_decode,
)


def test_redact_camera_url_masks_password():
    raw = "rtsp://lordowi:super-secret@192.168.1.43:554/stream1"
    redacted = redact_camera_url(raw)
    assert "super-secret" not in redacted
    assert "192.168.1.43" in redacted
    assert ":***@" in redacted
    assert redact_camera_url(None) == ""
    # Also redact credentials embedded in FFmpeg stderr.
    err = f"{raw}: Operation not permitted"
    assert "super-secret" not in redact_camera_url(err)


def test_drm_vaapi_skips_raspberry_pi_v3d(monkeypatch):
    monkeypatch.setattr("src.camera.os.path.exists", lambda path: path == "/dev/dri/renderD128")
    monkeypatch.setattr("src.camera._drm_vaapi_identity", lambda: ("0x14e4", "v3d"))
    assert drm_render_node_supports_vaapi_decode() is False

    monkeypatch.setattr("src.camera._drm_vaapi_identity", lambda: ("0x8086", "i915"))
    assert drm_render_node_supports_vaapi_decode() is True

    monkeypatch.setattr("src.camera._drm_vaapi_identity", lambda: ("0x1002", "amdgpu"))
    assert drm_render_node_supports_vaapi_decode() is True


def _fake_ffmpeg_hwaccels_run(cmd, **_kwargs):
    if cmd[:1] == ["ffmpeg"]:
        return SimpleNamespace(
            returncode=0,
            stdout="Hardware acceleration methods:\nvaapi\nvdpau\n",
        )
    return SimpleNamespace(returncode=1, stdout="")


def test_auto_hw_decode_skips_vaapi_on_pi(monkeypatch):
    monkeypatch.setattr("src.camera.drm_render_node_supports_vaapi_decode", lambda: False)
    monkeypatch.setattr("src.camera.subprocess.run", _fake_ffmpeg_hwaccels_run)
    assert resolve_ip_camera_hw_decode("auto") == "none"
    assert ip_camera_hw_modes_to_try("auto") == ["none"]


def test_auto_hw_decode_uses_vaapi_on_intel(monkeypatch):
    monkeypatch.setattr("src.camera.drm_render_node_supports_vaapi_decode", lambda: True)
    monkeypatch.setattr("src.camera.subprocess.run", _fake_ffmpeg_hwaccels_run)
    assert resolve_ip_camera_hw_decode("auto") == "vaapi"
    assert ip_camera_hw_modes_to_try("auto") == ["vaapi", "none"]


def test_explicit_vaapi_still_tried_before_software():
    assert ip_camera_hw_modes_to_try("vaapi") == ["vaapi", "none"]
    assert ip_camera_hw_modes_to_try("none") == ["none"]


def test_software_ffmpeg_cmd_has_no_vaapi():
    cmd, label = build_ip_camera_ffmpeg_cmd(
        "rtsp://user:pass@192.168.1.43:554/stream1",
        854,
        480,
        10,
        "none",
    )
    assert label == "software"
    assert "-hwaccel" not in cmd
    assert "-rtsp_transport" in cmd
    assert "tcp" in cmd
