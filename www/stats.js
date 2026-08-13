// Kittyhack Statistics tab — Chart.js dashboard (offline vendor bundle).
(function () {
    "use strict";

    var charts = [];
    var lastSig = "";
    var themeObserver = null;

    function q(sel, root) {
        try { return (root || document).querySelector(sel); } catch (e) { return null; }
    }

    function cssVar(name, fallback) {
        try {
            var val = getComputedStyle(document.documentElement).getPropertyValue(name);
            val = (val || "").trim();
            return val || fallback;
        } catch (e) {
            return fallback;
        }
    }

    function themeColors() {
        return {
            text: cssVar("--bs-body-color", "#212529"),
            muted: cssVar("--bs-secondary-color", "#6c757d"),
            grid: cssVar("--bs-border-color", "rgba(0,0,0,0.12)"),
            bg: cssVar("--bs-body-bg", "#fff"),
            primary: cssVar("--bs-primary", "#0d6efd"),
            success: cssVar("--bs-success", "#198754"),
            danger: cssVar("--bs-danger", "#dc3545"),
            warning: cssVar("--bs-warning", "#ffc107"),
            info: cssVar("--bs-info", "#0dcaf0")
        };
    }

    function palette() {
        var c = themeColors();
        return [c.primary, c.success, c.danger, c.warning, c.info, "#6f42c1", "#fd7e14", "#20c997"];
    }

    function withAlpha(color, alpha) {
        if (!color) return color;
        if (color.indexOf("rgb") === 0) {
            return color.replace(/rgba?\(([^)]+)\)/, function (_, inner) {
                var parts = inner.split(",").map(function (p) { return p.trim(); });
                return "rgba(" + parts[0] + ", " + parts[1] + ", " + parts[2] + ", " + alpha + ")";
            });
        }
        var hex = color.replace("#", "");
        if (hex.length === 3) {
            hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
        }
        if (hex.length !== 6) return color;
        var r = parseInt(hex.slice(0, 2), 16);
        var g = parseInt(hex.slice(2, 4), 16);
        var b = parseInt(hex.slice(4, 6), 16);
        return "rgba(" + r + ", " + g + ", " + b + ", " + alpha + ")";
    }

    function destroyCharts() {
        charts.forEach(function (ch) {
            try { ch.destroy(); } catch (e) {}
        });
        charts = [];
    }

    function parseJsonScript(id) {
        var el = q("#" + id);
        if (!el) return null;
        try {
            return JSON.parse(el.textContent || "null");
        } catch (e) {
            return null;
        }
    }

    function commonOptions(colors, stacked) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    labels: { color: colors.text, boxWidth: 12, font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: colors.bg,
                    titleColor: colors.text,
                    bodyColor: colors.text,
                    borderColor: colors.grid,
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    stacked: !!stacked,
                    ticks: { color: colors.muted, maxRotation: 45, minRotation: 0, font: { size: 10 } },
                    grid: { color: withAlpha(colors.grid, 0.45) }
                },
                y: {
                    stacked: !!stacked,
                    beginAtZero: true,
                    ticks: { color: colors.muted, precision: 0 },
                    grid: { color: withAlpha(colors.grid, 0.45) }
                }
            }
        };
    }

    function makeChart(canvasId, config) {
        if (typeof window.Chart !== "function") return;
        var canvas = q("#" + canvasId);
        if (!canvas) return;
        var existing = window.Chart.getChart ? window.Chart.getChart(canvas) : null;
        if (existing) {
            try { existing.destroy(); } catch (e) {}
        }
        var chart = new window.Chart(canvas, config);
        charts.push(chart);
    }

    function renderDashboard() {
        var root = q("#stats-dashboard");
        if (!root) {
            destroyCharts();
            lastSig = "";
            return;
        }
        var payloadEl = q("#stats-payload");
        var raw = payloadEl ? (payloadEl.textContent || "") : "";
        var theme = "";
        try { theme = document.documentElement.getAttribute("data-bs-theme") || ""; } catch (e) {}
        var sig = theme + "\n" + raw;
        var liveCanvas = q("#chart-passages");
        var hasLiveChart = !!(
            liveCanvas && window.Chart && window.Chart.getChart && window.Chart.getChart(liveCanvas)
        );
        if (sig === lastSig && hasLiveChart) return;
        lastSig = sig;

        var payload = parseJsonScript("stats-payload");
        if (!payload || payload.empty) {
            destroyCharts();
            return;
        }
        destroyCharts();
        var colors = themeColors();
        var pal = palette();

        var passageSets = (payload.passages && payload.passages.datasets) ? payload.passages.datasets : [];
        makeChart("chart-passages", {
            type: "bar",
            data: {
                labels: (payload.passages && payload.passages.labels) || [],
                datasets: passageSets.map(function (ds, i) {
                    var color = pal[Math.floor(i / 2) % pal.length];
                    var isOut = ds.direction === "out";
                    return {
                        label: ds.label,
                        data: ds.data,
                        stack: ds.stack || "passages",
                        backgroundColor: withAlpha(color, isOut ? 0.45 : 0.85),
                        borderColor: color,
                        borderWidth: 1,
                        borderRadius: 4
                    };
                })
            },
            options: commonOptions(colors, true)
        });

        var prey = payload.prey || {};
        makeChart("chart-prey", {
            type: "bar",
            data: {
                labels: prey.labels || [],
                datasets: [
                    {
                        label: prey.entered_label || "Entered",
                        data: prey.entered || [],
                        backgroundColor: withAlpha(colors.danger, 0.8),
                        borderRadius: 4
                    },
                    {
                        label: prey.blocked_label || "Blocked",
                        data: prey.blocked || [],
                        backgroundColor: withAlpha(colors.warning, 0.8),
                        borderRadius: 4
                    }
                ]
            },
            options: commonOptions(colors, true)
        });

        var attempts = payload.attempts || {};
        makeChart("chart-attempts", {
            type: "bar",
            data: {
                labels: attempts.labels || [],
                datasets: [
                    {
                        label: attempts.entry_label || "Entry",
                        data: attempts.entry || [],
                        backgroundColor: withAlpha(colors.primary, 0.8),
                        borderRadius: 4
                    },
                    {
                        label: attempts.exit_label || "Exit",
                        data: attempts.exit || [],
                        backgroundColor: withAlpha(colors.info, 0.8),
                        borderRadius: 4
                    }
                ]
            },
            options: commonOptions(colors, true)
        });

        var durations = payload.durations || {};
        var durOptions = commonOptions(colors, false);
        durOptions.scales.y.ticks.precision = undefined;
        durOptions.scales.y.ticks.callback = function (value) {
            if (value >= 86400) return Math.round(value / 86400) + "d";
            if (value >= 3600) return Math.round(value / 3600) + "h";
            if (value >= 60) return Math.round(value / 60) + "m";
            return value;
        };
        makeChart("chart-durations", {
            type: "line",
            data: {
                labels: durations.labels || [],
                datasets: [
                    {
                        label: durations.outside_label || "Outside",
                        data: durations.outside || [],
                        borderColor: colors.primary,
                        backgroundColor: withAlpha(colors.primary, 0.12),
                        fill: true,
                        spanGaps: true,
                        tension: 0.25,
                        pointRadius: 3
                    },
                    {
                        label: durations.inside_label || "Inside",
                        data: durations.inside || [],
                        borderColor: colors.success,
                        backgroundColor: withAlpha(colors.success, 0.12),
                        fill: true,
                        spanGaps: true,
                        tension: 0.25,
                        pointRadius: 3
                    }
                ]
            },
            options: durOptions
        });
    }

    function scheduleRender() {
        try { renderDashboard(); } catch (e) {}
    }

    function install() {
        if (window.__khStatsInstalled) return;
        window.__khStatsInstalled = true;

        function run() { scheduleRender(); }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", run, { once: true });
        } else {
            setTimeout(run, 0);
        }

        try {
            var scheduled = false;
            var obs = new MutationObserver(function () {
                if (scheduled) return;
                scheduled = true;
                setTimeout(function () {
                    scheduled = false;
                    run();
                }, 40);
            });
            obs.observe(document.body || document.documentElement, { childList: true, subtree: true });
        } catch (e) {}

        try {
            themeObserver = new MutationObserver(function () { run(); });
            themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-bs-theme"] });
        } catch (e) {}
    }

    install();
})();
