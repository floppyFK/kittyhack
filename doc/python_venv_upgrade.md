# Python venv upgrade guard (from v2.7.0)

## Goal

Allow a future Kittyhack release to change the required Python version (e.g. 3.11 → 3.13)
without locking users out of the Web UI after an in-app update.

v2.7.0 ships `setup/REQUIRED_PYTHON` = `3.11`. On all existing installs the new checks are a
fast no-op; they establish the mechanism for a later bump.

## Why not “recreate .venv inside Shiny on first start”?

Both `kittyhack.service` and `kittyhack_control.service` execute binaries from `.venv`.
Deleting or replacing that directory while the process is running is unsafe. Remote-mode
devices have no control supervisor, so a control-only fix is incomplete.

## Components

| File | Role |
|------|------|
| `setup/REQUIRED_PYTHON` | Single-line `X.Y` pin for this software version |
| `setup/ensure_venv.sh` | Dual-venv create / smoke / atomic swap |
| `setup/kittyhack.service` | `ExecStartPre=… --apply` + long `TimeoutStartSec` |
| `setup/kittyhack_control.service` | Same `ExecStartPre` (target boot path) |
| `KittyhackUpdater.update_kittyhack()` | Calls `--prepare` after checkout |

## Modes

1. **`--bootstrap`** — fresh install (`kittyhack-setup.sh`). Creates `.venv` in place.
2. **`--prepare`** — during software update while the old process may still be alive.
   On mismatch: build `.venv.new`, `pip install -r requirements.txt`, strict smoke test.
   Never swaps; leaves `.venv` untouched.
3. **`--apply`** — `ExecStartPre` / after reboot. If `.venv.new` is ready → atomic swap
   (`.venv` → `.venv.old`, `.venv.new` → `.venv`). If mismatch and no `.venv.new` → repair
   the same way, then swap. On failure, keep the previous `.venv`.

## Update + reboot sequence (future Python bump)

1. User starts update → git checkout of new tag (with new `REQUIRED_PYTHON`).
2. `update_kittyhack` runs `ensure_venv.sh --prepare` → `.venv.new` on the new Python.
3. Existing pip-into-`.venv` step is skipped when `.venv.new` was prepared (deps already there).
4. Systemd unit files are refreshed (include `ExecStartPre`).
5. UI asks for reboot (existing behaviour).
6. On boot, `ExecStartPre --apply` swaps `.venv.new` → `.venv`, then the service starts.

## Lockout rules

- Never `rm -rf .venv` before a smoke-tested replacement exists.
- Prefer side-by-side `.venv.new` + atomic rename.
- Keep `.venv.old` for one generation (manual recovery via SSH).
- `TimeoutStartSec=3600` so a cold torch download on a Pi does not get SIGKILL’d.
- File lock (`.venv-ensure.lock`) so control + kittyhack `ExecStartPre` do not race.

## Before raising `REQUIRED_PYTHON`

1. Migrate `tflite-runtime` → `ai-edge-litert` (or another runtime with aarch64 wheels).
2. Refresh `requirements.txt` for the target Python; validate on Pi 4 + Debian x64.
3. Bump `setup/REQUIRED_PYTHON` in the same release that needs the new interpreter.
4. Test: in-app update from previous tag → reboot → UI comes back; simulate failed pip and
   confirm old `.venv` remains bootable.
