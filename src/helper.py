"""Shared helpers for Kittyhack.

Domain helpers live on `Versioning`, `SystemInfo`, `DateTimeUtil`, and
`ImageUtil`. Remaining misc utilities stay as module-level functions.
Module-level aliases preserve existing imports.
"""
from dataclasses import dataclass

from datetime import datetime, time, timezone

from zoneinfo import ZoneInfo

import subprocess

import logging

import signal

import os

import threading

import requests

import shlex

import cv2

import numpy as np

import socket

import sys

import re

import htmltools

try:
    import fcntl
except ImportError:  # Windows / non-POSIX
    fcntl = None  # type: ignore

import struct

import uuid

import time as tm

import shutil

from faicons import icon_svg

from src.baseconfig import set_language, update_single_config_parameter, CONFIG, AllowedToExit

from src.system import ServiceOps

_ = set_language(CONFIG['LANGUAGE'])

@dataclass
class Result:
    """Simple success/message return value for helpers and DB writes."""
    success: bool
    message: str

class EventType:
    """Canonical event-type strings plus UI pretty labels and icons."""
    MOTION_OUTSIDE_ONLY = "motion_outside_only"
    MOTION_OUTSIDE_WITH_MOUSE = "motion_outside_with_mouse"
    CAT_WENT_INSIDE = "cat_went_inside"
    CAT_WENT_PROBABLY_INSIDE = "cat_went_probably_inside"
    CAT_WENT_INSIDE_WITH_MOUSE = "cat_went_inside_with_mouse"
    CAT_WENT_OUTSIDE = "cat_went_outside"
    MANUALLY_UNLOCKED = "manually_unlocked"
    MANUALLY_LOCKED = "manually_locked"
    MAX_UNLOCK_TIME_EXCEEDED = "max_unlock_time_exceeded"
    # Per-cat mode informational flags (appended as additional verdict infos)
    PER_CAT_PREY_DISABLED = "per_cat_prey_detection_disabled"
    ENTRY_PER_CAT_ALLOWED = "entry_per_cat_allowed"
    ENTRY_PER_CAT_DENIED = "entry_per_cat_denied"
    EXIT_PER_CAT_ALLOWED = "exit_per_cat_allowed"
    EXIT_PER_CAT_DENIED = "exit_per_cat_denied"

    @staticmethod
    def to_pretty_string(event_type):
        """Return a translated human-readable label for an event type."""
        return {
            EventType.MOTION_OUTSIDE_ONLY: _("Motion outside only"),
            EventType.MOTION_OUTSIDE_WITH_MOUSE: _("Motion outside with mouse"),
            EventType.CAT_WENT_INSIDE: _("Cat went inside"),
            EventType.CAT_WENT_PROBABLY_INSIDE: _("Cat went probably inside (no motion inside detected, but the flap was unlocked)"),
            EventType.CAT_WENT_INSIDE_WITH_MOUSE: _("Cat went inside with mouse"),
            EventType.CAT_WENT_OUTSIDE: _("Cat went outside"),
            EventType.MANUALLY_UNLOCKED: _("Manually unlocked flap"),
            EventType.MANUALLY_LOCKED: _("Manually locked flap"),
            EventType.MAX_UNLOCK_TIME_EXCEEDED: _("Maximum unlock time exceeded"),
            EventType.PER_CAT_PREY_DISABLED: _("Per-cat mode: prey detection disabled for this cat"),
            EventType.ENTRY_PER_CAT_ALLOWED: _("Per-cat mode: entry allowed for this cat"),
            EventType.ENTRY_PER_CAT_DENIED: _("Per-cat mode: entry denied for this cat"),
            EventType.EXIT_PER_CAT_ALLOWED: _("Per-cat mode: exit allowed for this cat"),
            EventType.EXIT_PER_CAT_DENIED: _("Per-cat mode: exit denied for this cat"),
        }.get(event_type, _("Unknown event"))

    @staticmethod
    def to_icons(event_type):
        """Return Font Awesome / local SVG icon markup for an event type."""
        return {
            EventType.MOTION_OUTSIDE_ONLY: [str(icon_svg("eye"))],
            EventType.MOTION_OUTSIDE_WITH_MOUSE: [str(icon_svg("hand")), icon_svg_local("mouse")],
            EventType.CAT_WENT_INSIDE: [str(icon_svg("circle-down"))],
            EventType.CAT_WENT_PROBABLY_INSIDE: [str(icon_svg("circle-down")), str(icon_svg("circle-question"))],
            EventType.CAT_WENT_INSIDE_WITH_MOUSE: [str(icon_svg("circle-down"))],
            EventType.CAT_WENT_OUTSIDE: [str(icon_svg("circle-up"))],
            EventType.MANUALLY_UNLOCKED: [str(icon_svg("lock-open"))],
            EventType.MANUALLY_LOCKED: [str(icon_svg("lock"))],
            EventType.MAX_UNLOCK_TIME_EXCEEDED: [str(icon_svg("clock"))],
            # Use information icons for per-cat notes
            EventType.PER_CAT_PREY_DISABLED: [str(icon_svg("circle-info"))],
            EventType.ENTRY_PER_CAT_ALLOWED: [str(icon_svg("circle-check"))],
            EventType.ENTRY_PER_CAT_DENIED: [str(icon_svg("circle-xmark"))],
            EventType.EXIT_PER_CAT_ALLOWED: [str(icon_svg("circle-check"))],
            EventType.EXIT_PER_CAT_DENIED: [str(icon_svg("circle-xmark"))],
        }.get(event_type, [str(icon_svg("circle-question"))])

class GracefulKiller:
    """SIGINT/SIGTERM handler that waits for registered backend tasks before exit."""

    def __init__(self):
        """Install signal handlers and initialize shutdown/task tracking state."""
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        self.stop_now = False
        self.tasks_done = threading.Event()
        self.tasks_count = 0
        self.lock = threading.Lock()
        self._shutdown_started = False

    def _get_tasks_count(self) -> int:
        with self.lock:
            return int(self.tasks_count)

    def _wait_for_tasks(self, timeout_s: float | None) -> bool:
        """Wait until registered tasks finish; True if done before timeout."""
        deadline = None
        if timeout_s is not None:
            try:
                deadline = tm.monotonic() + float(timeout_s)
            except Exception:
                deadline = None

        while True:
            count = self._get_tasks_count()
            if count <= 0:
                return True

            if deadline is not None:
                try:
                    if tm.monotonic() >= deadline:
                        return False
                except Exception:
                    # If monotonic fails for some reason, fall back to best-effort waiting.
                    deadline = None

            # Wait a bit, then re-check.
            self.tasks_done.wait(timeout=0.5)

    def exit_gracefully(self, signum, frame):
        """Handle SIGINT/SIGTERM: wait for tasks, clear shutdown flag, then exit."""
        if self._shutdown_started:
            return
        self._shutdown_started = True

        self.stop_now = True
        logging.info("Waiting for all tasks to finish...")

        with self.lock:
            if self.tasks_count <= 0:
                self.tasks_done.set()
            else:
                self.tasks_done.clear()

        # Keep this comfortably below systemd's TimeoutStopSec=30.
        all_tasks_done = self._wait_for_tasks(timeout_s=20.0)
        if not all_tasks_done:
            pending = self._get_tasks_count()
            logging.warning(f"Graceful shutdown timeout reached with {pending} pending task(s). Proceeding with forced shutdown to avoid systemd timeout.")

        # Set the shutdown flag (even if tasks were stuck) to avoid false NOT_GRACEFUL_SHUTDOWN increments.
        try:
            CONFIG['STARTUP_SHUTDOWN_FLAG'] = False
            update_single_config_parameter("STARTUP_SHUTDOWN_FLAG")
            logging.info("Updated STARTUP_SHUTDOWN_FLAG in the configfile to: False")
        except Exception as e:
            logging.error(f"Failed to update STARTUP_SHUTDOWN_FLAG during shutdown: {e}")

        # Best-effort: terminate child processes quickly (e.g., libcamera-vid).
        try:
            subprocess.run(["/usr/bin/pkill", "-TERM", "-P", str(os.getpid())], check=False)
        except Exception:
            pass

        logging.info("All tasks finished. Exiting now." if all_tasks_done else "Exiting now (forced).")
        try:
            subprocess.run(["/usr/bin/pkill", "-9", "-f", "shiny"], check=False)  # Ensure shiny exits
        finally:
            os._exit(0)

    def halt_backend(self):
        """Stop the backend loop and wait briefly for registered tasks."""
        self.stop_now = True
        with self.lock:
            if self.tasks_count <= 0:
                self.tasks_done.set()
            else:
                self.tasks_done.clear()
        self._wait_for_tasks(timeout_s=10.0)
        

    def signal_task_done(self):
        """Decrement the registered-task counter; set done when it reaches zero."""
        with self.lock:
            self.tasks_count -= 1
            if self.tasks_count <= 0:
                self.tasks_done.set()

    def register_task(self):
        """Increment the registered-task counter so shutdown waits for this work."""
        with self.lock:
            self.tasks_count += 1
            if self.tasks_count > 0:
                self.tasks_done.clear()

sigterm_monitor = GracefulKiller()

DEFAULT_UPDATE_REPO_OWNER = "floppyFK"

DEFAULT_UPDATE_REPO_NAME = "kittyhack"

# Beta release tags: e.g. v2.6.3_beta_1 / V2.6.3_beta_1 (never offered on Standard).
_BETA_TAG_RE = re.compile(r"(?i)^v?\d+(?:\.\d+)*_beta_\d+$")
_BETA_SORT_RE = re.compile(r"(?i)^v?(\d+)\.(\d+)\.(\d+)_beta_(\d+)$")
# Non-beta release tags: e.g. v2.6.3 / 2.6.3 (rejects branch names like "main").
_STABLE_TAG_RE = re.compile(r"(?i)^v?\d+(?:\.\d+)+$")

# Target-mode default. Prefer ``heavy_update_repo_files()`` so remote hosts
# watch ``requirements_remote.txt`` instead of the Kittyflap file.
HEAVY_UPDATE_REPO_FILES = ("setup/REQUIRED_PYTHON", "requirements.txt")
MIN_HEAVY_UPDATE_FREE_DISK_MB = 2048


def heavy_update_repo_files() -> tuple[str, ...]:
    """Runtime files whose change implies a venv rebuild and/or full pip reinstall.

    Kittyflap installs ``requirements.txt`` (no torch). Remote hosts install
    ``requirements_remote.txt``.
    """
    from src.mode import is_remote_mode

    req = "requirements_remote.txt" if is_remote_mode() else "requirements.txt"
    return ("setup/REQUIRED_PYTHON", req)

class Versioning:
    """Git/version comparison, update-repo resolution, changelogs, release notes."""

    @staticmethod
    def filter_release_notes_for_language(markdown_text: str, language: str) -> str:
        """If release notes contain both 'Deutsch' and 'English' sections, return only the configured one."""
        if not isinstance(markdown_text, str):
            return markdown_text
        text = markdown_text.strip("\n")
        if not text:
            return markdown_text

        if language not in ("de", "en"):
            return markdown_text

        wanted_label = "Deutsch" if language == "de" else "English"

        # Expected style:
        #   # vX.Y.Z - Deutsch
        #   ...
        #   --------
        #   # vX.Y.Z - English
        #   ...
        header_re = re.compile(
            r"^\s*#+\s*v?\d+(?:\.\d+)*\s*-\s*(Deutsch|English)\s*$",
            flags=re.IGNORECASE,
        )

        lines = text.splitlines()
        blocks: dict[str, tuple[int, int]] = {}

        header_positions: list[tuple[int, str]] = []
        for idx, line in enumerate(lines):
            m = header_re.match(line)
            if m:
                label = m.group(1)
                label = "Deutsch" if label.lower().startswith("de") else "English"
                header_positions.append((idx, label))

        labels_present = {label for _, label in header_positions}
        if not {"Deutsch", "English"}.issubset(labels_present):
            return markdown_text

        for i, (start_idx, label) in enumerate(header_positions):
            end_idx = header_positions[i + 1][0] if i + 1 < len(header_positions) else len(lines)
            blocks.setdefault(label, (start_idx, end_idx))

        if wanted_label not in blocks:
            return markdown_text

        start, end = blocks[wanted_label]
        chosen = lines[start:end]

        def is_separator(s: str) -> bool:
            s2 = (s or "").strip()
            if re.fullmatch(r"[-_]{3,}", s2):
                return True
            if re.fullmatch(r"(?:-\s*){3,}", s2):
                return True
            if re.fullmatch(r"(?:_\s*){3,}", s2):
                return True
            if re.fullmatch(r"\*{3,}", s2):
                return True
            return False

        def is_blank(s: str) -> bool:
            return not (s or "").strip()

        changed = True
        while chosen and changed:
            changed = False
            while chosen and is_blank(chosen[0]):
                chosen.pop(0)
                changed = True
            while chosen and is_blank(chosen[-1]):
                chosen.pop()
                changed = True
            while chosen and is_separator(chosen[0]):
                chosen.pop(0)
                changed = True
            while chosen and is_separator(chosen[-1]):
                chosen.pop()
                changed = True

        return "\n".join(chosen).strip("\n") or markdown_text

    @staticmethod
    def get_git_version():
        """Return the exact git tag for HEAD, else the short commit hash (or 'unknown')."""
        git_command = "/usr/bin/git" if os.name == "posix" else "git"

        try:
            # Check if the current commit has a tag
            tag = subprocess.check_output(
                [git_command, "describe", "--tags", "--exact-match"],
                stderr=subprocess.DEVNULL,
                text=True
            ).strip()
            return tag
        except subprocess.CalledProcessError:
            # If no tag is found, return the short commit hash
            try:
                commit_hash = subprocess.check_output(
                    [git_command, "rev-parse", "--short", "HEAD"],
                    text=True
                ).strip()
                return commit_hash
            except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                return "unknown"
        except (FileNotFoundError, OSError):
            return "unknown"

    @staticmethod
    def normalize_version(version_str):
        """Strip a leading 'v' and any '-commit' suffix from a version string."""
        # Remove 'v' prefix if present
        if version_str.startswith('v'):
            version_str = version_str[1:]
        # Remove commit hash if present
        if '-' in version_str:
            version_str = version_str.split('-')[0]
        return version_str

    @staticmethod
    def is_same_kittyhack_version(installed: str, reference: str) -> bool:
        """True if installed matches reference (tag equality or branch ``ref@sha`` SHA)."""
        if not installed or not reference:
            return False
        if installed == reference:
            return True
        if "@" in reference:
            _, _, ref_sha = reference.rpartition("@")
            if ref_sha and ref_sha == installed:
                return True
        return False

    @staticmethod
    def is_beta_version_tag(tag: str) -> bool:
        """True if tag matches the beta release naming ``…_beta_<counter>``."""
        if not tag:
            return False
        return bool(_BETA_TAG_RE.match(str(tag).strip()))

    @staticmethod
    def beta_version_sort_key(tag: str) -> tuple[int, int, int, int]:
        """Sort key for beta tags ``vX.Y.Z_beta_N`` → ``(X, Y, Z, N)``."""
        m = _BETA_SORT_RE.match(str(tag or "").strip())
        if not m:
            return (0, 0, 0, 0)
        return tuple(int(x) for x in m.groups())

    @staticmethod
    def pick_latest_beta_tag(tags) -> str | None:
        """Return the highest ``…_beta_N`` tag from ``tags``, or None."""
        betas = [str(t).strip() for t in (tags or []) if Versioning.is_beta_version_tag(t)]
        if not betas:
            return None
        return max(betas, key=Versioning.beta_version_sort_key)

    @staticmethod
    def is_stable_version_tag(tag: str) -> bool:
        """True if tag looks like a non-beta release ``vX.Y.Z`` (not a branch name)."""
        if not tag:
            return False
        return bool(_STABLE_TAG_RE.match(str(tag).strip()))

    @staticmethod
    def pick_latest_non_beta_tag(tags) -> str | None:
        """Return the highest non-beta ``vX.Y.Z``-style tag from ``tags``, or None."""
        stables = []
        for raw in tags or []:
            tag = str(raw or "").strip()
            if not Versioning.is_stable_version_tag(tag):
                continue
            stables.append(tag)
        if not stables:
            return None
        return max(stables, key=lambda t: Versioning.parse_version(t))

    @staticmethod
    def pick_latest_beta_channel_tag(tags) -> str | None:
        """Pick the update target for the beta channel.

        Rules:
        - Prefer the newest ``…_beta_N`` tag when it is ahead of the latest release
          (e.g. ``v2.6.4_beta_1`` beats ``v2.6.3``).
        - If no beta exists, fall back to the latest non-beta release.
        - If the latest release base version is **greater than or equal to** the
          latest beta base (e.g. release ``v2.6.3`` vs beta ``v2.6.3_beta_4``),
          use the release — a shipped release supersedes betas of the same base.
        """
        beta = Versioning.pick_latest_beta_tag(tags)
        stable = Versioning.pick_latest_non_beta_tag(tags)
        if not beta:
            return stable
        if not stable:
            return beta
        beta_base = Versioning.beta_version_sort_key(beta)[:3]
        stable_base = Versioning.parse_version(stable)
        if stable_base >= beta_base:
            return stable
        return beta

    @staticmethod
    def _parse_repo_spec(raw: str):
        """Parse ``owner/repo[@ref]`` (or ``owner:branch``) into (owner, repo, ref)."""
        if not raw:
            return None, None, None
        raw = raw.strip()

        # Slash form: owner/repo[@ref], possibly with URL wrapping.
        m = re.match(
            r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:@([\w./\-]+))?$",
            raw,
        )
        if m:
            return m.group(1), m.group(2), m.group(3) or None

        # Colon form: owner:branch — GitHub's "head ref" shorthand that shows up
        # in the PR header ("wants to merge N commits from FabulousGee:feat/xyz").
        # Repo name is implicit and defaults to this project.
        m = re.match(r"^([\w.-]+):([\w./\-]+)$", raw)
        if m:
            return m.group(1), DEFAULT_UPDATE_REPO_NAME, m.group(2)

        return None, None, None

    @staticmethod
    def normalize_repo_spec(raw: str) -> str | None:
        """Return canonical ``owner/repo[@ref]``, or None if the spec is invalid."""
        owner, repo, ref = Versioning._parse_repo_spec(raw)
        if not owner or not repo:
            return None
        return f"{owner}/{repo}" + (f"@{ref}" if ref else "")

    @staticmethod
    def check_custom_update_repo_reachable(raw_spec: str, timeout: float = 5.0):
        """Check GitHub that a custom update repo/ref exists; return (ok, reason, detail)."""
        owner, repo, ref = Versioning._parse_repo_spec(raw_spec)
        if not owner or not repo:
            return False, "invalid_format", ""

        try:
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                timeout=timeout,
            )
        except requests.RequestException as e:
            return False, "network_error", str(e)

        if r.status_code == 404:
            return False, "repo_not_found", f"{owner}/{repo}"
        if r.status_code != 200:
            return False, "network_error", f"HTTP {r.status_code} for {owner}/{repo}"

        if ref is None:
            return True, "", ""

        try:
            r = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}",
                timeout=timeout,
            )
        except requests.RequestException as e:
            return False, "network_error", str(e)

        if r.status_code == 404:
            return False, "ref_not_found", f"{owner}/{repo}@{ref}"
        if r.status_code != 200:
            return False, "network_error", f"HTTP {r.status_code} for {owner}/{repo}@{ref}"

        return True, "", ""

    @staticmethod
    def resolved_update_repo():
        """Resolve CONFIG update source to (owner, repo, ref, git_url, mode)."""
        try:
            from src.baseconfig import CONFIG
            mode = str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard").strip().lower()
            if mode == "custom":
                owner, repo, ref = Versioning._parse_repo_spec(CONFIG.get("UPDATE_REPOSITORY") or "")
                if owner and repo:
                    return owner, repo, ref, f"https://github.com/{owner}/{repo}.git", "custom"
                logging.warning(
                    f"[UPDATE] Invalid custom repository spec '{CONFIG.get('UPDATE_REPOSITORY', '')}'; "
                    f"falling back to {DEFAULT_UPDATE_REPO_OWNER}/{DEFAULT_UPDATE_REPO_NAME}"
                )
            elif mode == "beta":
                return (
                    DEFAULT_UPDATE_REPO_OWNER,
                    DEFAULT_UPDATE_REPO_NAME,
                    None,
                    f"https://github.com/{DEFAULT_UPDATE_REPO_OWNER}/{DEFAULT_UPDATE_REPO_NAME}.git",
                    "beta",
                )
        except Exception as e:
            logging.debug(f"[UPDATE] Versioning.resolved_update_repo fell back to default: {e}")
        return (
            DEFAULT_UPDATE_REPO_OWNER,
            DEFAULT_UPDATE_REPO_NAME,
            None,
            f"https://github.com/{DEFAULT_UPDATE_REPO_OWNER}/{DEFAULT_UPDATE_REPO_NAME}.git",
            "standard",
        )

    @staticmethod
    def _list_github_tag_names(owner: str, repo: str, timeout: float = 10) -> list[str]:
        """Return tag names from GitHub (releases + tags endpoints), newest pages first."""
        names: list[str] = []
        seen: set[str] = set()

        def _add(tag: str) -> None:
            tag = str(tag or "").strip()
            if tag and tag not in seen:
                seen.add(tag)
                names.append(tag)

        try:
            response = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/releases",
                params={"per_page": 100},
                timeout=timeout,
            )
            response.raise_for_status()
            for release in response.json():
                if release.get("draft"):
                    continue
                _add(release.get("tag_name", ""))
        except Exception as e:
            logging.debug(f"[UPDATE] Failed to list GitHub releases for {owner}/{repo}: {e}")

        try:
            response = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/tags",
                params={"per_page": 100},
                timeout=timeout,
            )
            response.raise_for_status()
            for tag in response.json():
                _add(tag.get("name", ""))
        except Exception as e:
            logging.debug(f"[UPDATE] Failed to list GitHub tags for {owner}/{repo}: {e}")

        return names

    @staticmethod
    def read_latest_kittyhack_version(timeout=10) -> str:
        """Fetch latest release tag, or ``ref@sha`` in branch mode (else 'unknown').

        - **standard** / custom without ref: newest non-beta release (``_beta_N`` tags excluded)
        - **beta**: newest ``…_beta_<counter>`` on the official repo, falling back to the
          latest non-beta release when no beta exists or the release base is >= the beta base
        - custom with ref: ``ref@sha7`` from the commits API
        """
        owner, repo, ref, _git_url, mode = Versioning.resolved_update_repo()
        try:
            ts_pre = tm.time()
            if ref is not None:
                url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
                response = requests.get(url, timeout=timeout)
                ts_post = tm.time()
                sha = str(response.json().get("sha", "") or "")[:7]
                latest_version = f"{ref}@{sha}" if sha else "unknown"
            elif mode == "beta":
                tags = Versioning._list_github_tag_names(owner, repo, timeout=timeout)
                ts_post = tm.time()
                latest_version = Versioning.pick_latest_beta_channel_tag(tags) or "unknown"
            else:
                # Stable channel (standard, or custom owner/repo without @ref).
                url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
                response = requests.get(url, timeout=timeout)
                ts_post = tm.time()
                latest_version = str(response.json().get("tag_name", "unknown"))
                # Hard guarantee: Standard must never surface a ``_beta_N`` tag, even if
                # it was published as a non-prerelease GitHub Release.
                if Versioning.is_beta_version_tag(latest_version):
                    logging.warning(
                        f"[UPDATE] /releases/latest returned beta tag '{latest_version}' for "
                        f"{owner}/{repo}; selecting newest non-beta tag instead"
                    )
                    tags = Versioning._list_github_tag_names(owner, repo, timeout=timeout)
                    ts_post = tm.time()
                    latest_version = Versioning.pick_latest_non_beta_tag(tags) or "unknown"
            logging.info(
                f"GitHub latest version fetch took {ts_post - ts_pre:.3f} seconds "
                f"({owner}/{repo}{'@' + ref if ref else ''} mode={mode}). Latest: {latest_version}"
            )
            return latest_version
        except Exception as e:
            logging.error(f"Failed to fetch the latest version from GitHub ({owner}/{repo}): {e}")
            return "unknown"

    @staticmethod
    def resolve_update_checkout_ref(target_version: str | None = None) -> str | None:
        """Return the git ref for an update target (tag, branch, or short SHA)."""
        _owner, _repo, update_ref, _git_url, _mode = Versioning.resolved_update_repo()
        if update_ref is not None:
            ver = str(target_version or "").strip()
            if "@" in ver:
                _branch, maybe_sha = ver.rsplit("@", 1)
                if maybe_sha.strip():
                    return maybe_sha.strip()
            return update_ref
        ver = str(target_version or "").strip()
        if not ver or ver == "unknown":
            return None
        return ver

    @staticmethod
    def _normalize_repo_file_for_compare(rel_path: str, text: str | None) -> str | None:
        if text is None:
            return None
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if rel_path.endswith("REQUIRED_PYTHON"):
            return normalized.strip()
        return normalized

    @staticmethod
    def _read_local_repo_file(rel_path: str) -> str | None:
        from src.paths import kittyhack_root

        path = os.path.join(kittyhack_root(), rel_path)
        try:
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logging.debug(f"[UPDATE] Failed to read local {rel_path}: {e}")
            return None

    @staticmethod
    def _read_repo_file_at_ref_via_git(ref: str, rel_path: str) -> tuple[bool, str | None]:
        """Return ``(resolved, content)`` for ``ref:rel_path`` via local git.

        ``resolved`` is True when git answered for a usable ref (content may still
        be None if the path is absent). False means the ref could not be queried.
        """
        from src.paths import kittyhack_root

        candidates = [ref]
        if "/" not in ref and not ref.startswith(("v", "refs/")):
            candidates.append(f"origin/{ref}")
        for candidate in candidates:
            try:
                result = subprocess.run(
                    ["/bin/git", "show", f"{candidate}:{rel_path}"],
                    cwd=kittyhack_root(),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if result.returncode == 0:
                    return True, result.stdout
                err = (result.stderr or "").lower()
                if (
                    "unknown revision" in err
                    or "bad revision" in err
                    or "invalid object name" in err
                    or "bad object" in err
                ):
                    continue
                # Usable ref, but path missing (or other non-fatal path error).
                if "does not exist" in err or "exists on disk" in err:
                    return True, None
            except Exception as e:
                logging.debug(f"[UPDATE] git show {candidate}:{rel_path} failed: {e}")
        return False, None

    @staticmethod
    def _read_repo_file_at_ref_via_github(
        owner: str, repo: str, ref: str, rel_path: str, timeout: float = 8
    ) -> tuple[bool, str | None]:
        """Return ``(resolved, content)`` from GitHub at ``ref``.

        ``resolved`` True + content None means the file is confirmed missing (404).
        """
        import base64

        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{rel_path}"
        try:
            response = requests.get(
                url,
                params={"ref": ref},
                headers={"Accept": "application/vnd.github.raw"},
                timeout=timeout,
            )
            if response.status_code == 200:
                return True, response.text
            if response.status_code == 404:
                return True, None
            # Fallback: JSON + base64 payload.
            response = requests.get(url, params={"ref": ref}, timeout=timeout)
            if response.status_code == 404:
                return True, None
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("encoding") == "base64":
                raw = payload.get("content") or ""
                return True, base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception as e:
            logging.debug(
                f"[UPDATE] Failed to fetch {owner}/{repo}/{rel_path}@{ref} from GitHub: {e}"
            )
        return False, None

    @staticmethod
    def get_runtime_file_changes(
        target_version: str | None = None, *, use_git_show: bool = False
    ) -> dict[str, bool]:
        """Return which heavy-update files differ from the target ref.

        Keys are paths from ``heavy_update_repo_files()``. A path is omitted (or
        False) when it could not be resolved — transient failures must not look
        like a change.
        """
        tracked = heavy_update_repo_files()
        changes = {path: False for path in tracked}
        ref = Versioning.resolve_update_checkout_ref(target_version)
        if not ref:
            return changes

        owner, repo, update_ref, _git_url, _mode = Versioning.resolved_update_repo()
        github_refs = [ref]
        if update_ref and update_ref not in github_refs:
            github_refs.append(update_ref)

        for rel_path in tracked:
            local = Versioning._normalize_repo_file_for_compare(
                rel_path, Versioning._read_local_repo_file(rel_path)
            )
            # Prefer HEAD for "installed" side when available (ignores dirty tree).
            head_ok, head_text = Versioning._read_repo_file_at_ref_via_git("HEAD", rel_path)
            if head_ok:
                local = Versioning._normalize_repo_file_for_compare(rel_path, head_text)

            remote_ok = False
            remote_text: str | None = None
            if use_git_show:
                remote_ok, remote_text = Versioning._read_repo_file_at_ref_via_git(
                    ref, rel_path
                )
                if not remote_ok and update_ref:
                    remote_ok, remote_text = Versioning._read_repo_file_at_ref_via_git(
                        f"origin/{update_ref}", rel_path
                    )
            if not remote_ok:
                for gh_ref in github_refs:
                    remote_ok, remote_text = Versioning._read_repo_file_at_ref_via_github(
                        owner, repo, gh_ref, rel_path
                    )
                    if remote_ok:
                        break

            if not remote_ok:
                logging.debug(
                    f"[UPDATE] Could not resolve target {rel_path} at {ref}; skipping compare"
                )
                continue

            remote = Versioning._normalize_repo_file_for_compare(rel_path, remote_text)
            if local != remote:
                logging.info(
                    f"[UPDATE] Heavy-update marker: {rel_path} differs from target ref '{ref}'"
                )
                changes[rel_path] = True

        return changes

    @staticmethod
    def update_changes_runtime_files(
        target_version: str | None = None, *, use_git_show: bool = False
    ) -> bool:
        """True if target differs from local in REQUIRED_PYTHON or this host's requirements file."""
        changes = Versioning.get_runtime_file_changes(
            target_version, use_git_show=use_git_show
        )
        return any(changes.values())

    @staticmethod
    def update_changes_required_python(
        target_version: str | None = None, *, use_git_show: bool = False
    ) -> bool:
        """True if ``setup/REQUIRED_PYTHON`` differs from the update target (venv rebuild)."""
        changes = Versioning.get_runtime_file_changes(
            target_version, use_git_show=use_git_show
        )
        return bool(changes.get("setup/REQUIRED_PYTHON"))

    @staticmethod
    def fetch_github_release_notes(version: str) -> str:
        """Fetch GitHub release body for a version (branch mode returns a short note)."""
        owner, repo, ref, _git_url, _mode = Versioning.resolved_update_repo()

        if ref is not None:
            # Branch mode — no release notes concept.
            return (
                f"You are tracking branch **{ref}** of **{owner}/{repo}**.\n\n"
                f"No release notes are published for branches. Latest commit: `{version}`."
            )

        url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            releases = response.json()
            # Accept both 'vX.Y.Z' and 'X.Y.Z' as tags
            for release in releases:
                tag = release.get("tag_name", "")
                if tag == version or tag == f"v{version}" or tag.lstrip("v") == version.lstrip("v"):
                    return release.get("body", "No release notes found.")
            return f"No release notes found for version {version}."
        except Exception as e:
            return f"Failed to fetch release notes: {e}"

    @staticmethod
    def execute_update_step(command: str, step_description: str) -> bool:
        """Execute a shell command and log its output."""
        try:
            cmd_list = shlex.split(command)
            result = subprocess.run(cmd_list, check=True, capture_output=True, text=True)
            if result.stdout:
                logging.info(f"[{step_description}] {result.stdout}")
            if result.stderr:
                logging.warning(f"[{step_description}] {result.stderr}")
            return True
        except subprocess.CalledProcessError as e:
            error_msg = f"[{step_description}] {str(e)}"
            logging.error(error_msg)
            return False

    @staticmethod
    def changelog_version_sort_key(version_label: str) -> tuple:
        """Sort key for changelog versions; stables sort above same-base betas."""
        label = str(version_label or "").strip()
        if Versioning.is_beta_version_tag(label):
            return Versioning.beta_version_sort_key(label)
        parsed = Versioning.parse_version(label)
        # Pad to 3 components then append a sentinel so stable > any beta of same base.
        if len(parsed) < 3:
            parsed = tuple(list(parsed) + [0] * (3 - len(parsed)))
        return (parsed[0], parsed[1], parsed[2], 10**9)

    @staticmethod
    def list_changelogs(
        after_version: str = "v1.0.0",
        language: str = "en",
        *,
        include_beta: bool = False,
        changelog_dir: str | None = None,
    ) -> list[dict]:
        """Return local changelog entries newer than ``after_version``, newest first.

        Each entry is ``{"version": "vX.Y.Z[_beta_N]", "is_beta": bool, "body": str}``.
        Beta files (``changelog_vX.Y.Z_beta_N_*.md``) are omitted unless ``include_beta``.
        """
        changelog_dir = changelog_dir or "doc/changelogs/"
        if not os.path.exists(changelog_dir):
            logging.warning(f"Changelog directory '{changelog_dir}' not found")
            return []

        try:
            files = os.listdir(changelog_dir)
        except Exception as e:
            logging.error(f"Failed to list changelog directory: {e}")
            return []

        # changelog_v2.7.0_en.md  or  changelog_v2.6.5_beta_1_de.md
        file_re = re.compile(
            r"^changelog_(v\d+(?:\.\d+)+(?:_beta_\d+)?)_([a-z]{2})\.md$",
            re.IGNORECASE,
        )

        lang = (language or "en").lower()
        matching: list[tuple[str, str, bool]] = []  # (version, filename, is_beta)
        for filename in files:
            m = file_re.match(filename)
            if not m:
                continue
            version_label = m.group(1)
            file_lang = m.group(2).lower()
            if file_lang != lang:
                continue
            is_beta = Versioning.is_beta_version_tag(version_label)
            if is_beta and not include_beta:
                continue
            matching.append((version_label, filename, is_beta))

        # Fall back to English if nothing found for the requested language.
        if not matching and lang != "en":
            for filename in files:
                m = file_re.match(filename)
                if not m or m.group(2).lower() != "en":
                    continue
                version_label = m.group(1)
                is_beta = Versioning.is_beta_version_tag(version_label)
                if is_beta and not include_beta:
                    continue
                matching.append((version_label, filename, is_beta))
            if matching:
                logging.info(
                    f"No changelogs found for language '{language}', falling back to English"
                )

        if not matching:
            logging.warning("No changelog files found")
            return []

        after_key = (
            Versioning.changelog_version_sort_key(after_version)
            if after_version != "unknown"
            else (0, 0, 0, -1)
        )

        newer: list[tuple[tuple, str, str, bool]] = []
        for version_label, filename, is_beta in matching:
            key = Versioning.changelog_version_sort_key(version_label)
            if after_version == "unknown" or key > after_key:
                newer.append((key, version_label, filename, is_beta))

        newer.sort(key=lambda item: item[0], reverse=True)

        entries: list[dict] = []
        for _key, version_label, filename, is_beta in newer:
            try:
                with open(
                    os.path.join(changelog_dir, filename), "r", encoding="utf-8"
                ) as f:
                    body = f.read()
            except Exception as e:
                logging.error(f"Failed to read changelog file {filename}: {e}")
                continue
            entries.append(
                {"version": version_label, "is_beta": is_beta, "body": body}
            )
        return entries

    @staticmethod
    def get_changelogs(
        after_version: str = "v1.0.0",
        language: str = "en",
        *,
        include_beta: bool = False,
        changelog_dir: str | None = None,
    ) -> str:
        """Concatenate local changelog markdown newer than ``after_version``."""
        entries = Versioning.list_changelogs(
            after_version=after_version,
            language=language,
            include_beta=include_beta,
            changelog_dir=changelog_dir,
        )
        if not entries:
            return ""
        separator = "\n\n" + "-" * 80 + "\n\n"
        return separator.join(entry["body"] for entry in entries)

    @staticmethod
    def parse_version(v_str):
        """Parse ``vX.Y.Z[-hash]`` into a comparable ``(X, Y, Z)`` tuple."""
        try:
            # Remove 'v' or 'V' prefix if present
            if v_str and v_str[0].lower() == 'v':
                v_str = v_str[1:]

            # Remove git hash suffix if present
            v_str = v_str.split('-')[0]

            # Parse the version components
            return tuple(map(int, v_str.split('.')))
        except:
            return (0, 0, 0)  # Default for unparseable versions

class SystemInfo:
    """Disk/RAM/DB size, IP/port checks, and system information logging."""

    @staticmethod
    def get_free_disk_space():
        """Return free root filesystem space in MB (0 on error)."""
        try:
            stat = os.statvfs('/')
            return (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        except Exception as e:
            logging.error(f"Failed to get the remaining disk space: {e}")
            return 0

    @staticmethod
    def get_total_disk_space():
        """Return total root filesystem space in MB (0 on error)."""
        try:
            stat = os.statvfs('/')
            return (stat.f_blocks * stat.f_frsize) / (1024 * 1024)
        except Exception as e:
            logging.error(f"Failed to get the total disk space: {e}")
            return 0

    @staticmethod
    def get_used_ram_space():
        """Return used RAM in MB from /proc/meminfo (0 on error)."""
        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.readlines()
            for line in meminfo:
                if 'MemAvailable' in line:
                    available_ram = int(line.split()[1]) / 1024  # Convert kB to MB
                    break
            total_ram = SystemInfo.get_total_ram_space()
            used_ram = total_ram - available_ram
            return used_ram
        except Exception as e:
            logging.error(f"Failed to get the used RAM space: {e}")
            return 0

    @staticmethod
    def get_total_ram_space():
        """Return total RAM in MB from /proc/meminfo (0 on error)."""
        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.readlines()
            for line in meminfo:
                if 'MemTotal' in line:
                    total_ram = int(line.split()[1]) / 1024  # Convert kB to MB
                    return total_ram
        except Exception as e:
            logging.error(f"Failed to get the total RAM space: {e}")
            return 0

    @staticmethod
    def get_database_size():
        """Return Kittyhack DB file size in MB (0 if missing/error)."""
        try:
            db_path = CONFIG['KITTYHACK_DATABASE_PATH']
            if os.path.exists(db_path):
                return os.path.getsize(db_path) / (1024 * 1024)  # size in MB
            else:
                logging.error(f"Database file '{db_path}' does not exist.")
                return 0
        except Exception as e:
            logging.error(f"Failed to get the size of the Kittyhack database: {e}")
            return 0

    @staticmethod
    def get_file_size(file_path):
        """Return a file's size in MB (0 if missing/error)."""
        try:
            if os.path.exists(file_path):
                return os.path.getsize(file_path) / (1024 * 1024)  # size in MB
            else:
                logging.error(f"File '{file_path}' does not exist.")
                return 0
        except Exception as e:
            logging.error(f"Failed to get the size of the file '{file_path}': {e}")
            return 0

    @staticmethod
    def log_relevant_deb_packages():
        """Log installed deb packages matching camera/Pi-related name filters."""
        relevant_packages = ["libcamera", "gstreamer", "libpisp", "rpicam", "raspi"]
        try:
            result = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, check=True)
            installed_packages = result.stdout.splitlines()
            for package in installed_packages:
                if any(relevant in package for relevant in relevant_packages):
                    logging.info(f"Installed software package: {package}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to retrieve installed packages: {e}")

    @staticmethod
    def log_system_information():
        """Log a periodic snapshot of OS, memory, CPU, network, and journal errors."""
        def _read_cpu_temperature() -> str | None:
            # Raspberry Pi
            try:
                if shutil.which("vcgencmd"):
                    cpu_temp = subprocess.check_output(["vcgencmd", "measure_temp"], text=True).strip()
                    if cpu_temp:
                        return cpu_temp
            except Exception:
                pass

            # Generic Linux: /sys/class/thermal/thermal_zone*/temp
            try:
                base = "/sys/class/thermal"
                if not os.path.isdir(base):
                    return None

                candidates: list[tuple[int, str, float]] = []
                for name in sorted(os.listdir(base)):
                    if not name.startswith("thermal_zone"):
                        continue

                    zone_dir = os.path.join(base, name)
                    temp_path = os.path.join(zone_dir, "temp")
                    type_path = os.path.join(zone_dir, "type")
                    if not os.path.isfile(temp_path):
                        continue

                    try:
                        raw = open(temp_path, "r", encoding="utf-8").read().strip()
                        if not raw:
                            continue
                        temp_val = float(raw)
                        # Most kernels expose millidegrees C.
                        if temp_val > 200:
                            temp_val = temp_val / 1000.0
                        if temp_val < -40 or temp_val > 130:
                            continue

                        zone_type = ""
                        try:
                            if os.path.isfile(type_path):
                                zone_type = open(type_path, "r", encoding="utf-8").read().strip()
                        except Exception:
                            zone_type = ""

                        zone_type_l = (zone_type or "").lower()
                        # Prefer obvious CPU-related zones.
                        if any(k in zone_type_l for k in ("cpu", "x86_pkg_temp", "package", "soc")):
                            priority = 0
                        else:
                            priority = 1

                        candidates.append((priority, zone_type or name, temp_val))
                    except Exception:
                        continue

                if not candidates:
                    return None

                candidates.sort(key=lambda t: (t[0], -t[2]))
                priority, zone_type, temp_c = candidates[0]
                label = zone_type if zone_type else "thermal"
                return f"{temp_c:.1f}°C ({label})"
            except Exception:
                return None

        def _get_default_route_linux() -> str | None:
            try:
                with open("/proc/net/route", "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                # Iface  Destination  Gateway  Flags  RefCnt  Use  Metric  Mask  MTU  Window  IRTT
                for line in lines[1:]:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    iface, destination, gateway_hex, flags_hex = parts[0], parts[1], parts[2], parts[3]
                    if destination != "00000000":
                        continue
                    try:
                        flags = int(flags_hex, 16)
                    except Exception:
                        flags = 0
                    if flags & 0x2 == 0:  # RTF_GATEWAY
                        continue
                    try:
                        g = int(gateway_hex, 16)
                        gateway_ip = socket.inet_ntoa(struct.pack("<L", g))
                    except Exception:
                        gateway_ip = gateway_hex
                    return f"{iface} via {gateway_ip}"
                return None
            except Exception:
                return None

        def _list_interface_ipv4() -> list[tuple[str, str]]:
            def _get_ip_address(ifname: str) -> str | None:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    return socket.inet_ntoa(fcntl.ioctl(
                        s.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack('256s', ifname[:15].encode('utf-8'))
                    )[20:24])
                except Exception:
                    return None

            out: list[tuple[str, str]] = []
            try:
                for ifname in sorted(os.listdir('/sys/class/net')):
                    if ifname == 'lo':
                        continue
                    ip = _get_ip_address(ifname)
                    if ip:
                        out.append((ifname, ip))
            except Exception:
                pass
            return out

        info_lines = []
        info_lines.append("\n---- System information: ------------------------------------------------")
        info_lines.append(f"System: {os.uname().sysname} {os.uname().release} {os.uname().machine}")
        info_lines.append(f"Python version: {sys.version}")
        info_lines.append(f"Git version: {Versioning.get_git_version()}")
        info_lines.append(f"Kittyhack version: {CONFIG['LATEST_VERSION']}")
        info_lines.append(f"Free disk space: {SystemInfo.get_free_disk_space():.2f} / {SystemInfo.get_total_disk_space():.2f} MB")
        info_lines.append(f"Database size: {SystemInfo.get_database_size():.2f} MB")

        # Memory information
        try:
            with open('/proc/meminfo', 'r') as f:
                mem_total = int(next(line for line in f if 'MemTotal' in line).split()[1]) // 1024
                f.seek(0)
                mem_available = int(next(line for line in f if 'MemAvailable' in line).split()[1]) // 1024
                info_lines.append(f"Memory: {mem_available}MB free of {mem_total}MB")
        except Exception as e:
            info_lines.append(f"Failed to get memory info: {e}")

        # CPU usage and temperature
        try:
            cpu_temp = _read_cpu_temperature()
            info_lines.append(f"CPU Temperature: {cpu_temp}" if cpu_temp else "CPU Temperature: unknown")

            # Get top processes sorted by CPU usage (no piping; works on remote-mode hosts too)
            if shutil.which("ps"):
                ps_cmd = ['ps', '-eo', 'pid,ppid,%mem,%cpu,args', '--sort=-%cpu', '--columns', '200']
                ps_text = subprocess.check_output(ps_cmd, text=True, stderr=subprocess.STDOUT)
                ps_lines = ps_text.splitlines()
                top = "\n".join(ps_lines[:11])  # header + 10 lines
                info_lines.append(f"Top processes by CPU:\n{top}")
            else:
                info_lines.append("Top processes by CPU: unavailable (missing 'ps')")
        except Exception as e:
            info_lines.append(f"Failed to get CPU info: {e}")

        # Network information
        try:
            hostname = socket.gethostname()
            info_lines.append(f"Hostname: {hostname}")

            # Reuse existing helper (scans all interfaces if interface is empty)
            current_ip = SystemInfo.get_current_ip("")
            info_lines.append(f"Current IP: {current_ip}")

            iface_ips = _list_interface_ipv4()
            if iface_ips:
                info_lines.append("Interfaces (IPv4): " + ", ".join([f"{i}={ip}" for i, ip in iface_ips]))
            else:
                info_lines.append("Interfaces (IPv4): none detected")

            default_route = _get_default_route_linux()
            if default_route:
                info_lines.append(f"Default route: {default_route}")

            # Optional: detailed WiFi info on systems that ship wireless-tools
            try:
                if shutil.which("iwconfig"):
                    wifi_ifaces: list[str] = []
                    try:
                        # Prefer /proc/net/wireless if present
                        if os.path.isfile("/proc/net/wireless"):
                            with open("/proc/net/wireless", "r", encoding="utf-8") as f:
                                for line in f.read().splitlines()[2:]:
                                    if ":" in line:
                                        wifi_ifaces.append(line.split(":", 1)[0].strip())
                    except Exception:
                        wifi_ifaces = []

                    if not wifi_ifaces:
                        try:
                            for ifname in sorted(os.listdir('/sys/class/net')):
                                if ifname.startswith(("wlan", "wl")):
                                    wifi_ifaces.append(ifname)
                        except Exception:
                            wifi_ifaces = []

                    if wifi_ifaces:
                        wifi_iface = wifi_ifaces[0]
                        wifi_info = subprocess.check_output(["iwconfig", wifi_iface], text=True, stderr=subprocess.STDOUT)
                        info_lines.append(f"WiFi status ({wifi_iface}):\n{wifi_info}")
            except Exception:
                pass
        except Exception as e:
            info_lines.append(f"Failed to get network info: {e}")

        # Check internet connectivity
        try:
            ping_result = subprocess.run(['ping', '-c', '1', '-W', '2', '8.8.8.8'], capture_output=True, text=True)
            info_lines.append(f"Internet connectivity: {'Connected' if ping_result.returncode == 0 else 'Disconnected'}")
        except Exception as e:
            info_lines.append(f"Failed to check internet connectivity: {e}")

        # System uptime
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                uptime_days = uptime_seconds / 86400  # Convert seconds to days
                info_lines.append(f"System uptime: {uptime_days:.1f} days")
        except Exception as e:
            info_lines.append(f"Failed to get uptime: {e}")

        # Journal errors from the last periodic interval
        try:
            interval_seconds = CONFIG['PERIODIC_JOBS_INTERVAL'] + 5
            journal_errors = subprocess.check_output(
                ['journalctl', '-p', 'err', '--since', f"{interval_seconds} seconds ago", '--no-pager'],
                stderr=subprocess.STDOUT
            ).decode()
            if journal_errors.strip():
                info_lines.append(f"System errors from the last {interval_seconds} seconds:\n{journal_errors}")
            else:
                info_lines.append("No systen errors in the specified time period")
        except Exception as e:
            info_lines.append(f"Failed to get journal errors: {e}")
        info_lines.append("-------------------------------------------------------------------------")

        # Log all information at once
        logging.info('\n'.join(info_lines))

    @staticmethod
    def get_current_ip(interface: str = "wlan0") -> str:
        """Return the best-effort current IPv4 address (prefers ``interface``, else any)."""
        def _get_ip_address(ifname: str) -> str | None:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                return socket.inet_ntoa(fcntl.ioctl(
                    s.fileno(),
                    0x8915,  # SIOCGIFADDR
                    struct.pack('256s', ifname[:15].encode('utf-8'))
                )[20:24])
            except Exception:
                return None

        # 1) Keep legacy behavior: prefer requested interface (default: wlan0)
        if interface:
            ip_address = _get_ip_address(interface)
            if ip_address:
                return ip_address

        # 2) Try all available non-loopback interfaces (works for remote-mode hosts)
        try:
            for ifname in sorted(os.listdir('/sys/class/net')):
                if ifname == 'lo':
                    continue
                ip_address = _get_ip_address(ifname)
                if ip_address:
                    return ip_address
        except Exception:
            pass

        # 3) Routing-based fallback (best effort; no packets are sent)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_address = s.getsockname()[0]
            s.close()
            if ip_address and ip_address != "0.0.0.0":
                return ip_address
        except Exception:
            pass

        # 4) Hostname fallback
        try:
            ip_address = socket.gethostbyname(socket.gethostname())
            if ip_address and ip_address != "0.0.0.0":
                return ip_address
        except Exception:
            pass

        logging.error("Failed to determine current IP address. Falling back to 127.0.0.1")
        return "127.0.0.1"

    @staticmethod
    def is_port_open(port, host='localhost'):
        """Check if a port is open on the given host."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)  # 1 second timeout
                result = s.connect_ex((host, port))
                return result == 0  # If result is 0, port is open
        except:
            return False

class DateTimeUtil:
    """Timezone and UTC/local date formatting helpers."""

    @staticmethod
    def format_date_minmax(date: datetime, to_start=True):
        """Format ``date`` as start (00:00:00) or end (23:59:59) of that day."""
        dt_time = time.min if to_start else time.max
        return datetime.combine(date, dt_time).strftime('%Y-%m-%d %H:%M:%S')

    @staticmethod
    def get_timezone():
        """Return CONFIG timezone as ZoneInfo (falls back to UTC)."""
        try:
            timezone = ZoneInfo(CONFIG['TIMEZONE'])
        except Exception:
            logging.error(f"Unknown timezone '{CONFIG['TIMEZONE']}'. Falling back to UTC.")
            timezone = ZoneInfo('UTC')
        return timezone

    @staticmethod
    def get_utc_date_string(time: float):
        """Format a unix timestamp as a UTC datetime string with ``+00:00``."""
        # Convert the time to a datetime object in UTC
        utc_datetime = datetime.fromtimestamp(time, tz=timezone.utc)

        # Format the datetime object to the specified string format with UTC offset
        utc_date_string = utc_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-2] + "+00:00"

        return utc_date_string

    @staticmethod
    def get_local_date_from_utc_date(utc_date_string: str):
        """Convert a UTC datetime string to the configured local timezone string."""
        # Truncate the microseconds to 4 decimal places if necessary
        if '.' in utc_date_string:
            date_part, microseconds_part = utc_date_string.split('.')
            microseconds_part = microseconds_part[:4]
            utc_date_string = f"{date_part}.{microseconds_part}"

        # Convert the UTC date string to a datetime object
        utc_datetime = datetime.strptime(utc_date_string, '%Y-%m-%d %H:%M:%S.%f')

        # Convert the UTC datetime object to the local timezone
        local_datetime = utc_datetime.astimezone(DateTimeUtil.get_timezone())

        # Format the local datetime object to the specified string format
        local_date_string = local_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-2]

        return local_date_string

class ImageUtil:
    """Image resize/process helpers for thumbnails and overlays."""

    @staticmethod
    def resize_image_to_square(img: cv2.typing.MatLike, size: int = 800, quality: int = 85) -> bytes:
        """Center-crop to square, resize, and return JPEG bytes (or None on error)."""
        try:
            if img is not None:
                # Crop and resize the image to the specified size
                height, width, _ = img.shape
                if height > width:
                    diff = (height - width) // 2
                    img_cropped = img[diff:diff + width, :]
                else:
                    diff = (width - height) // 2
                    img_cropped = img[:, diff:diff + height]

                img_resized = cv2.resize(img_cropped, (size, size))
                # Encode the image to jpg format
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
                _, img_blob = cv2.imencode('.jpg', img_resized, encode_param)
                return img_blob.tobytes()
        except:
            return None

    @staticmethod
    def process_image(image_blob, target_width, target_height, quality):
        """Resize a JPEG blob to fit target size (aspect preserved) and re-encode."""
        try:
            # Convert the blob to a numpy array for OpenCV
            nparr = np.frombuffer(image_blob, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            # Resize the image while maintaining aspect ratio
            height, width = img.shape[:2]
            aspect = width / height
            if aspect > (target_width / target_height):
                new_width = target_width
                new_height = int(target_width / aspect)
            else:
                new_height = target_height
                new_width = int(target_height * aspect)
            resized = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_AREA)
            # Encode the resized image with reduced quality
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            _, encoded_img = cv2.imencode('.jpg', resized, encode_param)
            return encoded_img.tobytes()
        except Exception as e:
            logging.error(f"Failed to process image: {e}")
            return None

# ---------------------------------------------------------------------------
# Module-level helpers (not folded into a class yet)
# ---------------------------------------------------------------------------

def icon_svg_local(svg: str, margin_left: str | None = "auto", margin_right: str | None = "0.2em",) -> htmltools.TagChild:
    """Render a local ``icons/{svg}.svg`` as a theme-aware CSS-mask icon span."""
    # NOTE: <img src="...svg"> does not inherit `currentColor` from the page,
    # so it won't follow light/dark theme colors. Using an SVG as a CSS mask
    # makes it reliably tintable via `background-color: currentColor`.
    return htmltools.span(
        role="img",
        aria_label=svg,
        style=f"""
        display:inline-block;
        background-color: currentColor;
        height:1em;
        width:1em;
        -webkit-mask: url('icons/{svg}.svg') no-repeat center / contain;
        mask: url('icons/{svg}.svg') no-repeat center / contain;
        margin-left: {margin_left};
        margin-right: {margin_right};
        position:relative;
        vertical-align:-0.125em;
        overflow:visible;
        outline-width: 0px;
        margin-top: 0;
        margin-bottom: 0;
        """,
    )

def check_and_stop_kittyflap_services(simulate_operations=False):
    """Stop/mask legacy Kittyflap services and disable their executables."""

    # Remove 'manager' entries from the cron jobs
    try:
        # Check if the cron configuration contains 'manager' entries
        result = subprocess.run("crontab -l 2>/dev/null | grep 'manager'", shell=True, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout:
            # Remove 'manager' entries from the cron jobs
            subprocess.run("crontab -l 2>/dev/null | grep -v 'manager' | crontab -", shell=True, check=True)
            logging.info("Removed 'manager' entries from the cron jobs.")
        else:
            logging.info("No 'manager' entries found in the cron jobs.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to check or remove 'manager' entries from the cron jobs: {e}")

    services_to_manage = {
        'kwork': {'mask': False, 'delete': False},
        'manager': {'mask': True, 'delete': True},
        'setup': {'mask': True, 'delete': False},
        'mqtt': {'mask': True, 'delete': False}
    }

    # Stop and mask services
    # Check the 'delete' flag and remove the service file if set to True
    for service_name, options in services_to_manage.items():
        service_file = f"/etc/systemd/system/{service_name}.service"
        if options['delete'] and os.path.exists(service_file):
            try:
                os.remove(service_file)
                logging.info(f"Service file {service_file} deleted.")
            except Exception as e:
                logging.warning(f"Failed to delete service file {service_file}: {e}")
    for service_name, options in services_to_manage.items():
        if ServiceOps.is_service_running(service_name, simulate_operations):
            logging.warning(f"The {service_name} service is running! Stopping it now.")
            try:
                ServiceOps.systemctl("stop", service_name, simulate_operations)
                ServiceOps.systemctl("disable", service_name, simulate_operations)
            except Exception as e:
                logging.error(f"Failed to stop the {service_name} service: {e}")
        
        if options['mask'] and not ServiceOps.is_service_masked(service_name, simulate_operations):
            logging.warning(f"The {service_name} service is not masked! Masking it now.")
            try:
                ServiceOps.systemctl("mask", service_name, simulate_operations)
            except Exception as e:
                logging.warning(f"Failed to mask the {service_name} service: {e}")

    # Rename executables
    import glob

    executables_to_disable = {
        "/root/kittyflap_versions/*/manager",
        "/root/kittyflap_versions/*/dependencies",
        "/root/kittyflap_versions/latest/main",
        "/root/manager"
    }

    for pattern in executables_to_disable:
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                try:
                    os.rename(path, f"{path}_disabled")
                    logging.info(f"{os.path.basename(path)} executable renamed to {path}_disabled.")
                except Exception as e:
                    logging.error(f"Failed to rename {path}: {e}")
            else:
                logging.info(f"{os.path.basename(path)} executable not found. Skipping.")

def wait_for_network(timeout: int = 120) -> bool:
    """Wait until NTP is synced and outbound network works (or ``timeout`` seconds)."""
    interval = 1
    attempts = 0
    
    while attempts < timeout:
        try:
            # Check NTP synchronization status with timeout
            result = subprocess.run(['/usr/bin/timedatectl', 'status'], 
                                 capture_output=True, 
                                 text=True, 
                                 timeout=5)
            
            # Check if command was successful and contains sync info
            if result.returncode == 0 and 'System clock synchronized: yes' in result.stdout:
                # Test network connectivity
                socket.create_connection(("8.8.8.8", 53), timeout=1).close()
                logging.info("Network connectivity and time synchronization established")
                return True
            
        except (subprocess.TimeoutExpired, socket.error, subprocess.SubprocessError) as e:
            logging.debug(f"Network check attempt failed: {str(e)}")
        except Exception:
            pass
        
        attempts += interval
        tm.sleep(interval)
    logging.error(f"Failed to establish network connectivity after {timeout} seconds")
    return False

def check_allowed_to_exit():
    """True if global exit policy + configured time ranges allow leaving now."""
    from src.backend.decisions import time_in_exit_ranges

    # If exit is globally denied, return False immediately
    if CONFIG['ALLOWED_TO_EXIT'] == AllowedToExit.DENY:
        logging.info("[CAT_EXIT_CHECK] Not allowed to exit, as the setting is disabled.")
        return False

    # For ALLOW and CONFIGURE_PER_CAT we evaluate time ranges (if any). In per-cat mode,
    # this function provides the base schedule; per-cat filtering happens in backend.
    if CONFIG['ALLOWED_TO_EXIT'] in (AllowedToExit.ALLOW, AllowedToExit.CONFIGURE_PER_CAT):
        now = datetime.now(DateTimeUtil.get_timezone())
        current_time = now.strftime("%H:%M")

        ranges = [
            (
                CONFIG[f'ALLOWED_TO_EXIT_RANGE{i}'],
                CONFIG[f'ALLOWED_TO_EXIT_RANGE{i}_FROM'],
                CONFIG[f'ALLOWED_TO_EXIT_RANGE{i}_TO'],
            )
            for i in (1, 2, 3)
        ]
        allowed = time_in_exit_ranges(current_time, ranges)

        if allowed:
            logging.info("[CAT_EXIT_CHECK] Allowed to exit based on configured ranges.")
            return True
        else:
            logging.info("[CAT_EXIT_CHECK] Not allowed to exit based on configured ranges.")
            return False
    # Fallback safety
    return False

def is_valid_uuid4(s: str) -> bool:
    """True if ``s`` is a canonical lowercase UUID4 string."""
    try:
        val = uuid.UUID(s, version=4)
    except ValueError:
        return False
    return val.version == 4 and str(val) == s.lower()

# Compatibility aliases (preserve existing imports)
filter_release_notes_for_language = Versioning.filter_release_notes_for_language
get_git_version = Versioning.get_git_version
normalize_version = Versioning.normalize_version
is_same_kittyhack_version = Versioning.is_same_kittyhack_version
_parse_repo_spec = Versioning._parse_repo_spec
normalize_repo_spec = Versioning.normalize_repo_spec
check_custom_update_repo_reachable = Versioning.check_custom_update_repo_reachable
resolved_update_repo = Versioning.resolved_update_repo
read_latest_kittyhack_version = Versioning.read_latest_kittyhack_version
update_changes_runtime_files = Versioning.update_changes_runtime_files
update_changes_required_python = Versioning.update_changes_required_python
get_runtime_file_changes = Versioning.get_runtime_file_changes
fetch_github_release_notes = Versioning.fetch_github_release_notes
execute_update_step = Versioning.execute_update_step
list_changelogs = Versioning.list_changelogs
get_changelogs = Versioning.get_changelogs
parse_version = Versioning.parse_version
get_free_disk_space = SystemInfo.get_free_disk_space
get_total_disk_space = SystemInfo.get_total_disk_space
get_used_ram_space = SystemInfo.get_used_ram_space
get_total_ram_space = SystemInfo.get_total_ram_space
get_database_size = SystemInfo.get_database_size
get_file_size = SystemInfo.get_file_size
log_relevant_deb_packages = SystemInfo.log_relevant_deb_packages
log_system_information = SystemInfo.log_system_information
get_current_ip = SystemInfo.get_current_ip
is_port_open = SystemInfo.is_port_open
format_date_minmax = DateTimeUtil.format_date_minmax
get_timezone = DateTimeUtil.get_timezone
get_utc_date_string = DateTimeUtil.get_utc_date_string
get_local_date_from_utc_date = DateTimeUtil.get_local_date_from_utc_date
resize_image_to_square = ImageUtil.resize_image_to_square
process_image = ImageUtil.process_image
