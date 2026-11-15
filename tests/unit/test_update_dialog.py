"""The update dialog must stay in front of the remote-disconnect notice."""

from src.server_ui.state import set_update_progress, update_dialog_is_open


def test_update_dialog_blocks_disconnect_notice():
    set_update_progress(in_progress=False, result=None)
    assert update_dialog_is_open() is False

    set_update_progress(in_progress=True, result=None)
    assert update_dialog_is_open() is True

    for result in ("ok", "error", "reboot_dialog"):
        set_update_progress(in_progress=False, result=result)
        assert update_dialog_is_open() is True

    set_update_progress(in_progress=False, result=None)
