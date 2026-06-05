import os

BASE = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def test_css_has_new_tokens():
    css = _read("static/style.css")
    for token in ("--bg:", "--panel:", "--accent:", "--ok:", "--warn:",
                  "--crit:", "--line-session:", "--line-sonnet:",
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
    assert "#a78bfa" in html      # sonnet line (violet)
    assert "#3498db" not in html  # old blue removed
    assert "#9b59b6" not in html  # old purple removed
    assert "Sonnet Usage (% used)" in html


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
