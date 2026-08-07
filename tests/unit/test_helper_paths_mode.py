"""Helper / paths / mode / clock / versioning unit coverage."""

from src.clock import monotonic_time, sleep, wall_time
from src.helper import EventType, Versioning, is_valid_uuid4
from src.mode import is_remote_mode, remote_mode_marker_path
from src.paths import (
    install_base,
    kittyhack_root,
    labelstudio_root,
    models_yolo_root,
    pictures_original_dir,
    pictures_root,
    pictures_thumbnails_dir,
)
from src.system import ServiceOps
from src.backend import constants


def test_clock_helpers():
    assert monotonic_time() >= 0
    assert wall_time() > 0
    sleep(0.0)


def test_paths_and_mode(monkeypatch, tmp_path):
    root = kittyhack_root()
    assert root.endswith("fk_kth") or "fk_kth" in root or "src" not in root
    monkeypatch.setenv("KITTYHACK_INSTALL_BASE", str(tmp_path))
    install_base.cache_clear()
    assert install_base() == str(tmp_path.resolve())
    assert pictures_root().startswith(str(tmp_path.resolve()))
    assert "original_images" in pictures_original_dir()
    assert "thumbnails" in pictures_thumbnails_dir()
    assert "yolo" in models_yolo_root()
    assert "labelstudio" in labelstudio_root()
    install_base.cache_clear()

    monkeypatch.setenv("KITTYHACK_MODE", "remote")
    assert is_remote_mode() is True
    monkeypatch.setenv("KITTYHACK_MODE", "target")
    assert is_remote_mode() is False
    monkeypatch.delenv("KITTYHACK_MODE", raising=False)
    assert remote_mode_marker_path().endswith(".remote-mode")


def test_versioning_normalize_and_compare():
    assert Versioning.normalize_version("v2.5.4") == "2.5.4"
    assert Versioning.normalize_version("2.5.4-abc1234") == "2.5.4"
    assert Versioning.is_same_kittyhack_version("v1", "v1") is True
    assert Versioning.is_same_kittyhack_version("abc1234", "main@abc1234") is True
    assert Versioning.is_same_kittyhack_version("", "v1") is False
    assert Versioning.is_same_kittyhack_version("a", "b") is False


def test_beta_version_tag_helpers():
    assert Versioning.is_beta_version_tag("v2.6.3_beta_1") is True
    assert Versioning.is_beta_version_tag("V2.6.3_beta_1") is True
    assert Versioning.is_beta_version_tag("2.6.3_beta_12") is True
    assert Versioning.is_beta_version_tag("v2.6.3") is False
    assert Versioning.is_beta_version_tag("v2.6.3-beta.1") is False
    assert Versioning.is_beta_version_tag("") is False

    assert Versioning.is_stable_version_tag("v2.6.3") is True
    assert Versioning.is_stable_version_tag("2.6.3") is True
    assert Versioning.is_stable_version_tag("main") is False
    assert Versioning.is_stable_version_tag("v2.6.3_beta_1") is False
    assert Versioning.is_stable_version_tag("") is False

    assert Versioning.beta_version_sort_key("v2.6.3_beta_1") == (2, 6, 3, 1)
    assert Versioning.beta_version_sort_key("V2.6.3_beta_10") == (2, 6, 3, 10)

    tags = [
        "v2.6.2",
        "v2.6.3_beta_1",
        "v2.6.3_beta_2",
        "v2.6.4_beta_1",
        "v2.6.3",
        "V2.5.9_beta_9",
    ]
    assert Versioning.pick_latest_beta_tag(tags) == "v2.6.4_beta_1"
    assert Versioning.pick_latest_non_beta_tag(tags) == "v2.6.3"
    assert Versioning.pick_latest_beta_tag(["v2.6.3", "main"]) is None
    assert Versioning.pick_latest_non_beta_tag(["v2.6.3_beta_1"]) is None
    assert Versioning.pick_latest_non_beta_tag(["main", "develop"]) is None

    # Beta channel: beta ahead of release → keep beta
    assert Versioning.pick_latest_beta_channel_tag(tags) == "v2.6.4_beta_1"
    # No beta tags → fall back to latest release (not "unknown")
    assert Versioning.pick_latest_beta_channel_tag(["v2.6.3", "v2.6.2", "main"]) == "v2.6.3"
    assert Versioning.pick_latest_beta_channel_tag(["main"]) is None
    # Release base >= beta base → prefer the shipped release
    assert (
        Versioning.pick_latest_beta_channel_tag(
            ["v2.6.3", "v2.6.3_beta_4", "v2.6.2_beta_9"]
        )
        == "v2.6.3"
    )
    # Release older than beta base → keep beta
    assert (
        Versioning.pick_latest_beta_channel_tag(["v2.6.2", "v2.6.3_beta_1"])
        == "v2.6.3_beta_1"
    )
    # Only betas → keep latest beta
    assert (
        Versioning.pick_latest_beta_channel_tag(["v2.6.3_beta_1", "v2.6.3_beta_2"])
        == "v2.6.3_beta_2"
    )


def test_parse_repo_spec():
    owner, repo, ref = Versioning._parse_repo_spec("floppyFK/kittyhack@main")
    assert owner == "floppyFK" and repo == "kittyhack" and ref == "main"
    assert Versioning._parse_repo_spec("") == (None, None, None)
    owner, repo, ref = Versioning._parse_repo_spec("floppyFK/kittyhack@v2.6.3_beta_1")
    assert owner == "floppyFK" and repo == "kittyhack" and ref == "v2.6.3_beta_1"


def test_resolved_update_repo_modes(monkeypatch):
    import src.baseconfig as baseconfig

    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY_MODE", "beta")
    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY", "")
    owner, repo, ref, url, mode = Versioning.resolved_update_repo()
    assert mode == "beta"
    assert owner == "floppyFK" and repo == "kittyhack" and ref is None
    assert url.endswith("floppyFK/kittyhack.git")

    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY_MODE", "standard")
    _o, _r, _ref, _url, mode = Versioning.resolved_update_repo()
    assert mode == "standard"


def test_read_latest_kittyhack_version_beta_channel(monkeypatch):
    """Beta channel must fall back to stable instead of reporting 'unknown'."""
    import src.baseconfig as baseconfig

    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY_MODE", "beta")
    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY", "")

    # No beta tags → latest non-beta release
    monkeypatch.setattr(
        Versioning,
        "_list_github_tag_names",
        lambda owner, repo, timeout=10: ["v2.6.3", "v2.6.2", "main"],
    )
    assert Versioning.read_latest_kittyhack_version(timeout=1) == "v2.6.3"

    # Release base >= beta base → prefer shipped release
    monkeypatch.setattr(
        Versioning,
        "_list_github_tag_names",
        lambda owner, repo, timeout=10: [
            "v2.6.3",
            "v2.6.3_beta_4",
            "v2.6.2_beta_9",
            "main",
        ],
    )
    assert Versioning.read_latest_kittyhack_version(timeout=1) == "v2.6.3"

    # Beta ahead of release → keep beta
    monkeypatch.setattr(
        Versioning,
        "_list_github_tag_names",
        lambda owner, repo, timeout=10: ["v2.6.2", "v2.6.3_beta_1", "main"],
    )
    assert Versioning.read_latest_kittyhack_version(timeout=1) == "v2.6.3_beta_1"

    # Nothing usable → unknown
    monkeypatch.setattr(
        Versioning,
        "_list_github_tag_names",
        lambda owner, repo, timeout=10: ["main", "develop"],
    )
    assert Versioning.read_latest_kittyhack_version(timeout=1) == "unknown"


def test_read_latest_kittyhack_version_standard_ignores_betas(monkeypatch):
    """Standard channel must never surface ``_beta_N`` tags as LATEST_VERSION."""
    import src.baseconfig as baseconfig
    import src.helper as helper_mod

    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY_MODE", "standard")
    monkeypatch.setitem(baseconfig.CONFIG, "UPDATE_REPOSITORY", "")

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    # Happy path: /releases/latest is stable even when betas exist elsewhere.
    def _get_stable(url, timeout=10, params=None):
        assert "/releases/latest" in url
        return _Resp({"tag_name": "v2.6.4"})

    monkeypatch.setattr(helper_mod.requests, "get", _get_stable)
    assert Versioning.read_latest_kittyhack_version(timeout=1) == "v2.6.4"

    # Mis-published beta as non-prerelease "latest" → reject and pick stable.
    def _get_beta_as_latest(url, timeout=10, params=None):
        assert "/releases/latest" in url
        return _Resp({"tag_name": "v2.6.5_beta_1"})

    monkeypatch.setattr(helper_mod.requests, "get", _get_beta_as_latest)
    monkeypatch.setattr(
        Versioning,
        "_list_github_tag_names",
        lambda owner, repo, timeout=10: [
            "v2.6.5_beta_1",
            "v2.6.4",
            "v2.6.3",
            "main",
        ],
    )
    assert Versioning.read_latest_kittyhack_version(timeout=1) == "v2.6.4"


def test_event_type_pretty_and_uuid():
    pretty = EventType.to_pretty_string(EventType.CAT_WENT_INSIDE)
    assert isinstance(pretty, str) and len(pretty) > 0
    assert is_valid_uuid4("550e8400-e29b-41d4-a716-446655440000") is True
    assert is_valid_uuid4("not-a-uuid") is False


def test_serviceops_systemcmd_simulate(monkeypatch):
    monkeypatch.setenv("KITTYHACK_SIMULATE", "1")
    from src import runtime_flags

    monkeypatch.setattr(runtime_flags, "_FORCE_SIMULATE", None)
    assert ServiceOps.systemcmd(["/sbin/reboot"]) is True


def test_backend_constants_importable():
    assert constants.TAG_TIMEOUT == 30.0
    assert constants.MAX_UNLOCK_TIME == 60.0
    assert constants.EVENT_COOLDOWN_SECONDS == 3.0


def test_backend_package_lazy_exports():
    from src.backend import manual_door_override

    assert "unlock_inside" in manual_door_override
