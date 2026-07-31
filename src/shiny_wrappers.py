"""Shiny UI helpers with small fixes (e.g. file input scroll behavior)."""

from __future__ import annotations

__all__ = ("input_file",)

from typing import Literal, Optional

from htmltools import Tag, TagChild, css, div, span, tags

from shiny._docstring import add_example
from shiny._namespaces import resolve_id
from shiny.ui._utils import shiny_input_label

class uix:
    """Namespace for patched Shiny UI input helpers."""

    #@add_example()
    @staticmethod
    def input_file(
        id: str,
        label: TagChild,
        *,
        multiple: bool = False,
        accept: Optional[str | list[str]] = None,
        width: Optional[str] = None,
        button_label: str = "Browse...",
        placeholder: str = "No file selected",
        capture: Optional[Literal["environment", "user"]] = None,
    ) -> Tag:
        """File upload control that does not scroll the page to the top on click."""

        if isinstance(accept, str):
            accept = [accept]

        resolved_id = resolve_id(id)
        btn_file = span(
            button_label,
            tags.input(
                id=resolved_id,
                name=resolved_id,
                type="file",
                multiple="multiple" if multiple else None,
                accept=",".join(accept) if accept else None,
                capture=capture,
                # The original input_file function has a bad implementation, where the page is scrolled to the top when the file input is clicked:
                # Don't use "display: none;" style, which causes keyboard accessibility issue; instead use the following workaround: https://css-tricks.com/places-its-tempting-to-use-display-none-but-dont/
                #style="position: absolute !important; top: -99999px !important; left: -99999px !important;",

                # Fixed version: This function prevents the scrolling to the top of the page when the file input is clicked.
                # The input is hidden, but the button is still clickable.
                style="display: none !important;",
                class_="shiny-input-file-fixed",
            ),
            class_="btn btn-default btn-file",
        )
        return div(
            shiny_input_label(resolved_id, label),
            div(
                tags.label(btn_file, class_="input-group-btn input-group-prepend"),
                tags.input(
                    type="text",
                    class_="form-control",
                    placeholder=placeholder,
                    readonly="readonly",
                ),
                class_="input-group",
            ),
            div(
                div(class_="progress-bar"),
                id=resolved_id + "_progress",
                class_="progress active shiny-file-input-progress",
            ),
            class_="form-group shiny-input-container",
            style=css(width=width),
        )
