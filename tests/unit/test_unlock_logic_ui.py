"""Decision-step model and HTML for the flap configuration tab."""

from __future__ import annotations

from pathlib import Path

from src.baseconfig import AllowedToEnter, AllowedToExit
from src.server_ui.unlock_logic import (
    build_entry_logic,
    build_exit_logic,
    entry_logic_ui,
    exit_logic_ui,
)

ROOT = Path(__file__).resolve().parents[2]
PO_PATH = ROOT / "locales" / "de" / "LC_MESSAGES" / "messages.po"


def _identity(text: str) -> str:
    return text


def _walk(flow: dict):
    steps = flow["steps"]
    assert steps, flow["mode"]
    for index, step in enumerate(steps):
        assert step["question"].strip()
        for key in ("no", "yes"):
            branch = step[key]
            assert branch["kind"] in {"continue", "end"}
            if branch["kind"] == "end":
                assert branch["label"].strip()
                assert branch["tone"] in {"ok", "block", "muted"}
            else:
                assert index < len(steps) - 1


def _ends(flow: dict) -> list[str]:
    labels = []
    for step in flow["steps"]:
        for key in ("no", "yes"):
            branch = step[key]
            if branch["kind"] == "end":
                labels.append(branch["label"])
    return labels


def test_entry_and_exit_cover_every_mode():
    entry = {flow["mode"]: flow for flow in build_entry_logic(_identity)}
    exit_ = {flow["mode"]: flow for flow in build_exit_logic(_identity)}
    assert set(entry) == {mode.value for mode in AllowedToEnter}
    assert set(exit_) == {mode.value for mode in AllowedToExit}
    for flow in (*entry.values(), *exit_.values()):
        assert flow["label"].strip()
        assert flow["summary"].strip()
        _walk(flow)

    assert "Unlock inside" in _ends(entry["all"])
    assert "Unlock inside" not in _ends(entry["none"])
    assert "Unlock outside" in _ends(exit_["allow"])
    assert "Unlock outside" in _ends(exit_["configure_per_cat"])
    assert "Unlock outside" not in _ends(exit_["deny"])


def test_stopping_answer_is_rendered_before_continue():
    html = str(entry_logic_ui(_identity)[0])
    assert html.find("No action") < html.find("Continue")
    assert 'data-mode="all"' in html
    assert 'class="kh-flow"' in html


def test_active_mode_is_visible_and_text_is_escaped():
    panels = entry_logic_ui(lambda text: text + " <cat>", active="known")
    html = "".join(str(panel) for panel in panels)
    assert 'data-mode="known"' in html
    known = str(panels[2])
    assert "display:block;" in known
    assert "<cat>" not in html
    assert "&lt;cat&gt;" in html
    assert "display:block;" not in str(panels[0])


def test_german_catalog_contains_decision_strings():
    seen: list[str] = []

    def record(text: str) -> str:
        seen.append(text)
        return text

    entry_logic_ui(record)
    exit_logic_ui(record)
    seen.extend(
        [
            "The steps follow the option selected above.",
            "See the decision steps above for details.",
        ]
    )
    msgids = set(_po_msgids(PO_PATH.read_text(encoding="utf-8")))
    missing = sorted({text for text in seen if text not in msgids})
    assert not missing, "Missing German msgid(s):\n" + "\n".join(missing)


def _po_msgids(text: str) -> list[str]:
    msgids = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("msgid "):
            value, index = _read_po_string(lines, index)
            msgids.append(value)
        else:
            index += 1
    return msgids


def _read_po_string(lines: list[str], index: int) -> tuple[str, int]:
    first = lines[index]
    prefix = "msgid " if first.startswith("msgid ") else "msgstr "
    body = first[len(prefix):]
    parts = []
    if body != '""':
        parts.append(_unquote(body))
        index += 1
    else:
        index += 1
        while index < len(lines) and lines[index].startswith('"'):
            parts.append(_unquote(lines[index]))
            index += 1
    return "".join(parts), index


def _unquote(token: str) -> str:
    assert token.startswith('"') and token.endswith('"'), token
    return bytes(token[1:-1], "utf-8").decode("unicode_escape")
