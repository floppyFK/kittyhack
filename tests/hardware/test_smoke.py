"""On-target smoke tests (skipped by default via pytest.ini addopts).

Run on a Kittyflap only:

    pytest -m hardware -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]


def test_core_modules_import():
    """Sanity: packages used on-device import under the Pi venv."""
    import src.baseconfig  # noqa: F401
    import src.backend  # noqa: F401
    import src.hardware_sim  # noqa: F401
    import src.runtime_flags  # noqa: F401


def test_gpio_sysfs_present_when_not_simulating():
    """When not simulating, expect the Pi GPIO chip sysfs path."""
    from src.runtime_flags import is_simulate_mode

    if is_simulate_mode():
        pytest.skip("KITTYHACK_SIMULATE is set; skipping real GPIO path check")
    path = Path("/sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio")
    assert path.parent.exists() or Path("/sys/class/gpio").exists()


def test_real_hardware_init_optional():
    """Optional: Magnets/Pir init on board (skipped if simulating)."""
    from src.runtime_flags import is_simulate_mode

    if is_simulate_mode():
        pytest.skip("simulating; not initializing real GPIO")
    if not Path("/sys/class/gpio").exists():
        pytest.skip("no GPIO sysfs on this host")

    from src.magnets_rfid import Magnets
    from src.pir import Pir

    pir = Pir(simulate_kittyflap=False)
    pir.init()
    magnets = Magnets(simulate_kittyflap=False)
    magnets.init()
    assert Pir.instance is pir
    assert Magnets.instance is magnets
