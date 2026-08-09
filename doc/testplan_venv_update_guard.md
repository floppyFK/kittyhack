# Test plan: Python venv update guard (from v2.7.0)

Use this on real target hardware (Raspberry Pi 4 / Raspberry Pi OS) and, if you use it, on a Debian x64 remote-controller host.

Goal: prove that future releases can change the runtime safely, without lockout, and that v2.7.0 itself stays a fast no-op for normal updates.

Related code:

- `setup/REQUIRED_PYTHON`
- `setup/ensure_venv.sh`
- `setup/update_status_server.py`
- `setup/kittyhack.service` / `setup/kittyhack_control.service` (`ExecStartPre`)
- `KittyhackUpdater.update_kittyhack()` in `src/system.py`
- Design notes: `doc/python_venv_upgrade.md`

---

## 0. Preparation

### 0.1 Devices

Test at least:

1. **Target device** (Kittyflap / Pi) in normal target mode
2. Optional but recommended: **remote-mode** host (Debian x64) if you ship remote installs

### 0.2 Baseline snapshot (before any risky test)

On each device:

```bash
cd /root/kittyhack   # or your install path
git describe --tags --always
.venv/bin/python -V
.venv/bin/python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
systemctl cat kittyhack.service
systemctl cat kittyhack_control.service   # target mode only
ls -la setup/REQUIRED_PYTHON setup/ensure_venv.sh setup/update_status_server.py
```

Also copy aside (USB/SSH):

- `config.ini` (and `config.remote.ini` if present)
- `kittyhack.db`
- `api_tokens.json` / `notifications.json` if used

### 0.3 Useful watch commands

In a second SSH session:

```bash
journalctl -u kittyhack -u kittyhack_control -f --no-pager
# or:
tail -f /root/kittyhack/kittyhack.log
```

---

## 1. Fresh / updated v2.7.0 baseline (must be a no-op)

These tests confirm shipping the mechanism with `REQUIRED_PYTHON=3.11` does not slow or break normal boots.

### 1.1 Unit files contain ExecStartPre

```bash
systemctl cat kittyhack.service | grep -E 'ExecStartPre|TimeoutStartSec'
systemctl cat kittyhack_control.service | grep -E 'ExecStartPre|TimeoutStartSec'   # target mode
```

**Expect:**

- `ExecStartPre=.../setup/ensure_venv.sh --apply --root ...`
- `TimeoutStartSec=3600`

If missing after installing v2.7.0: reboot once after update (units are written during update; ExecStartPre applies on next start).

### 1.2 Normal service restart is fast

```bash
# Target mode:
sudo systemctl restart kittyhack_control.service
# Remote mode:
sudo systemctl restart kittyhack.service
```

**Expect:**

- Service becomes active within a few seconds
- Journal shows `[ensure_venv] apply: ... already on Python 3.11` (or equivalent “OK / nothing to do”)
- **No** long pip install
- **No** status page on port 80 (UI comes up normally)
- Web UI loads as usual

### 1.3 Marker files absent after healthy boot

```bash
ls -la /root/kittyhack/.runtime-update-pending \
       /root/kittyhack/.update-status-server.pid \
       /root/kittyhack/.venv.new 2>/dev/null || true
```

**Expect:** none of these should remain after a normal boot.

---

## 2. In-app software update path (v2.7.0 → v2.7.0 re-update / next patch)

### 2.1 Requirements-only change (Python unchanged)

1. On a test branch/tag based on v2.7.0, change only a harmless pin in `requirements.txt` (or bump a safe package).
2. Publish/install that version via the normal Info-tab update.
3. Watch update progress in the UI and journal.

**Expect during update (before reboot):**

- Step “Ensuring required Python runtime” is quick (no `.venv.new`)
- Step “Updating python dependencies” runs `pip install -r requirements.txt` into the **live** `.venv`
- Systemd unit files are rewritten (`Updating systemd service file` / `daemon-reload`)
- UI offers reboot as today

**Expect after reboot:**

- Fast `[ensure_venv] apply ... OK`
- No status page
- New package version present:

```bash
.venv/bin/pip show <package> | grep -i version
```

### 2.2 Deferred update from kittyhack_control (target / remote-control update)

If you use remote→target update:

1. Trigger target update from the remote UI.
2. Confirm journal on the **target** still writes unit files + `daemon-reload` even when enable/start is deferred.
3. Reboot target when prompted.

**Expect:**

- After reboot, units still have `ExecStartPre`
- UI returns
- No leftover `.venv.new`

---

## 3. Simulated future Python migration (core test)

Do **not** change production `REQUIRED_PYTHON` permanently on a device you need. Use a disposable test device or restore from backup afterwards.

### 3.1 Simulate / use the Python 3.12 stack

This branch/release targets `REQUIRED_PYTHON=3.12` with the updated `requirements*.txt`.

If you only need to re-run prepare/apply on an already-checked-out tree:

```bash
cd /root/kittyhack
cat setup/REQUIRED_PYTHON   # expect 3.12
sudo bash setup/ensure_venv.sh --prepare --root /root/kittyhack
```

Notes:

- Prefer a Python version that is installable on that OS (`apt` or `uv`).
- This test may download large wheels (torch). Plan 20–60+ minutes on a Pi and a stable network.
- Pip is forced to **PyPI only** (piwheels disabled) inside `ensure_venv.sh`.

### 3.2 Run `--prepare` (update-time path)

```bash
sudo systemctl stop kittyhack.service 2>/dev/null || true
# On target mode you may leave control running, or stop both for a cleaner test:
# sudo systemctl stop kittyhack_control.service

sudo bash setup/ensure_venv.sh --prepare --root /root/kittyhack
```

**Expect:**

- Journal/log: mismatch detected, creating `.venv.new`
- `.venv` (old) still present and unchanged in Python version
- `.venv.new` created with the new Python
- `.runtime-update-pending` created
- Live UI, if still on old `.venv`, continues to work until you reboot/swap

Verify:

```bash
.venv/bin/python -V
.venv.new/bin/python -V
test -f .runtime-update-pending && echo pending_ok
```

### 3.3 Confirm live `.venv` was not deleted

```bash
ls -ld .venv .venv.new
.venv/bin/python -c "import shiny, cv2; print('old venv imports ok')"
```

**Expect:** old venv still imports; device would still be recoverable if you aborted here.

### 3.4 Run `--apply` and verify status page

Start apply while watching from a browser on another device:

```bash
# Ensure port 80 is free:
sudo systemctl stop kittyhack.service kittyhack_control.service 2>/dev/null || true

# From a PC browser open: http://<device-ip>/
# Then in SSH:
sudo bash setup/ensure_venv.sh --apply --root /root/kittyhack
```

**Expect:**

1. Browser shows the friendly page:
   - EN: “Update in progress…” / do not switch off
   - DE: “Aktualisierung läuft…” (if `language = de` in `config.ini`)
2. Page text does **not** mention Python/venv/pip
3. Page auto-refreshes
4. After apply finishes:
   - Status page stops
   - `.venv` is the new Python
   - `.venv.new` is gone
   - `.venv.old` exists (previous runtime)
   - `.runtime-update-pending` is gone
   - `.update-status-server.pid` is gone

```bash
.venv/bin/python -V
ls -ld .venv .venv.old 2>/dev/null
test ! -e .venv.new && echo no_new_ok
test ! -e .runtime-update-pending && echo no_pending_ok
```

### 3.5 Start services on the swapped venv

```bash
# Target:
sudo systemctl start kittyhack_control.service
# Remote:
# sudo systemctl start kittyhack.service
```

**Expect:**

- UI loads
- Camera / door / basic navigation works
- Inference path smoke (open live view / trigger a detection if practical):

```bash
.venv/bin/python -c "import torch, cv2, shiny; print('ok')"
```

### 3.6 Restore device after simulation (important)

```bash
sudo systemctl stop kittyhack.service kittyhack_control.service 2>/dev/null || true
cp /tmp/REQUIRED_PYTHON.bak setup/REQUIRED_PYTHON
# Either rebuild correct venv:
sudo bash setup/ensure_venv.sh --bootstrap --root /root/kittyhack
# Or, if .venv.old is the known-good previous venv and versions match REQUIRED_PYTHON:
# sudo rm -rf .venv && sudo mv .venv.old .venv
sudo systemctl start kittyhack_control.service   # or kittyhack.service in remote mode
```

Confirm UI and `REQUIRED_PYTHON` are back to `3.11` for v2.7.0 production testing.

---

## 4. Status page edge cases

### 4.1 Language selection

1. Set `language = de` in `config.ini`, repeat a short `--apply` with pending work (or temporarily create `.runtime-update-pending` + fake need).
2. Repeat with `language = en`.

**Expect:** matching DE/EN user text.

### 4.2 Port 80 already busy

1. Start a dummy listener on 80, or leave kittyhack running on 80.
2. Run `--apply` with real work pending.

**Expect:**

- Warning in log that status page could not bind
- Apply **still proceeds** (must not abort only because the page failed)
- Prefer retesting with port 80 free for the happy-path UI check

### 4.3 Crash during apply must not leave status server forever

1. Start `--apply` with work pending (status page visible).
2. Kill the ensure script hard (`kill -9` on the bash process) while page is up.

**Expect:**

- You may need to manually `kill` the status server once (PID in `.update-status-server.pid`) — note whether cleanup worked
- Document actual behaviour; ideal is no orphan page after normal `die()` failures
- `.venv` should still be the previous working one if swap did not complete

---

## 5. Failure / no-lockout tests

### 5.1 Failed prepare leaves old venv bootable

```bash
# Simulate failure: wrong requirements path or disconnect network mid-prepare
sudo bash setup/ensure_venv.sh --prepare --root /root/kittyhack --requirements /tmp/does-not-exist.txt
```

**Expect:**

- Non-zero exit
- `.venv` still present and importable
- No successful pending swap left behind (or pending marker cleared on failure)
- Starting services still brings UI up on old venv

### 5.2 Failed apply repair leaves old venv

Force repair path (no `.venv.new`, wrong required Python), then cause pip/smoke failure (e.g. pull network mid-install).

**Expect:**

- Apply fails
- Old `.venv` still starts the app
- Status page stops
- Device not bricked

### 5.3 Update rollback removes half-prepared `.venv.new`

1. Start an in-app update that would prepare a new runtime (only if you have a test tag that changes `REQUIRED_PYTHON`).
2. Interrupt / force failure after `.venv.new` appears, or trigger the updater’s exception path.

**Expect (from `update_kittyhack` rollback logic):**

- Git returns to previous version
- `.venv.new` is removed
- Previous app version runs after restart

---

## 6. End-to-end “future release” rehearsal (recommended before any real Python bump)

Use two tags on a test repo/device:

| Tag | Meaning |
|-----|---------|
| `v2.7.0` (or current) | Mechanism present, `REQUIRED_PYTHON=3.11` |
| `vX.Y.Z-test` | Same tree + `REQUIRED_PYTHON` bumped + requirements that install on that Python |

### Steps

1. Device on `v2.7.0` with healthy UI.
2. Point update source at the test repo/tag (or custom update repo).
3. Run in-app update to `vX.Y.Z-test`.
4. **During update**, confirm `--prepare` builds `.venv.new` (progress may take long).
5. Confirm reboot prompt appears; **do not power-cycle manually**.
6. After reboot, open `http://<device-ip>/` immediately:
   - Status page while finishing/swapping
   - Then normal Kittyhack UI
7. Verify:

```bash
cat setup/REQUIRED_PYTHON
.venv/bin/python -V
systemctl is-active kittyhack.service kittyhack_control.service
```

8. Functional check: login/UI, live view, RFID/door if available, MQTT if used.

Only after this rehearsal passes on Pi **and** (if applicable) remote host should a real production Python bump be released.

---

## 7. Regression checklist (quick)

After all tests, on a production-like v2.7.0 device:

- [ ] Web UI loads after reboot
- [ ] `REQUIRED_PYTHON` is `3.11`
- [ ] `.venv` Python is 3.11
- [ ] No `.venv.new` / pending marker leftovers
- [ ] `ExecStartPre` present on active unit(s)
- [ ] Normal update with unchanged Python still updates pip deps before reboot when `requirements.txt` changes
- [ ] Door/camera/MQTT smoke OK
- [ ] Log download / Info tab OK

---

## 8. Pass / fail criteria

**Pass** if all are true:

1. Normal v2.7.0 boots/updates stay fast (ensure_venv no-op).
2. Simulated migration never deletes the working `.venv` before a smoke-tested replacement exists.
3. Status page appears for pending post-reboot work and disappears afterwards.
4. Failed prepare/apply still allows the previous UI to start.
5. Unit files with `ExecStartPre` are on disk after an in-app update, before reboot.

**Fail** if any of these happen:

- Device unreachable on port 80 with no status page and no UI for a long time during a known pending migration
- `.venv` removed/broken while `.venv.new` incomplete
- Update “succeeds” but reboot loops on `ExecStartPre`
- Pip-only updates (Python unchanged) skip installing changed requirements

---

## 9. Suggested order on one test Pi (half-day)

1. Section 1 (baseline no-op) — 15 min  
2. Section 2.1 (requirements-only update) — 30–60 min  
3. Section 3 (simulated migration + status page) — 1–2 h  
4. Section 5.1 (failed prepare) — 15 min  
5. Section 3.6 restore — 30–60 min  
6. Section 7 regression — 15 min  

Optional later: full Section 6 with a dedicated test tag before any real Python upgrade release.
