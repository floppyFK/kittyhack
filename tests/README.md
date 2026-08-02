# Testing & simulation (developers)

Local unit tests and hardware simulation for Kittyhack. End-user install docs stay in the root [README.md](../README.md).

## Local unit tests

No Kittyflap hardware required. The default pytest run excludes on-device tests.

```bash
uv pip install -r requirements-dev.txt
# also need project deps used by the modules under test (see requirements.txt)
pytest
# or: uv run pytest
```

### Coverage (`pytest-cov`)

Config lives in `pytest.ini` (`[coverage:run]` / `[coverage:report]`). Measure unit-test coverage of `src/` (hardware excluded by default):

```bash
pytest --cov=src --cov-report=term-missing
# HTML report:
pytest --cov=src --cov-report=html
# open htmlcov/index.html
```

On-target hardware coverage (Kittyflap only):

```bash
pytest -m hardware --cov=src --cov-report=term-missing
```

Layout:

- `unit/` — pure logic, config roundtrip, temp SQLite, fake hardware, backend decisions / MQTT / **loop scenarios**
- `helpers/backend_loop_harness.py` — `FakeClock` + `build_loop_context()` for unit tests; `build_hardware_loop_context()` for on-target Magnets
- `hardware/` — on-target unlock-logic / Door Control matrix (`@pytest.mark.hardware`)
- `conftest.py` — temp config, simulate env (skipped for `hardware` marker), fake HW fixtures

Backend loop scenarios (`tests/unit/test_backend_loop_scenarios.py`) drive a real `process_tick` with injectable fakes (no GPIO/camera/MQTT): entry allow/deny, exit, prey block, manual override, fast crossing.

```bash
pytest tests/unit/test_backend_loop_scenarios.py -v
```

`pytest.ini` sets `addopts = -m "not hardware"` so local/CI never touches real GPIO by default.

## Simulate mode (process flag)

Simulation is **not** controlled via `config.ini` (the old `simulate_kittyflap` key is ignored and removed on load). Use a process-level flag:

```bash
# Web UI / backend (e.g. under systemd or uvicorn)
Environment=KITTYHACK_SIMULATE=1

# Or control service CLI
python -m src.kittyhack_control --simulate
```

Production systemd units must **not** set `KITTYHACK_SIMULATE`. See `src/runtime_flags.py` and `src/hardware_sim.py` (`FakePir` / `FakeMagnets` / `FakeRfid`).

## On-target hardware matrix

Run only on a Kittyflap (board with real GPIO). These tests drive the **real Magnets** path while injecting PIR / RFID / camera detections so the full unlock-logic and Door Control Settings permutations stay automatic.

**Before running:** stop services that own the GPIO / magnet queue (otherwise singleton / pin conflicts):

```bash
sudo systemctl stop kittyhack kittyhack_control
```

Then:

```bash
# KITTYHACK_SIMULATE must be unset
cd /path/to/kittyhack
pytest -m hardware -vv
```

Notes:

- The door **will physically unlock/lock**. Keep clear of moving parts.
- The suite can take many minutes (real `MAG_RFID_CMD_DELAY` between magnet ops).
- Pytest timeout is disabled for `tests/hardware/` (`timeout(0)`).
- After tests: `sudo systemctl start kittyhack kittyhack_control`

Modules:

- `test_smoke.py` — import / GPIO presence / optional real Pir+Magnets init
- `test_entry_unlock_modes.py` — all five `ALLOWED_TO_ENTER` modes
- `test_entry_prey.py` — global / per-cat prey, thresholds, analyze window, lockout
- `test_exit_unlock_modes.py` — `ALLOWED_TO_EXIT` + time ranges
- `test_door_control_params.py` — camera motion/ID, immediate lock, remaining knobs
