"""In-app decision steps for the flap entry and exit modes.

The configuration tab renders these as HTML. The Mermaid files under ``doc/``
are only an offline reference and are not shown in the UI.
"""

from __future__ import annotations

from collections.abc import Callable

from faicons import icon_svg
from shiny import ui

Translate = Callable[[str], str]

_ICONS = {
    "ok": "lock-open",
    "block": "lock",
    "muted": "minus",
}


def _cont() -> dict:
    return {"kind": "continue"}


def _end(label: str, tone: str) -> dict:
    return {"kind": "end", "label": label, "tone": tone}


def _step(question: str, no, yes, detail: str = "") -> dict:
    return {"question": question, "detail": detail, "no": no, "yes": yes}


def _flow(mode: str, label: str, summary: str, steps: list[dict]) -> dict:
    return {"mode": mode, "label": label, "summary": summary, "steps": steps}


def _icon(name: str):
    # Leave the icon's own aspect ratio alone. Forcing width and height
    # squashes wide glyphs because faicons sets preserveAspectRatio="none".
    return icon_svg(
        name,
        a11y="deco",
        margin_left="0",
        margin_right="0",
    )


def _motion_outside(_: Translate, no_action: dict) -> dict:
    return _step(
        _("Motion outside?"),
        no_action,
        _cont(),
        _(
            "From the motion sensor or the camera, depending on your settings."
        ),
    )


def _recognize(_: Translate, no_action: dict) -> list[dict]:
    return [
        _step(
            _("Cat recognized?"),
            no_action,
            _cont(),
            _("By RFID, or by the camera when camera identification is turned on."),
        ),
        _step(_("Registered in Kittyhack?"), no_action, _cont()),
    ]


def _prey_steps(_: Translate, unlock: dict, stay: dict, *, per_cat: bool) -> list[dict]:
    steps = []
    if per_cat:
        steps.append(
            _step(_("Prey detection enabled for every cat?"), unlock, _cont())
        )
        steps.append(
            _step(
                _("Prey detection enabled for this cat?"),
                unlock,
                _cont(),
                _("You can turn this off for a single cat under Cats."),
            )
        )
    else:
        steps.append(_step(_("Prey detection enabled?"), unlock, _cont()))
    steps.append(
        _step(
            _("Prey detected?"),
            unlock,
            stay,
            _("The flap stays locked while a picture shows prey."),
        )
    )
    return steps


def build_entry_logic(_: Translate) -> list[dict]:
    """Entry decision for each ``AllowedToEnter`` value."""
    no_action = _end(_("No action"), "muted")
    unlock = _end(_("Unlock inside"), "ok")
    stay = _end(_("Stay locked"), "block")
    per_cat_choice = _("You set this separately for each cat under Cats.")

    return [
        _flow(
            "all",
            _("Entry logic for all cats"),
            _(
                "Motion outside unlocks the inside, unless prey detection keeps the flap locked."
            ),
            [
                _motion_outside(_, no_action),
                *_prey_steps(_, unlock, stay, per_cat=False),
            ],
        ),
        _flow(
            "all_rfids",
            _("Entry logic for cats with an RFID chip"),
            _(
                "An RFID chip has to be detected. The cat does not need to be registered."
            ),
            [
                _motion_outside(_, no_action),
                _step(
                    _("RFID chip detected?"),
                    no_action,
                    _cont(),
                    _("Any chip is enough. The cat does not have to be registered."),
                ),
                *_prey_steps(_, unlock, stay, per_cat=True),
            ],
        ),
        _flow(
            "known",
            _("Entry logic for registered cats"),
            _(
                "Only a registered cat can unlock the inside, by RFID or by the camera."
            ),
            [
                _motion_outside(_, no_action),
                *_recognize(_, no_action),
                *_prey_steps(_, unlock, stay, per_cat=True),
            ],
        ),
        _flow(
            "none",
            _("Entry logic when entry is closed"),
            _("The inside stays locked."),
            [
                _step(
                    _("Motion outside?"),
                    no_action,
                    no_action,
                    _(
                        "From the motion sensor or the camera, depending on your settings."
                    ),
                )
            ],
        ),
        _flow(
            "configure_per_cat",
            _("Entry logic for individual cat settings"),
            _(
                "Only cats you allow under Cats can enter, after Kittyhack recognizes them."
            ),
            [
                _motion_outside(_, no_action),
                *_recognize(_, no_action),
                _step(_("May this cat enter?"), no_action, _cont(), per_cat_choice),
                *_prey_steps(_, unlock, stay, per_cat=True),
            ],
        ),
    ]


def build_exit_logic(_: Translate) -> list[dict]:
    """Exit decision for each ``AllowedToExit`` value."""
    no_action = _end(_("No action"), "muted")
    unlock = _end(_("Unlock outside"), "ok")
    denied = _end(_("Exit not allowed"), "block")
    per_cat_choice = _("You set this separately for each cat under Cats.")
    motion_inside = _step(
        _("Motion inside?"),
        no_action,
        _cont(),
        _("The sensor on the house side of the flap."),
    )
    time_window = _step(
        _("Inside an allowed exit time window?"),
        denied,
        _cont(),
        _("If every time range below is off, cats may leave at any time."),
    )

    return [
        _flow(
            "allow",
            _("Exit logic when exit is allowed"),
            _(
                "Motion inside unlocks the outside, within the time windows you set below."
            ),
            [
                motion_inside,
                _step(
                    _("Inside an allowed exit time window?"),
                    denied,
                    unlock,
                    _(
                        "If every time range below is off, cats may leave at any time."
                    ),
                ),
            ],
        ),
        _flow(
            "deny",
            _("Exit logic when exit is closed"),
            _("The outside stays locked."),
            [
                _step(
                    _("Motion inside?"),
                    no_action,
                    no_action,
                    _("The sensor on the house side of the flap."),
                )
            ],
        ),
        _flow(
            "configure_per_cat",
            _("Exit logic for individual cat settings"),
            _(
                "The cat has to be identified by RFID and allowed to leave. "
                "The time windows below apply to every cat."
            ),
            [
                motion_inside,
                time_window,
                _step(
                    _("Cat identified by RFID?"),
                    denied,
                    _cont(),
                    _("Camera identification does not count here."),
                ),
                _step(_("May this cat leave?"), denied, unlock, per_cat_choice),
            ],
        ),
    ]


def _ordered_branches(step: dict) -> list[tuple[str, dict]]:
    """Show a stopping answer before the branch that continues downward."""
    branches = [("no", step["no"]), ("yes", step["yes"])]
    branches.sort(key=lambda item: item[1].get("kind") == "continue")
    return branches


def _render_branch(_: Translate, which: str, branch: dict):
    answer = _("No") if which == "no" else _("Yes")
    if branch.get("kind") == "continue":
        return ui.tags.li(
            ui.span(answer, class_="kh-flow-answer"),
            ui.span(_("Continue"), class_="kh-flow-next"),
            class_="kh-flow-branch kh-flow-branch-next",
        )

    tone = branch.get("tone") or "muted"
    icon_name = _ICONS.get(tone)
    result_children = []
    if icon_name:
        result_children.append(
            ui.span(_icon(icon_name), class_="kh-flow-result-icon", aria_hidden="true")
        )
    result_children.append(ui.span(branch.get("label") or "", class_="kh-flow-result-label"))
    return ui.tags.li(
        ui.span(answer, class_="kh-flow-answer"),
        ui.span(*result_children, class_=f"kh-flow-result kh-flow-result-{tone}"),
        class_=f"kh-flow-branch kh-flow-branch-end kh-flow-branch-{tone}",
    )


def _render_step(_: Translate, step: dict, index: int, show_index: bool):
    question = [
        ui.tags.p(step.get("question") or "", class_="kh-flow-question"),
    ]
    detail = (step.get("detail") or "").strip()
    if detail:
        question.append(ui.tags.p(detail, class_="kh-flow-detail"))
    head = []
    if show_index:
        head.append(
            ui.span(str(index), class_="kh-flow-index", aria_hidden="true")
        )
    head.append(ui.div(*question, class_="kh-flow-q-text"))
    return ui.tags.li(
        ui.div(*head, class_="kh-flow-q"),
        ui.tags.ul(
            *[_render_branch(_, which, branch) for which, branch in _ordered_branches(step)],
            class_="kh-flow-branches",
        ),
        class_="kh-flow-step",
    )


def render_logic_panels(flows: list[dict], _: Translate, active: str | None = None) -> list:
    """Return one panel per mode. Only the active mode is visible until JS switches it."""
    panels = []
    for flow in flows:
        steps = flow.get("steps") or []
        show_index = len(steps) > 1
        kwargs = {}
        if active and flow.get("mode") == active:
            kwargs["style_"] = "display:block;"
        panels.append(
            ui.tags.section(
                ui.tags.p(flow.get("summary") or "", class_="kh-flow-summary"),
                ui.tags.ol(
                    *[
                        _render_step(_, step, index, show_index)
                        for index, step in enumerate(steps, start=1)
                    ],
                    class_="kh-flow-steps",
                ),
                class_="kh-flow",
                data_mode=flow.get("mode") or "",
                aria_label=flow.get("label") or "",
                **kwargs,
            )
        )
    return panels


def entry_logic_ui(_: Translate, active: str | None = None) -> list:
    return render_logic_panels(build_entry_logic(_), _, active=active)


def exit_logic_ui(_: Translate, active: str | None = None) -> list:
    return render_logic_panels(build_exit_logic(_), _, active=active)
