"""Small UI tag helpers shared across configuration and other tabs."""

from shiny import ui

from src.baseconfig import CONFIG, set_language

_ = set_language(CONFIG["LANGUAGE"])


def _disable_numeric_input(tag):
    """Disable a ui.input_numeric Tag (shiny currently has no disabled= for input_numeric)."""
    try:
        if getattr(tag, "children", None) and len(tag.children) >= 2:
            tag.children[1].attrs["disabled"] = "disabled"
    except Exception:
        pass
    return tag


def _disable_input(tag):
    """Disable a Shiny input Tag by setting disabled on its inner control."""
    try:
        if getattr(tag, "children", None) and len(tag.children) >= 2:
            tag.children[1].attrs["disabled"] = "disabled"
    except Exception:
        pass
    return tag


def collapsible_section(section_id, title, intro, content):
    """Bootstrap-collapse section with chevron header, intro text, and body content."""
    return ui.div(
        ui.div(
            ui.HTML(f"""
                <button class="collapsible-header-btn btn" type="button" data-bs-toggle="collapse" data-bs-target="#{section_id}_body" aria-expanded="false" aria-controls="{section_id}_body">
                    <span class="collapsible-chevron">&#9654;</span>
                    <span class="collapsible-title">{title}</span>
                </button>
                <div class="collapsible-section-intro">{intro}</div>
            """),
        ),
        ui.div(
            ui.div(content),
            id=f"{section_id}_body",
            class_="collapse",  # collapsed by default
        ),
        class_="collapsible-section",
        style_="margin-bottom:1.5em;",
    )


def wlan_add_dialog():
    """Show the modal dialog for adding a new WLAN connection."""
    m = ui.modal(
        ui.div(ui.input_text("txtWlanSSID", _("SSID"), "")),
        ui.div(ui.input_password("txtWlanPassword", _("Password"), "")),
        ui.input_numeric("numWlanPriority", _("Priority"), 0, min=0, max=100, step=1),
        ui.help_text(
            _(
                "The priority determines the order in which the WLANs are tried to connect. Higher numbers are tried first."
            )
        ),
        title=_("Add new WLAN configuration"),
        easy_close=False,
        footer=ui.div(
            ui.input_action_button(
                id="btn_wlan_save",
                label=_("Save"),
                class_="btn-vertical-margin btn-narrow",
            ),
            ui.input_action_button(
                id="btn_modal_cancel",
                label=_("Cancel"),
                class_="btn-vertical-margin btn-narrow",
            ),
        ),
    )
    ui.modal_show(m)
