#!/usr/bin/env python3
"""Kittyhack target-device development helper.

Stdlib only — works with system Python even when the app venv is broken or
being swapped. Run from anywhere; the script locates the repo root itself.

Examples:
    python3 tools/dev.py --help
    python3 tools/dev.py --status
    python3 tools/dev.py --backup --name before-stats
    python3 tools/dev.py --restore
    python3 tools/dev.py --restore 20260829-002530
    python3 tools/dev.py --reset-default
    python3 tools/dev.py --checkout v2.7.0
    python3 tools/dev.py --checkout python3.14_statistics
    python3 tools/dev.py --restart
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
DEFAULT_BACKUP_DIR = Path("/root/kittyhack-dev-backups")

# Stop control first so its watchdog cannot respawn kittyhack.service.
STOP_ORDER = ("kittyhack_control.service", "kittyhack.service")
START_ORDER = ("kittyhack_control.service", "kittyhack.service")

# Copied as-is when present. The database is snapshotted via SQLite's backup
# API (consistent, no need to stop services). WAL/SHM files are not copied.
OPTIONAL_FILES = (
    "config.ini",
    "notifications.json",
    "api_tokens.json",
    "config.remote.ini",
    ".remote-mode",
)
DB_NAME = "kittyhack.db"


class DevError(Exception):
    """Expected, user-facing failure (printed without a traceback)."""


def log(msg: str) -> None:
    print(f"[khdev] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[khdev] WARNING: {msg}", flush=True, file=sys.stderr)


def die(msg: str, code: int = 1) -> int:
    print(f"[khdev] ERROR: {msg}", file=sys.stderr)
    return code


def pictures_dir() -> Path:
    override = os.environ.get("KITTYHACK_INSTALL_BASE")
    base = Path(override).resolve() if override else ROOT.parent
    return base / "pictures"


def backup_dir_from_args(args: argparse.Namespace) -> Path:
    raw = getattr(args, "backup_dir", None) or os.environ.get("KITTYHACK_DEV_BACKUP_DIR")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_BACKUP_DIR


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(cmd),
        cwd=str(cwd or ROOT),
        check=False,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise DevError(f"command failed ({result.returncode}): {' '.join(cmd)}" + (f"\n{err}" if err else ""))
    return result


def git(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["/usr/bin/git", *args], check=check, capture=capture)


def git_out(*args: str) -> str:
    return git(*args).stdout.strip()


def git_ok(*args: str) -> bool:
    return git(*args, check=False).returncode == 0


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def human_size(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise DevError("refusing destructive action without --yes (stdin is not a TTY)")
    reply = input(f"[khdev] {prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def copy_if_exists(src: Path, dest: Path) -> bool:
    if not src.exists() or not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def sqlite_backup(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dest), timeout=30)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
            log(f"removed stale {sidecar.name}")


def service_active(name: str) -> bool:
    result = run(
        ["/usr/bin/systemctl", "is-active", "--quiet", name],
        check=False,
        capture=True,
        timeout=10,
    )
    return result.returncode == 0


def systemctl(mode: str, service: str, timeout: float = 45.0) -> None:
    log(f"systemctl {mode} {service}")
    run(["/usr/bin/systemctl", mode, service], timeout=timeout, capture=True)


def stop_services() -> None:
    for name in STOP_ORDER:
        if service_active(name):
            systemctl("stop", name)
        else:
            log(f"{name} already stopped")


def start_services() -> None:
    for name in START_ORDER:
        systemctl("start", name)


def restart_services() -> None:
    stop_services()
    start_services()
    print_service_status()


def maybe_restart(args: argparse.Namespace) -> None:
    if getattr(args, "no_restart", False):
        log("skipping service restart (--no-restart)")
        return
    restart_services()


def print_service_status() -> None:
    for name in START_ORDER:
        state = "active" if service_active(name) else "inactive"
        log(f"{name}: {state}")


def git_state() -> dict[str, str | bool]:
    branch = git_out("rev-parse", "--abbrev-ref", "HEAD")
    head = git_out("rev-parse", "HEAD")
    short = git_out("rev-parse", "--short", "HEAD")
    describe = git("describe", "--tags", "--always", "--dirty", check=False).stdout.strip() or short
    porcelain = git_out("status", "--porcelain")
    return {
        "branch": branch,
        "head": head,
        "short": short,
        "describe": describe,
        "dirty": bool(porcelain),
        "porcelain": porcelain,
    }


def snapshot_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    dirs = [p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return sorted(dirs, key=lambda p: p.name)


def _is_snapshot_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "manifest.json").exists():
        return True
    return any((path / f).exists() for f in (*OPTIONAL_FILES, DB_NAME))


def _snapshot_archives(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.tar.gz") if p.is_file())


def resolve_snapshot(base: Path, name: str) -> Path:
    if name in ("", "latest"):
        dirs = snapshot_dirs(base)
        archives = _snapshot_archives(base)
        newest_dir = dirs[-1] if dirs else None
        newest_arch = archives[-1] if archives else None
        if newest_dir is None and newest_arch is None:
            raise DevError(f"no snapshots in {base}")
        if newest_dir is None:
            return newest_arch
        if newest_arch is None:
            return newest_dir
        return newest_dir if newest_dir.name >= newest_arch.name else newest_arch

    candidate = Path(name).expanduser()
    suffixes = candidate.suffixes
    if candidate.is_file() and (suffixes[-2:] == [".tar", ".gz"] or candidate.suffix == ".tgz"):
        return candidate.resolve()
    if _is_snapshot_dir(candidate):
        return candidate.resolve()

    exact = base / name
    if exact.is_dir() or exact.is_file():
        return exact

    matches = [p for p in snapshot_dirs(base) + _snapshot_archives(base) if p.name.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = ", ".join(p.name for p in matches)
        raise DevError(f"ambiguous snapshot '{name}': {listed}")
    raise DevError(f"snapshot not found: {name} (looked in {base})")


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def collect_persistent(dest: Path, *, with_pictures: bool) -> list[str]:
    stored: list[str] = []
    for name in OPTIONAL_FILES:
        if copy_if_exists(ROOT / name, dest / name):
            stored.append(name)
            log(f"copied {name}")
        else:
            log(f"skip {name} (not present)")

    db_src = ROOT / DB_NAME
    if db_src.exists():
        log(f"sqlite backup {DB_NAME} …")
        sqlite_backup(db_src, dest / DB_NAME)
        stored.append(DB_NAME)
        log(f"wrote {DB_NAME} ({human_size((dest / DB_NAME).stat().st_size)})")
    else:
        log(f"skip {DB_NAME} (not present)")

    pics = pictures_dir()
    if with_pictures:
        if pics.is_dir():
            log(f"copying pictures from {pics} …")
            shutil.copytree(pics, dest / "pictures", dirs_exist_ok=True, symlinks=True)
            stored.append("pictures/")
            log(f"pictures copied ({human_size(dir_size(dest / 'pictures'))})")
        else:
            log(f"skip pictures (not present at {pics})")
    elif pics.is_dir():
        log(f"pictures not included ({human_size(dir_size(pics))} at {pics}; pass --with-pictures)")

    return stored


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def write_git_sidecar(dest: Path, state: dict[str, str | bool]) -> None:
    (dest / "git.head").write_text(str(state["head"]) + "\n", encoding="utf-8")
    (dest / "git.status").write_text(
        git("status", "-sb", check=False).stdout,
        encoding="utf-8",
    )
    diff = git("diff", check=False).stdout
    staged = git("diff", "--cached", check=False).stdout
    if diff:
        (dest / "git.diff").write_text(diff, encoding="utf-8")
    if staged:
        (dest / "git.diff.cached").write_text(staged, encoding="utf-8")


# --- commands -----------------------------------------------------------------

def cmd_backup(args: argparse.Namespace) -> int:
    base = backup_dir_from_args(args)
    base.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    label = (args.name or "").strip().replace("/", "-").replace(" ", "_")
    folder = f"{stamp}_{label}" if label else stamp
    dest = base / folder
    dest.mkdir(parents=False, exist_ok=False)
    log(f"creating snapshot {dest}")

    try:
        os.chdir(ROOT)
        state = git_state()
        stored = collect_persistent(dest, with_pictures=args.with_pictures)
        write_git_sidecar(dest, state)
        manifest = {
            "created": iso_now(),
            "hostname": socket.gethostname(),
            "repo": str(ROOT),
            "git": {k: state[k] for k in ("branch", "head", "short", "describe", "dirty")},
            "files": stored,
            "with_pictures": bool(args.with_pictures),
        }
        write_json(dest / "manifest.json", manifest)

        if args.archive:
            archive = dest.with_suffix(".tar.gz")
            log(f"writing {archive.name} …")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(dest, arcname=dest.name)
            shutil.rmtree(dest)
            log(f"snapshot archive: {archive} ({human_size(archive.stat().st_size)})")
        else:
            log(f"snapshot ready: {dest} ({human_size(dir_size(dest))})")
            log(f"restore with: python3 tools/dev.py --restore {dest.name}")
    except Exception:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise
    return 0


def cmd_list_backups(args: argparse.Namespace) -> int:
    base = backup_dir_from_args(args)
    dirs = snapshot_dirs(base)
    archives = _snapshot_archives(base)
    if not dirs and not archives:
        log(f"no snapshots in {base}")
        return 0
    log(f"snapshots in {base}:")
    for path in dirs:
        size = human_size(dir_size(path))
        extra = ""
        manifest_path = path / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                git_info = manifest.get("git") or {}
                extra = f"  git={git_info.get('describe', '?')} dirty={git_info.get('dirty', '?')}"
            except json.JSONDecodeError:
                extra = "  (invalid manifest)"
        print(f"  {path.name:40}  {size:>10}{extra}")
    for path in archives:
        print(f"  {path.name:40}  {human_size(path.stat().st_size):>10}  (archive)")
    return 0


def _extract_archive_if_needed(path: Path) -> Path:
    if path.is_dir():
        return path
    if path.suffixes[-2:] == [".tar", ".gz"] or path.suffix == ".tgz":
        tmp = Path(tempfile.mkdtemp(prefix="khdev-restore-"))
        with tarfile.open(path, "r:gz") as tar:
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:
                tar.extractall(tmp)
        inner = [p for p in tmp.iterdir() if p.is_dir()]
        if len(inner) == 1:
            return inner[0]
        return tmp
    raise DevError(f"not a snapshot directory or .tar.gz: {path}")


def cmd_restore(args: argparse.Namespace) -> int:
    base = backup_dir_from_args(args)
    src = resolve_snapshot(base, args.restore or "latest")
    if src.is_file():
        src = _extract_archive_if_needed(src)
    log(f"restoring from {src}")

    has_pictures = (src / "pictures").is_dir()
    if has_pictures and not args.with_pictures:
        log("snapshot contains pictures/; pass --with-pictures to restore them too")

    if not confirm(f"Restore persistent files from '{src.name}' onto {ROOT}?", args.yes):
        log("aborted")
        return 1

    stop_services()
    try:
        restored: list[str] = []
        for name in OPTIONAL_FILES:
            src_file = src / name
            if src_file.is_file():
                shutil.copy2(src_file, ROOT / name)
                restored.append(name)
                log(f"restored {name}")

        src_db = src / DB_NAME
        if src_db.is_file():
            dest_db = ROOT / DB_NAME
            shutil.copy2(src_db, dest_db)
            remove_sqlite_sidecars(dest_db)
            restored.append(DB_NAME)
            log(f"restored {DB_NAME}")

        if args.with_pictures and has_pictures:
            dest_pics = pictures_dir()
            log(f"restoring pictures to {dest_pics} …")
            if dest_pics.exists():
                shutil.rmtree(dest_pics)
            shutil.copytree(src / "pictures", dest_pics, symlinks=True)
            restored.append("pictures/")
            log("restored pictures/")

        if not restored:
            raise DevError("snapshot contained none of the expected files")
        log("restored: " + ", ".join(restored))
    except Exception:
        warn("restore failed; services are still stopped")
        raise

    maybe_restart(args)
    return 0


def cmd_delete_backup(args: argparse.Namespace) -> int:
    base = backup_dir_from_args(args)
    src = resolve_snapshot(base, args.delete_backup)
    if not confirm(f"Delete snapshot '{src}'?", args.yes):
        log("aborted")
        return 1
    if src.is_dir():
        shutil.rmtree(src)
    else:
        src.unlink()
    log(f"deleted {src}")
    return 0


def cmd_reset_default(args: argparse.Namespace) -> int:
    config = ROOT / "config.ini"
    if not confirm("Reset config.ini to factory defaults? (current file is copied aside first)", args.yes):
        log("aborted")
        return 1

    stop_services()
    try:
        if config.exists():
            bak = ROOT / f"config.ini.bak-{now_stamp()}"
            shutil.copy2(config, bak)
            log(f"current config saved as {bak.name}")
            config.unlink()
            log("removed config.ini — kittyhack will recreate defaults on start")
        else:
            log("config.ini already absent; next start will create defaults")
    except Exception:
        warn("reset failed; services are still stopped")
        raise

    maybe_restart(args)
    return 0


def cmd_reset_db(args: argparse.Namespace) -> int:
    db = ROOT / DB_NAME
    if not confirm(
        "Wipe kittyhack.db (cats, events, photos metadata)? Current DB is copied aside first.",
        args.yes,
    ):
        log("aborted")
        return 1

    stop_services()
    try:
        if db.exists():
            bak = ROOT / f"kittyhack.db.bak-{now_stamp()}"
            sqlite_backup(db, bak)
            log(f"current database saved as {bak.name}")
            db.unlink()
            remove_sqlite_sidecars(db)
            log(f"removed {DB_NAME} — kittyhack will create an empty DB on start")
        else:
            log(f"{DB_NAME} already absent")
            remove_sqlite_sidecars(db)
    except Exception:
        warn("reset-db failed; services are still stopped")
        raise

    maybe_restart(args)
    return 0


def _resolve_checkout_ref(ref: str) -> tuple[list[str], str]:
    """Return (git checkout argv without 'git', description)."""
    if git_ok("rev-parse", "--verify", "--quiet", ref):
        return (["checkout", ref], ref)
    remote = f"origin/{ref}"
    if git_ok("rev-parse", "--verify", "--quiet", remote):
        return (["checkout", "-B", ref, remote], f"{ref} tracking {remote}")
    raise DevError(
        f"unknown ref '{ref}'. Try --fetch first, or pass a tag / branch / commit. "
        "Use --versions to list tags."
    )


def cmd_checkout(args: argparse.Namespace) -> int:
    ref = args.checkout.strip()
    if not ref:
        raise DevError("empty --checkout ref")

    log("fetching remotes and tags …")
    git("fetch", "--all", "--tags", capture=False)

    state = git_state()
    if state["dirty"] and not args.force and not args.stash:
        print(state["porcelain"], file=sys.stderr)
        raise DevError(
            "working tree is dirty. Commit/stash your changes, or pass --stash "
            "(git stash -u) or --force (discard tracked changes)."
        )

    checkout_args, description = _resolve_checkout_ref(ref)
    if not confirm(f"Check out {description} in {ROOT}?", args.yes):
        log("aborted")
        return 1

    stop_services()
    try:
        if args.stash and state["dirty"]:
            log("stashing local changes (including untracked) …")
            git("stash", "push", "-u", "-m", f"khdev checkout {now_stamp()} {ref}", capture=False)

        if args.force and not args.stash:
            checkout_args = ["checkout", "--force", *checkout_args[1:]]

        log(f"git {' '.join(checkout_args)}")
        git(*checkout_args, capture=False)

        new_state = git_state()
        log(f"HEAD is now {new_state['describe']} (branch {new_state['branch']})")
        if new_state["branch"] == "HEAD":
            log("detached HEAD — normal when checking out a tag or commit")

        if not args.no_deps:
            ensure_script = ROOT / "setup" / "ensure_venv.sh"
            req = ROOT / "requirements.txt"
            if ensure_script.is_file():
                cmd = ["/bin/bash", str(ensure_script), "--prepare", "--root", str(ROOT)]
                if req.is_file():
                    cmd.extend(["--requirements", str(req)])
                log("running ensure_venv.sh --prepare (no-op if Python/venv already match) …")
                run(cmd, capture=False)
            else:
                warn(f"{ensure_script} not found; skipping venv prepare")
        else:
            log("skipping ensure_venv (--no-deps)")
    except Exception:
        warn("checkout failed; services are still stopped")
        raise

    maybe_restart(args)
    return 0


def host_ips() -> list[str]:
    ips: list[str] = []
    try:
        ips.extend(subprocess.check_output(["hostname", "-I"], text=True).split())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    non_loop = [ip for ip in ips if not ip.startswith("127.")]
    return non_loop or ips


def cmd_status(_args: argparse.Namespace) -> int:
    os.chdir(ROOT)
    state = git_state()
    host = socket.gethostname()
    addrs = host_ips()

    print(f"host:       {host}")
    print(f"ips:        {', '.join(addrs) or '?'}")
    print(f"ui:         http://{addrs[0] if addrs else host}/")
    print(f"repo:       {ROOT}")
    print(f"git:        {state['describe']}")
    print(f"branch:     {state['branch']}")
    print(f"head:       {state['short']}")
    print(f"dirty:      {state['dirty']}")
    print_service_status()

    req_py = ROOT / "setup" / "REQUIRED_PYTHON"
    if req_py.is_file():
        print(f"required:   Python {req_py.read_text(encoding='utf-8').strip()}")
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.is_file():
        ver = run([str(venv_py), "--version"], capture=True, check=False).stdout.strip()
        print(f"venv:       {ver or venv_py}")
    else:
        print("venv:       missing")

    usage = shutil.disk_usage(ROOT)
    print(f"disk:       {human_size(usage.free)} free / {human_size(usage.total)} total")

    print("persistent:")
    for name in (*OPTIONAL_FILES, DB_NAME):
        path = ROOT / name
        if path.exists():
            print(f"  {name:22} {human_size(path.stat().st_size):>10}")
        else:
            print(f"  {name:22} {'(absent)':>10}")
    pics = pictures_dir()
    if pics.is_dir():
        print(f"  {'pictures/':22} {human_size(dir_size(pics)):>10}  {pics}")
    else:
        print(f"  {'pictures/':22} {'(absent)':>10}  {pics}")

    if state["dirty"]:
        print("git status:")
        print(state["porcelain"])
    return 0


def cmd_restart(_args: argparse.Namespace) -> int:
    restart_services()
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    stop_services()
    print_service_status()
    return 0


def cmd_start(_args: argparse.Namespace) -> int:
    start_services()
    print_service_status()
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    cmd = [
        "/usr/bin/journalctl",
        "-u", "kittyhack.service",
        "-u", "kittyhack_control.service",
        "-n", str(args.lines if args.lines is not None else 80),
        "--no-pager",
    ]
    if args.follow:
        cmd.append("-f")
        log("following journal (Ctrl+C to stop)")
    run(cmd, capture=False, check=False)
    return 0


def cmd_fetch(_args: argparse.Namespace) -> int:
    log("git fetch --all --tags")
    git("fetch", "--all", "--tags", capture=False)
    log("done")
    return 0


def cmd_git_status(_args: argparse.Namespace) -> int:
    git("status", "-sb", capture=False)
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    result = git("tag", "-l", "v*", "--sort=-v:refname")
    tags = [line for line in result.stdout.splitlines() if line.strip()]
    limit = args.lines if args.lines is not None else 25
    shown = tags[:limit]
    log(f"latest {len(shown)} of {len(tags)} version tags:")
    current = git("describe", "--tags", "--exact-match", check=False).stdout.strip()
    for tag in shown:
        mark = "  <-- HEAD" if tag == current else ""
        print(f"  {tag}{mark}")
    if len(tags) > limit:
        print(f"  … {len(tags) - limit} more (raise --lines)")
    return 0


def cmd_compile_locales(_args: argparse.Namespace) -> int:
    msgfmt = shutil.which("msgfmt")
    if not msgfmt:
        raise DevError("msgfmt not found (install gettext)")
    compiled = 0
    for lang in ("de", "en"):
        po = ROOT / "locales" / lang / "LC_MESSAGES" / "messages.po"
        mo = ROOT / "locales" / lang / "LC_MESSAGES" / "messages.mo"
        if not po.is_file():
            warn(f"missing {po}")
            continue
        log(f"msgfmt {po.relative_to(ROOT)}")
        run([msgfmt, "-o", str(mo), str(po)], capture=True)
        compiled += 1
    log(f"compiled {compiled} locale(s)")
    return 0


def cmd_db_check(_args: argparse.Namespace) -> int:
    db = ROOT / DB_NAME
    if not db.exists():
        raise DevError(f"{db} not found")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    result = row[0] if row else "unknown"
    if result == "ok":
        log(f"{DB_NAME}: integrity_check ok")
        return 0
    print(result)
    return 1


def cmd_ensure_venv(_args: argparse.Namespace) -> int:
    script = ROOT / "setup" / "ensure_venv.sh"
    req = ROOT / "requirements.txt"
    if not script.is_file():
        raise DevError(f"{script} not found")
    cmd = ["/bin/bash", str(script), "--prepare", "--root", str(ROOT)]
    if req.is_file():
        cmd.extend(["--requirements", str(req)])
    log("ensure_venv.sh --prepare …")
    run(cmd, capture=False)
    log("done. ExecStartPre --apply runs on the next service start.")
    return 0


def cmd_reboot(args: argparse.Namespace) -> int:
    if not confirm("Reboot this Kittyflap now?", args.yes):
        log("aborted")
        return 1
    log("rebooting")
    run(["/sbin/reboot"], check=False)
    return 0


def cmd_show_config(_args: argparse.Namespace) -> int:
    config = ROOT / "config.ini"
    if not config.exists():
        raise DevError("config.ini not found")
    text = config.read_text(encoding="utf-8")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        print()
    return 0


# --- argparse -----------------------------------------------------------------

COMMANDS: dict[str, str] = {
    "backup": "Create a timestamped snapshot of config, database, tokens, notifications",
    "restore": "Restore a snapshot (NAME, path, or omit for latest)",
    "list_backups": "List snapshots in the backup directory",
    "delete_backup": "Delete a snapshot by name",
    "reset_default": "Reset config.ini to factory defaults (current file is copied aside)",
    "reset_db": "Wipe kittyhack.db (copied aside first)",
    "checkout": "git fetch + checkout a tag, branch, or commit, then restart",
    "status": "Show git HEAD, services, disk, venv, and persistent files",
    "restart": "Restart kittyhack_control and kittyhack",
    "stop": "Stop both services (control first, so it cannot respawn kittyhack)",
    "start": "Start both services",
    "logs": "Show journalctl for both services",
    "fetch": "git fetch --all --tags",
    "git_status": "Short git status",
    "versions": "List recent version tags",
    "compile_locales": "Compile locales/*/LC_MESSAGES/messages.po → .mo",
    "db_check": "PRAGMA integrity_check on kittyhack.db",
    "ensure_venv": "Run setup/ensure_venv.sh --prepare",
    "reboot": "Reboot the device",
    "show_config": "Print config.ini",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev.py",
        description="Development helper for a Kittyhack target device (backup, restore, git, services).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s --status
  %(prog)s --backup --name before-experiment
  %(prog)s --backup --with-pictures --archive
  %(prog)s --list-backups
  %(prog)s --restore                  # latest snapshot
  %(prog)s --restore 20260829-002530
  %(prog)s --reset-default --yes
  %(prog)s --checkout v2.7.0
  %(prog)s --checkout python3.14_statistics --stash
  %(prog)s --restart
  %(prog)s --logs --follow

snapshots land in /root/kittyhack-dev-backups (outside the git repo, so
production updates that run `git clean -fd` cannot delete them). Override
with --backup-dir or $KITTYHACK_DEV_BACKUP_DIR.

--checkout does NOT wipe the working tree the way the in-app updater does.
Dirty trees are refused unless you pass --stash or --force.
""",
    )

    cmds = parser.add_argument_group("commands (exactly one required)")
    exclusive = cmds.add_mutually_exclusive_group(required=True)
    exclusive.add_argument("--backup", action="store_true", help=COMMANDS["backup"])
    exclusive.add_argument(
        "--restore",
        nargs="?",
        const="latest",
        metavar="NAME",
        help=COMMANDS["restore"],
    )
    exclusive.add_argument("--list-backups", action="store_true", help=COMMANDS["list_backups"])
    exclusive.add_argument("--delete-backup", metavar="NAME", help=COMMANDS["delete_backup"])
    exclusive.add_argument("--reset-default", action="store_true", help=COMMANDS["reset_default"])
    exclusive.add_argument("--reset-db", action="store_true", help=COMMANDS["reset_db"])
    exclusive.add_argument("--checkout", metavar="REF", help=COMMANDS["checkout"])
    exclusive.add_argument("--status", action="store_true", help=COMMANDS["status"])
    exclusive.add_argument("--restart", action="store_true", help=COMMANDS["restart"])
    exclusive.add_argument("--stop", action="store_true", help=COMMANDS["stop"])
    exclusive.add_argument("--start", action="store_true", help=COMMANDS["start"])
    exclusive.add_argument("--logs", action="store_true", help=COMMANDS["logs"])
    exclusive.add_argument("--fetch", action="store_true", help=COMMANDS["fetch"])
    exclusive.add_argument("--git-status", action="store_true", help=COMMANDS["git_status"])
    exclusive.add_argument("--versions", action="store_true", help=COMMANDS["versions"])
    exclusive.add_argument("--compile-locales", action="store_true", help=COMMANDS["compile_locales"])
    exclusive.add_argument("--db-check", action="store_true", help=COMMANDS["db_check"])
    exclusive.add_argument("--ensure-venv", action="store_true", help=COMMANDS["ensure_venv"])
    exclusive.add_argument("--reboot", action="store_true", help=COMMANDS["reboot"])
    exclusive.add_argument("--show-config", action="store_true", help=COMMANDS["show_config"])

    opts = parser.add_argument_group("options")
    opts.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    opts.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart services after restore / reset / checkout",
    )
    opts.add_argument(
        "--force",
        action="store_true",
        help="checkout: discard local tracked changes (git checkout --force)",
    )
    opts.add_argument(
        "--stash",
        action="store_true",
        help="checkout: git stash push -u before switching",
    )
    opts.add_argument(
        "--no-deps",
        action="store_true",
        help="checkout: skip setup/ensure_venv.sh --prepare",
    )
    opts.add_argument(
        "--with-pictures",
        action="store_true",
        help="backup/restore: include /root/pictures (originals + thumbnails)",
    )
    opts.add_argument("--name", metavar="LABEL", help="backup: extra label in the snapshot directory name")
    opts.add_argument(
        "--archive",
        action="store_true",
        help="backup: pack the snapshot as NAME.tar.gz",
    )
    opts.add_argument(
        "--backup-dir",
        metavar="DIR",
        help=f"Snapshot directory (default: {DEFAULT_BACKUP_DIR})",
    )
    opts.add_argument("--follow", action="store_true", help="logs: follow journalctl (-f)")
    opts.add_argument(
        "--lines",
        type=int,
        default=None,
        metavar="N",
        help="logs: journal lines (default 80). versions: tag count (default 25)",
    )
    return parser


def dispatch(args: argparse.Namespace) -> int:
    if args.backup:
        return cmd_backup(args)
    if args.restore is not None:
        return cmd_restore(args)
    if args.list_backups:
        return cmd_list_backups(args)
    if args.delete_backup:
        return cmd_delete_backup(args)
    if args.reset_default:
        return cmd_reset_default(args)
    if args.reset_db:
        return cmd_reset_db(args)
    if args.checkout:
        return cmd_checkout(args)
    if args.status:
        return cmd_status(args)
    if args.restart:
        return cmd_restart(args)
    if args.stop:
        return cmd_stop(args)
    if args.start:
        return cmd_start(args)
    if args.logs:
        return cmd_logs(args)
    if args.fetch:
        return cmd_fetch(args)
    if args.git_status:
        return cmd_git_status(args)
    if args.versions:
        return cmd_versions(args)
    if args.compile_locales:
        return cmd_compile_locales(args)
    if args.db_check:
        return cmd_db_check(args)
    if args.ensure_venv:
        return cmd_ensure_venv(args)
    if args.reboot:
        return cmd_reboot(args)
    if args.show_config:
        return cmd_show_config(args)
    return 2


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    os.chdir(ROOT)
    try:
        return dispatch(args)
    except DevError as exc:
        return die(str(exc))
    except KeyboardInterrupt:
        print()
        return die("interrupted")
    except subprocess.TimeoutExpired as exc:
        return die(f"timed out: {exc.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
