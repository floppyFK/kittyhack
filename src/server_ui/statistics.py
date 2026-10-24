"""Statistics tab: filters, KPIs, charts, per-cat cards."""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from shiny import reactive, render, ui
from zoneinfo import ZoneInfo
from faicons import icon_svg

from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.database import CatsRepo, ReturnDataCatDB, VisitStatsRepo
from src.helper import DateTimeUtil
from src.server_ui.context import SessionContext
from src.server_ui.state import reload_trigger_photos, stats_hidden_cats_revision
from src.statistics import (
    CAT_FILTER_ALL,
    CAT_FILTER_UNKNOWN,
    DashboardLabels,
    build_dashboard,
    dump_hidden_cat_keys,
    format_duration,
    parse_hidden_cat_keys,
    valid_hidden_cat_key,
)

_ = set_language(CONFIG["LANGUAGE"])


def _dashboard_labels() -> DashboardLabels:
    return DashboardLabels(
        unknown=_("Unknown"),
        entries=_("Entries"),
        exits=_("Exits"),
        in_label=_("Inside"),
        out_label=_("Outside"),
        prey=_("Prey"),
        prey_entered=_("Entered with prey"),
        prey_blocked=_("Prey blocked"),
        attempts_entry=_("Entry attempts"),
        attempts_exit=_("Denied exits"),
        median_outside=_("Median time outside"),
        median_inside=_("Median time inside"),
        passages=_("Passages"),
        duration_d=_("d"),
        duration_h=_("h"),
        duration_m=_("m"),
        inside=_("Inside"),
        outside=_("Outside"),
        location_unknown=_("Unknown"),
        last_seen_inside=_("Last seen inside"),
        last_seen_outside=_("Last seen outside"),
    )


def _cat_filter_choices() -> dict:
    choices = {CAT_FILTER_ALL: _("All cats"), CAT_FILTER_UNKNOWN: _("Unknown")}
    names = {}
    try:
        names = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
    except Exception:
        names = {}
    for rfid, name in sorted(names.items(), key=lambda item: str(item[1]).lower()):
        if rfid:
            choices[str(rfid)] = str(name)
    try:
        for extra in VisitStatsRepo.list_cats(CONFIG["KITTYHACK_DATABASE_PATH"]):
            rfid = extra.get("cat_rfid")
            if not rfid or str(rfid) in choices:
                continue
            choices[str(rfid)] = str(extra.get("cat_name") or rfid)
    except Exception:
        pass
    return choices


def _cat_thumbnails() -> dict[str, str]:
    thumbs: dict[str, str] = {}
    try:
        df = CatsRepo.db_get_cats(
            CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all
        )
    except Exception:
        return thumbs
    if df.empty:
        return thumbs
    for __, row in df.iterrows():
        rfid = row.get("rfid")
        cat_id = row.get("id")
        if not rfid or cat_id is None:
            continue
        thumb = CatsRepo.get_cat_thumbnail(
            CONFIG["KITTYHACK_DATABASE_PATH"], int(cat_id), size=(64, 64)
        )
        if thumb:
            thumbs[str(rfid)] = thumb
    return thumbs


def _kpi_card(title: str, value: str, hint: str | None = None):
    children = [
        ui.span(title, class_="stats-kpi-label"),
        ui.span(value, class_="stats-kpi-value"),
    ]
    if hint:
        children.append(ui.span(hint, class_="stats-kpi-hint"))
    return ui.div(*children, class_="stats-kpi-card")


def _location_label(location: str, labels: DashboardLabels) -> str:
    if location == "inside":
        return labels.last_seen_inside
    if location == "outside":
        return labels.last_seen_outside
    return labels.location_unknown


def _cat_key_of(cat: dict) -> str:
    key = cat.get("key")
    if key:
        return str(key)
    rfid = cat.get("rfid")
    if rfid:
        return str(rfid)
    return CAT_FILTER_UNKNOWN


def _visibility_button(label: str, key: str, *, hide: bool):
    return ui.tags.button(
        label,
        type="button",
        class_="stats-cat-hide-btn" if hide else "stats-cat-show-btn",
        data_stats_cat_key=key,
        data_stats_cat_hidden="1" if hide else "0",
    )


def _parse_visibility_input(payload) -> tuple[str, bool] | None:
    if not isinstance(payload, dict):
        return None
    raw_key = payload.get("key")
    raw_hidden = payload.get("hidden")
    if isinstance(raw_key, list):
        raw_key = raw_key[0] if raw_key else ""
    if isinstance(raw_hidden, list):
        raw_hidden = raw_hidden[0] if raw_hidden else False
    key = str(raw_key or "").strip()
    if not valid_hidden_cat_key(key):
        return None
    if isinstance(raw_hidden, bool):
        hide = raw_hidden
    elif isinstance(raw_hidden, (int, float)):
        hide = raw_hidden != 0
    else:
        hide = str(raw_hidden).strip().lower() in {"1", "true", "yes"}
    return key, hide


def _cat_cards_ui(cats: list, labels: DashboardLabels):
    cards = []
    for cat in cats:
        loc = cat.get("location") or "unknown"
        loc_safe = loc if loc in ("inside", "outside") else "unknown"
        loc_class = f"stats-loc stats-loc--{loc_safe}"
        thumb = cat.get("thumbnail") or ""
        if thumb:
            img = ui.HTML(
                f'<img class="stats-cat-thumb" alt="" '
                f'src="data:image/jpeg;base64,{thumb}" />'
            )
        else:
            img = ui.span(
                ui.HTML(str(icon_svg("cat"))),
                class_="stats-cat-thumb stats-cat-thumb--empty",
            )
        name = cat.get("name") or labels.unknown
        extra_items = [
            ui.div(
                ui.span(_("Typical first exit"), class_="stats-cat-metric-label"),
                ui.span(cat.get("typical_first_exit") or "—"),
            ),
            ui.div(
                ui.span(_("Typical last return"), class_="stats-cat-metric-label"),
                ui.span(cat.get("typical_last_return") or "—"),
            ),
            ui.div(
                ui.span(_("Longest time outside"), class_="stats-cat-metric-label"),
                ui.span(
                    format_duration(
                        cat.get("longest_outside_s"),
                        d=labels.duration_d,
                        h=labels.duration_h,
                        m=labels.duration_m,
                    )
                ),
            ),
            ui.div(
                ui.span(_("Median time outside"), class_="stats-cat-metric-label"),
                ui.span(
                    format_duration(
                        cat.get("median_outside_s"),
                        d=labels.duration_d,
                        h=labels.duration_h,
                        m=labels.duration_m,
                    )
                ),
            ),
        ]
        other_n = int(cat.get("other_access") or 0)
        if other_n:
            extra_items.append(
                ui.div(
                    ui.span(_("Another way"), class_="stats-cat-metric-label"),
                    ui.span(str(other_n)),
                )
            )
        cards.append(
            ui.div(
                ui.div(
                    img,
                    ui.div(
                        ui.span(name, class_="stats-cat-name"),
                        ui.span(
                            _location_label(loc, labels),
                            class_=loc_class,
                            title=_("Based on the last flap passage"),
                        ),
                        class_="stats-cat-identity",
                    ),
                    _visibility_button(_("Hide"), _cat_key_of(cat), hide=True),
                    class_="stats-cat-head",
                ),
                ui.div(
                    ui.div(
                        ui.span(_("Entries"), class_="stats-cat-metric-label"),
                        ui.span(str(cat.get("entries") or 0), class_="stats-cat-metric-value"),
                    ),
                    ui.div(
                        ui.span(_("Exits"), class_="stats-cat-metric-label"),
                        ui.span(str(cat.get("exits") or 0), class_="stats-cat-metric-value"),
                    ),
                    ui.div(
                        ui.span(_("Prey"), class_="stats-cat-metric-label"),
                        ui.span(str(cat.get("prey") or 0), class_="stats-cat-metric-value"),
                    ),
                    ui.div(
                        ui.span(_("Attempts"), class_="stats-cat-metric-label"),
                        ui.span(str(cat.get("attempts") or 0), class_="stats-cat-metric-value"),
                    ),
                    class_="stats-cat-metrics",
                ),
                ui.div(*extra_items, class_="stats-cat-extra"),
                class_="stats-cat-card",
            )
        )
    if not cards:
        return ui.div()
    return ui.div(*cards, class_="stats-cat-grid")


def _hidden_cats_ui(cats: list):
    chips = []
    for cat in cats:
        name = cat.get("name") or _("Unknown")
        chips.append(
            ui.div(
                ui.span(name, class_="stats-cats-hidden-name"),
                _visibility_button(_("Show"), _cat_key_of(cat), hide=False),
                class_="stats-cats-hidden-chip",
            )
        )
    return ui.div(
        ui.span(_("Hidden"), class_="stats-cats-hidden-label"),
        *chips,
        class_="stats-cats-hidden",
    )


def _cat_section_ui(payload: dict, labels: DashboardLabels, hidden: set[str]):
    cats = payload.get("cats") or []
    visible = [cat for cat in cats if _cat_key_of(cat) not in hidden]
    concealed = [cat for cat in cats if _cat_key_of(cat) in hidden]
    if not visible and not concealed:
        return ui.div()
    parts = [ui.h3(_("Per cat"), class_="stats-section-title")]
    if visible:
        parts.append(_cat_cards_ui(visible, labels))
    if concealed:
        parts.append(_hidden_cats_ui(concealed))
    return ui.div(*parts, class_="stats-cats")


def _empty_state(has_history: bool):
    if has_history:
        msg = _("No events in this time range.")
        detail = _("Try a longer range or another cat.")
    else:
        msg = _("No statistics yet.")
        detail = _(
            "New flap activity will appear here. Activity deleted before statistics "
            "were recorded cannot be restored."
        )
    return ui.div(
        ui.h3(msg, class_="stats-empty-title"),
        ui.p(detail, class_="stats-empty-text"),
        class_="stats-empty",
    )


def register_statistics(input, output, session, ctx: SessionContext):
    """Register Statistics tab handlers."""

    @output
    @render.ui
    def ui_statistics():
        today = datetime.now(DateTimeUtil.get_timezone()).date()
        return ui.div(
            ui.div(
                ui.input_select(
                    "stats_range",
                    _("Time range"),
                    choices={
                        "24h": _("Last 24 hours"),
                        "7d": _("Last 7 days"),
                        "30d": _("Last 30 days"),
                        "12m": _("Last 12 months"),
                        "all": _("All time"),
                        "custom": _("Custom range"),
                    },
                    selected="30d",
                    width="100%",
                ),
                ui.input_select(
                    "stats_bucket",
                    _("Grouping"),
                    choices={
                        "auto": _("Automatic"),
                        "hour": _("Hour"),
                        "day": _("Day"),
                        "week": _("Week"),
                        "month": _("Month"),
                    },
                    selected="auto",
                    width="100%",
                ),
                ui.input_select(
                    "stats_cat",
                    _("Cat"),
                    choices=_cat_filter_choices(),
                    selected=CAT_FILTER_ALL,
                    width="100%",
                ),
                ui.panel_conditional(
                    "input.stats_range === 'custom'",
                    ui.input_date_range(
                        "stats_custom_dates",
                        _("Date range"),
                        start=today - timedelta(days=30),
                        end=today,
                        format=CONFIG.get("DATE_FORMAT", "yyyy-mm-dd"),
                        width="100%",
                    ),
                ),
                class_="stats-filters",
            ),
            ui.output_ui("ui_statistics_dashboard"),
            ui.output_ui("ui_statistics_cats"),
            class_="stats-page",
        )

    @reactive.calc
    def _stats_dashboard_payload():
        # Read reload_trigger_photos so this output re-executes when events change.
        reload_trigger_photos.get()
        try:
            range_key = str(input.stats_range() or "30d")
        except Exception:
            range_key = "30d"
        try:
            bucket_key = str(input.stats_bucket() or "auto")
        except Exception:
            bucket_key = "auto"
        try:
            cat_filter = str(input.stats_cat() or CAT_FILTER_ALL)
        except Exception:
            cat_filter = CAT_FILTER_ALL

        custom_start = custom_end = None
        if range_key == "custom":
            try:
                dates = input.stats_custom_dates()
                if dates and len(dates) >= 2:
                    custom_start, custom_end = dates[0], dates[1]
            except Exception:
                custom_start = custom_end = None
            if custom_start is None or custom_end is None:
                range_key = "30d"

        labels = _dashboard_labels()
        try:
            tz = ZoneInfo(CONFIG.get("TIMEZONE") or "UTC")
        except Exception:
            tz = DateTimeUtil.get_timezone()

        payload = build_dashboard(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            range_key=range_key,
            bucket_key=bucket_key,
            cat_filter=cat_filter,
            custom_start=custom_start,
            custom_end=custom_end,
            tz=tz,
            labels=labels,
            cat_thumbnails=_cat_thumbnails(),
        )
        return payload

    def _known_cat_keys() -> set[str]:
        payload = _stats_dashboard_payload()
        return {_cat_key_of(cat) for cat in (payload.get("cats") or [])}

    def _update_hidden_cat(key: str, *, hide: bool) -> None:
        if key not in _known_cat_keys():
            return
        keys = parse_hidden_cat_keys(CONFIG.get("STATS_HIDDEN_CATS"))
        if hide:
            if key in keys:
                return
            keys.add(key)
        else:
            if key not in keys:
                return
            keys.discard(key)
        previous = CONFIG.get("STATS_HIDDEN_CATS")
        CONFIG["STATS_HIDDEN_CATS"] = dump_hidden_cat_keys(keys)
        if not update_single_config_parameter("STATS_HIDDEN_CATS"):
            CONFIG["STATS_HIDDEN_CATS"] = previous
            return
        stats_hidden_cats_revision.set(stats_hidden_cats_revision.get() + 1)

    @reactive.effect
    @reactive.event(input.stats_cat_visibility, ignore_none=True)
    def on_stats_cat_visibility():
        parsed = _parse_visibility_input(input.stats_cat_visibility())
        if parsed is None:
            return
        key, hide = parsed
        _update_hidden_cat(key, hide=hide)

    @output
    @render.ui
    def ui_statistics_dashboard():
        payload = _stats_dashboard_payload()

        if payload.get("empty"):
            return _empty_state(bool(payload.get("has_history")))

        kpis = payload.get("kpis") or {}
        payload_json = json.dumps(payload, ensure_ascii=True, default=str).replace(
            "<", "\\u003c"
        )

        return ui.div(
            ui.div(
                _kpi_card(_("Entries"), str(kpis.get("entries") or 0)),
                _kpi_card(_("Exits"), str(kpis.get("exits") or 0)),
                _kpi_card(
                    _("Prey detections"),
                    str(kpis.get("prey") or 0),
                    _("Entered: {n}").format(n=kpis.get("prey_entered") or 0)
                    + " · "
                    + _("Blocked: {n}").format(n=kpis.get("prey_blocked") or 0),
                ),
                _kpi_card(_("Attempts without passing"), str(kpis.get("attempts") or 0)),
                _kpi_card(
                    _("Median time outside"),
                    str(kpis.get("median_outside") or "—"),
                ),
                _kpi_card(
                    _("Median time inside"),
                    str(kpis.get("median_inside") or "—"),
                ),
                class_="stats-kpis",
            ),
            ui.p(
                _(
                    "Times inside and outside, and the location shown, are based only on "
                    "Kittyflap passages. If a cat uses another way in or out, times may be "
                    "missing or too long, and the location may be wrong until the next flap passage."
                ),
                class_="stats-note",
            ),
            ui.div(
                ui.div(
                    ui.h3(_("Passages"), class_="stats-chart-title"),
                    ui.div(
                        ui.HTML('<canvas id="chart-passages" aria-label="{}"></canvas>'.format(
                            html.escape(_("Passages"))
                        )),
                        class_="stats-chart-canvas",
                    ),
                    class_="stats-chart-card",
                ),
                ui.div(
                    ui.h3(_("Prey detections"), class_="stats-chart-title"),
                    ui.div(
                        ui.HTML('<canvas id="chart-prey"></canvas>'),
                        class_="stats-chart-canvas",
                    ),
                    class_="stats-chart-card",
                ),
                ui.div(
                    ui.h3(_("Attempts without passing"), class_="stats-chart-title"),
                    ui.div(
                        ui.HTML('<canvas id="chart-attempts"></canvas>'),
                        class_="stats-chart-canvas",
                    ),
                    class_="stats-chart-card",
                ),
                ui.div(
                    ui.h3(_("Time inside and outside"), class_="stats-chart-title"),
                    ui.div(
                        ui.HTML('<canvas id="chart-durations"></canvas>'),
                        class_="stats-chart-canvas",
                    ),
                    class_="stats-chart-card",
                ),
                class_="stats-charts",
            ),
            ui.div(
                ui.h3(_("Activity by hour of day"), class_="stats-chart-title"),
                ui.div(
                    ui.HTML(
                        '<canvas id="chart-activity-hour" aria-label="{}"></canvas>'.format(
                            html.escape(_("Activity by hour of day"))
                        )
                    ),
                    class_="stats-chart-canvas",
                ),
                class_="stats-chart-card",
            ),
            ui.HTML(
                f'<script type="application/json" id="stats-payload">{payload_json}</script>'
            ),
            id="stats-dashboard",
            class_="stats-dashboard",
        )

    @output
    @render.ui
    def ui_statistics_cats():
        payload = _stats_dashboard_payload()
        if payload.get("empty"):
            return ui.div()
        stats_hidden_cats_revision.get()
        hidden = parse_hidden_cat_keys(CONFIG.get("STATS_HIDDEN_CATS"))
        return _cat_section_ui(payload, _dashboard_labels(), hidden)
