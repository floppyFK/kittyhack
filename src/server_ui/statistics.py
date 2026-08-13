"""Statistics tab: filters, KPIs, charts, heatmap, per-cat cards."""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from shiny import render, ui
from zoneinfo import ZoneInfo
from faicons import icon_svg

from src.baseconfig import CONFIG, set_language
from src.database import CatsRepo, ReturnDataCatDB, VisitStatsRepo
from src.helper import DateTimeUtil
from src.server_ui.context import SessionContext
from src.server_ui.state import reload_trigger_photos
from src.statistics import (
    CAT_FILTER_ALL,
    CAT_FILTER_UNKNOWN,
    DashboardLabels,
    build_dashboard,
    format_duration,
)

_ = set_language(CONFIG["LANGUAGE"])


def _dashboard_labels() -> DashboardLabels:
    return DashboardLabels(
        unknown=_("Unknown"),
        entries=_("Entries"),
        exits=_("Exits"),
        in_label=_("In"),
        out_label=_("Out"),
        prey=_("Prey"),
        prey_entered=_("Entered with prey"),
        prey_blocked=_("Prey blocked"),
        attempts_entry=_("Entry attempts"),
        attempts_exit=_("Denied exits"),
        median_outside=_("Median time outside"),
        median_inside=_("Median time inside"),
        weekdays=(
            _("Mon"),
            _("Tue"),
            _("Wed"),
            _("Thu"),
            _("Fri"),
            _("Sat"),
            _("Sun"),
        ),
        duration_d=_("d"),
        duration_h=_("h"),
        duration_m=_("m"),
        inside=_("Inside"),
        outside=_("Outside"),
        location_unknown=_("Unknown"),
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


def _heatmap_html(payload: dict) -> str:
    heat = payload.get("heatmap") or {}
    weekdays = heat.get("weekdays") or []
    hours = heat.get("hours") or []
    cells = heat.get("cells") or []
    max_val = 1
    for row in cells:
        for n in row:
            try:
                max_val = max(max_val, int(n))
            except (TypeError, ValueError):
                pass
    hour_cells = "".join(
        f'<div class="stats-heatmap-hour">{html.escape(str(h))}</div>' for h in hours
    )
    body = []
    for i, day in enumerate(weekdays):
        row = cells[i] if i < len(cells) else [0] * 24
        parts = [f'<div class="stats-heatmap-day">{html.escape(str(day))}</div>']
        for hour, n in enumerate(row):
            try:
                count = int(n)
            except (TypeError, ValueError):
                count = 0
            pct = 0 if max_val <= 0 else int(round(100 * count / max_val))
            title = html.escape(f"{day} {hour:02d}:00 — {count}")
            parts.append(
                f'<div class="stats-heatmap-cell" data-n="{count}" '
                f'style="--stats-heat:{pct}%" title="{title}"></div>'
            )
        body.append(f'<div class="stats-heatmap-row">{"".join(parts)}</div>')
    return (
        f'<div class="stats-heatmap" role="img" '
        f'aria-label="{html.escape(_("Activity by hour of day"))}">'
        f'<div class="stats-heatmap-row stats-heatmap-head">'
        f'<div class="stats-heatmap-day"></div>{hour_cells}</div>'
        f'{"".join(body)}</div>'
    )


def _location_label(location: str, labels: DashboardLabels) -> str:
    if location == "inside":
        return labels.inside
    if location == "outside":
        return labels.outside
    return labels.location_unknown


def _cat_cards_ui(payload: dict, labels: DashboardLabels):
    cards = []
    for cat in payload.get("cats") or []:
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
        cards.append(
            ui.div(
                ui.div(
                    img,
                    ui.div(
                        ui.span(name, class_="stats-cat-name"),
                        ui.span(_location_label(loc, labels), class_=loc_class),
                        class_="stats-cat-identity",
                    ),
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
                ui.div(
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
                    class_="stats-cat-extra",
                ),
                class_="stats-cat-card",
            )
        )
    if not cards:
        return ui.div()
    return ui.div(*cards, class_="stats-cat-grid")


def _empty_state(has_history: bool):
    if has_history:
        msg = _("No events in this time range.")
        detail = _("Try a longer range or another cat.")
    else:
        msg = _("No statistics yet")
        detail = _(
            "Statistics are stored independently of pictures. Activity that was already "
            "deleted from the picture archive cannot be reconstructed. New flap events "
            "will appear here."
        )
    return ui.div(
        ui.h3(msg, class_="stats-empty-title"),
        ui.p(detail, class_="stats-empty-text"),
        class_="stats-empty",
    )


def _numbers_table(payload: dict) -> str:
    kpis = payload.get("kpis") or {}
    rows = [
        (_("Entries"), kpis.get("entries", 0)),
        (_("Exits"), kpis.get("exits", 0)),
        (_("Uncertain entries"), kpis.get("uncertain_entries", 0)),
        (_("Prey detections"), kpis.get("prey", 0)),
        (_("Entered with prey"), kpis.get("prey_entered", 0)),
        (_("Prey blocked"), kpis.get("prey_blocked", 0)),
        (_("Attempts without passing"), kpis.get("attempts", 0)),
        (_("Denied entry"), kpis.get("denied_entry", 0)),
        (_("Denied exit"), kpis.get("denied_exit", 0)),
        (_("Avg. time outside"), kpis.get("avg_outside", "—")),
        (_("Median time outside"), kpis.get("median_outside", "—")),
        (_("Avg. time inside"), kpis.get("avg_inside", "—")),
        (_("Median time inside"), kpis.get("median_inside", "—")),
    ]
    body = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )
    return (
        f'<details class="stats-numbers"><summary>{html.escape(_("Show numbers"))}</summary>'
        f'<table class="stats-numbers-table"><tbody>{body}</tbody></table></details>'
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
                        "auto": _("Auto"),
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
                        _("From / to"),
                        start=today - timedelta(days=30),
                        end=today,
                        format=CONFIG.get("DATE_FORMAT", "yyyy-mm-dd"),
                        width="100%",
                    ),
                ),
                class_="stats-filters",
            ),
            ui.output_ui("ui_statistics_dashboard"),
            class_="stats-page",
        )

    @output
    @render.ui
    def ui_statistics_dashboard():
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
                    _("Avg. / median outside"),
                    f"{kpis.get('avg_outside') or '—'} / {kpis.get('median_outside') or '—'}",
                ),
                _kpi_card(
                    _("Avg. / median inside"),
                    f"{kpis.get('avg_inside') or '—'} / {kpis.get('median_inside') or '—'}",
                ),
                class_="stats-kpis",
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
                    ui.h3(_("Time inside / outside"), class_="stats-chart-title"),
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
                ui.HTML(_heatmap_html(payload)),
                class_="stats-chart-card stats-heatmap-card",
            ),
            ui.h3(_("Per cat"), class_="stats-section-title"),
            _cat_cards_ui(payload, labels),
            ui.HTML(_numbers_table(payload)),
            ui.HTML(
                f'<script type="application/json" id="stats-payload">{payload_json}</script>'
            ),
            id="stats-dashboard",
            class_="stats-dashboard",
        )
