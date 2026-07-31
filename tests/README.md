# Testing & simulation (developers)

Local unit tests and hardware simulation for Kittyhack. End-user install docs stay in the root [README.md](../README.md).

## Local unit tests

No Kittyflap hardware required. The default pytest run excludes on-device smoke tests.

```bash
uv pip install -r requirements-dev.txt
# also need project deps used by the modules under test (see requirements.txt)
pytest
# or: uv run pytest
```

Layout:

- `unit/` — pure logic, config roundtrip, temp SQLite, fake hardware, backend decisions / MQTT / **loop scenarios**
- `helpers/backend_loop_harness.py` — `FakeClock` + `build_loop_context()` for pumping `backend_main(..., run_forever=False)`
- `hardware/` — on-target smoke (`@pytest.mark.hardware`)
- `conftest.py` — temp config, simulate env, fake HW fixtures

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

## On-target smoke

Run only on a Kittyflap (or a board with real GPIO):

```bash
pytest -m hardware -q
```
