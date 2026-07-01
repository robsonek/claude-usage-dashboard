import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def test_css_has_new_tokens():
    css = _read("static/style.css")
    for token in ("--bg:", "--panel:", "--accent:", "--ok:", "--warn:",
                  "--crit:", "--line-session:",
                  "--reset-marker-color:", "--mono:"):
        assert token in css, f"missing token {token}"


def test_css_has_no_light_theme():
    css = _read("static/style.css")
    assert '[data-theme="light"]' not in css
    assert ".theme-toggle" not in css


def test_dashboard_has_no_theme_toggle():
    html = _read("templates/dashboard.html")
    assert "theme-toggle" not in html
    assert "toggleTheme" not in html
    assert "data-theme" not in html
    assert 'content="#0a0e12"' in html  # static dark meta color


def test_dashboard_has_freshness_indicator():
    html = _read("templates/dashboard.html")
    assert 'id="freshness"' in html
    assert 'id="fresh-text"' in html
    assert "updateFreshness" in html


def test_status_cards_have_value_and_trend_hooks():
    html = _read("templates/dashboard.html")
    for hook in ('id="weekly-used"', 'id="session-used"', 'id="model-used"',
                 'id="weekly-trend"', 'id="session-trend"', 'id="model-trend"',
                 'id="card-weekly"', 'id="card-session"', 'id="card-model"'):
        assert hook in html, f"missing {hook}"


def test_chart_colors_swapped_to_palette():
    html = _read("templates/dashboard.html")
    assert "#22d3ee" in html      # weekly line (cyan)
    assert "#10b981" in html      # session line (green)
    assert "#a78bfa" in html      # model line (violet)
    assert "#3498db" not in html  # old blue removed
    assert "#9b59b6" not in html  # old purple removed
    # The per-model card returned 2026-07-01 (weekly_scoped/Fable) with a dynamic
    # title — no hardcoded model name anywhere in the markup.
    assert "Sonnet Usage (% used)" not in html
    assert 'id="model-chart-title"' in html


def test_status_cards_have_stale_value_markers():
    html = _read("templates/dashboard.html")
    for hook in ('id="weekly-stale"', 'id="session-stale"', 'id="model-stale"',
                 'setStaleBadge', 'is-stale', 'staleAge'):
        assert hook in html, f"missing {hook}"
    css = _read("static/style.css")
    assert ".stale-badge" in css
    assert ".is-stale" in css


def test_apply_status_color_preserves_is_stale():
    """applyStatusColor must toggle only severity classes via classList, never
    reassign className wholesale (which would wipe the is-stale marker)."""
    html = _read("templates/dashboard.html")
    assert "card.classList.remove('ok', 'warning', 'critical')" in html
    assert "'status-card ' + cls" not in html, \
        "wholesale className reset would drop the is-stale class"


def test_setStatusCard_clears_stale_badge_when_quota_absent():
    """When a quota is absent, setStatusCard resets the card to blanks and clears
    any prior stale marker, rather than leaving a frozen value on screen across
    refreshes."""
    html = _read("templates/dashboard.html")
    assert "setStaleBadge(prefix, null)" in html
    assert "if (!q)" in html  # the blank-reset branch in setStatusCard


def test_app_version_is_shown_in_ui():
    """The version is a valid semver in config and surfaced in the dashboard only
    (header chip + footer) — intentionally NOT on the login screen."""
    import config
    assert re.match(r'^\d+\.\d+\.\d+$', config.VERSION), config.VERSION
    dash = _read("templates/dashboard.html")
    assert 'class="version-badge"' in dash
    assert dash.count("v{{ version }}") >= 2  # header chip + footer
    login = _read("templates/login.html")
    assert "{{ version }}" not in login   # version is dashboard-only (post-login)
    assert "login-version" not in login
    css = _read("static/style.css")
    assert ".version-badge" in css


def test_login_has_no_theme_toggle():
    html = _read("templates/login.html")
    assert "theme-toggle" not in html
    assert "toggleTheme" not in html
    assert "data-theme" not in html


def test_status_color_is_forecast_aware():
    html = _read("templates/dashboard.html")
    assert "setStatusForecast" in html
    assert "applyStatusColor" in html
    assert "willExceed" in html


def test_ui_text_is_english():
    html = _read("templates/dashboard.html")
    for pl in ("temu", "teraz", "Wiek ostatniego"):
        assert pl not in html, f"Polish text leaked: {pl}"


def test_session_chart_has_daily_peak_envelope():
    html = _read("templates/dashboard.html")
    assert "dailyPeakEnvelope" in html
    assert "Daily peak" in html


def test_dashboard_uses_smart_polling_heartbeat():
    """Refresh is event-driven: a lightweight heartbeat polls the latest
    snapshot timestamp and only triggers the heavy refresh when it changed."""
    html = _read("templates/dashboard.html")
    assert "POLL_INTERVAL_MS" in html
    assert "lastSnapshotTs" in html
    assert "heartbeat" in html
    # re-check on tab focus so coming back doesn't wait for the next tick
    assert "visibilitychange" in html


def test_dashboard_dropped_blind_5min_full_refresh():
    """The old blind 'full refresh every 5 minutes' interval must be gone —
    heavy refreshes are now gated on a new snapshot, not a wall clock."""
    html = _read("templates/dashboard.html")
    assert "5 * 60 * 1000" not in html


def test_dashboard_does_not_load_scripts_from_third_party_cdn():
    """Audit point 3: no runtime <script src> to a third-party CDN. A floating
    version with no SRI means a compromised CDN injects arbitrary JS into the
    logged-in dashboard. Chart libs are vendored under static/ instead."""
    html = _read("templates/dashboard.html")
    for cdn in ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com"):
        assert cdn not in html, f"third-party CDN reference still present: {cdn}"


def test_dashboard_vendors_chart_libs_locally():
    html = _read("templates/dashboard.html")
    assert "vendor/chart.umd.min.js" in html
    assert "vendor/chartjs-adapter-date-fns.bundle.min.js" in html
    chart = os.path.join(BASE, "static/vendor/chart.umd.min.js")
    adapter = os.path.join(BASE, "static/vendor/chartjs-adapter-date-fns.bundle.min.js")
    assert os.path.exists(chart), "vendored Chart.js missing"
    assert os.path.exists(adapter), "vendored date-fns adapter missing"
    # Sanity: non-empty, looks like the real minified library, not an error page.
    assert os.path.getsize(chart) > 50_000
    assert os.path.getsize(adapter) > 20_000


def test_vendored_libs_have_no_dangling_sourcemap_ref():
    """The .map files aren't vendored (no value for a third-party lib), so the
    minified files must not reference one — otherwise devtools requests
    chart.umd.js.map and gets a 404 in the console."""
    for name in ("chart.umd.min.js", "chartjs-adapter-date-fns.bundle.min.js"):
        content = _read(os.path.join("static/vendor", name))
        assert "sourceMappingURL" not in content, f"{name} still references a source map"
