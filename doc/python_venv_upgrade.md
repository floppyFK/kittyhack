# Python venv upgrade guard (from v2.7.0)

## Goal

Allow a future Kittyhack release to change the required Python version (e.g. 3.11 → 3.13)
without locking users out of the Web UI after an in-app update.

v2.8.0+ targets `setup/REQUIRED_PYTHON` = `3.12`. The venv dual-path (`--prepare` /
`--apply`) was introduced in v2.7.0 so devices can migrate without deleting the working
`.venv` first.

## Why not “recreate .venv inside Shiny on first start”?

Both `kittyhack.service` and `kittyhack_control.service` execute binaries from `.venv`.
Deleting or replacing that directory while the process is running is unsafe. Remote-mode
devices have no control supervisor, so a control-only fix is incomplete.

## Components

| File | Role |
|------|------|
| `setup/REQUIRED_PYTHON` | Single-line `X.Y` pin for this software version |
| `setup/ensure_venv.sh` | Dual-venv create / smoke / atomic swap |
| `setup/update_status_server.py` | Simple “Update in progress” page on port 80 during `--apply` |
| `setup/kittyhack.service` | `ExecStartPre=… --apply` + long `TimeoutStartSec` |
| `setup/kittyhack_control.service` | Same `ExecStartPre` (target boot path) |
| `KittyhackUpdater.update_kittyhack()` | Calls `--prepare` after checkout |

## Modes

1. **`--bootstrap`** — fresh install (`kittyhack-setup.sh`). Creates `.venv` in place.
2. **`--prepare`** — during software update while the old process may still be alive.
   On mismatch: build `.venv.new`, `pip install -r requirements.txt`, strict smoke test.
   Never swaps; leaves `.venv` untouched. Sets `.runtime-update-pending`.
3. **`--apply`** — `ExecStartPre` / after reboot. If work is needed, serves a non-technical
   status page on port 80 (“Update in progress… do not power off”), then either swaps
   `.venv.new` → `.venv` or repairs on mismatch. On failure, keeps the previous `.venv`
   and stops the status page.

## Update + reboot sequence (future Python bump)

1. User starts update → git checkout of new tag (with new `REQUIRED_PYTHON`).
2. `update_kittyhack` runs `ensure_venv.sh --prepare` → `.venv.new` on the new Python.
3. Existing pip-into-`.venv` step is skipped when `.venv.new` was prepared (deps already there).
4. Systemd unit files are refreshed (include `ExecStartPre`).
5. UI asks for reboot (existing behaviour).
6. On boot, `ExecStartPre --apply` shows the status page if needed, swaps `.venv.new` → `.venv`
   (rewriting embedded shebang paths), then the service starts and the normal UI comes back.

**Important:** systemd must start the app via `.venv/bin/python -m uvicorn …` (not `.venv/bin/uvicorn`).
Console scripts embed absolute shebangs that break when `.venv.new` is renamed to `.venv`.

## Lockout rules

- Never `rm -rf .venv` before a smoke-tested replacement exists.
- Prefer side-by-side `.venv.new` + atomic rename.
- Keep `.venv.old` for one generation after a successful swap (manual recovery via SSH).
  On the *next* Python migration, `--prepare` deletes `.venv.old` **before** creating
  `.venv.new`, so peak disk is `.venv` + `.venv.new` (not three full copies). After
  reboot `--apply` swaps and recreates `.venv.old` from the previous live `.venv`.
- `TimeoutStartSec=3600` so a cold torch download on a Pi does not get SIGKILL’d.
- File lock (`.venv-ensure.lock`) so control + kittyhack `ExecStartPre` do not race.
- Pip installs use `--no-cache-dir`; after a successful bootstrap / prepare / apply-swap
  (and after in-app updates), the pip download cache under `/root/.cache/pip` is purged.

## Before raising `REQUIRED_PYTHON` again

1. Confirm aarch64 + x86_64 wheels exist for torch/torchvision/ai-edge-litert/ncnn.
2. Refresh `requirements.txt` / `requirements_remote.txt` together and resolve pip conflicts on a Pi.
3. Bump `setup/REQUIRED_PYTHON` in the same release that needs the new interpreter.
4. Test: in-app update from previous tag → reboot → UI comes back; simulate failed pip and
   confirm old `.venv` remains bootable.
