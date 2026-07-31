"""Backend entry-decision helpers exercised with policy + fake RFID identity."""

from src.baseconfig import AllowedToEnter
from src.backend.entry_policy import (
    _compute_tag_id_valid_for_entry,
    _identified_tag_for_entry,
)
from src.hardware_sim import FakeRfid


def test_entry_decision_with_injected_rfid_tag():
    """Simulate: inject RFID → identify → validate under KNOWN policy."""
    rfid = FakeRfid()
    known = ["CAT123"]
    rfid.inject_tag("CAT123", timestamp=1.0)
    tag_id, _ = rfid.get_tag()

    identified, source = _identified_tag_for_entry(
        tag_id, None, known, allowed_to_enter=AllowedToEnter.KNOWN
    )
    assert identified == "CAT123"
    assert source == "RFID"
    assert (
        _compute_tag_id_valid_for_entry(AllowedToEnter.KNOWN, tag_id, identified, {})
        is True
    )


def test_entry_denied_for_unknown_chip_under_known_policy():
    rfid = FakeRfid()
    rfid.inject_tag("STRANGER", timestamp=1.0)
    tag_id, _ = rfid.get_tag()
    identified, _ = _identified_tag_for_entry(
        tag_id, None, ["CAT123"], allowed_to_enter=AllowedToEnter.KNOWN
    )
    assert identified is None
    assert (
        _compute_tag_id_valid_for_entry(AllowedToEnter.KNOWN, tag_id, identified, {})
        is False
    )
