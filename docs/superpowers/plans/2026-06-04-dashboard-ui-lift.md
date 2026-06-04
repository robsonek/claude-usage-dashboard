# Dashboard UI Lift — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reskin the dashboard to a dark-only "Cyan Terminal" data-dense look (airy, borderless panels) without changing what data is shown or where.

**Architecture:** Pure front-end change. Rewrite `static/style.css` into one dark design system, then rewire `templates/dashboard.html` markup + inline JS to the new classes/tokens and remove the light theme. No backend, DB, collector, or endpoint changes — all needed data already comes from `/api/current` and `/api/prediction`.

**Tech Stack:** Flask templates (Jinja2), vanilla JS, Chart.js (CDN), plain CSS with custom properties. Tests: pytest (file-content guardrails; visual checks are manual).

**Spec:** `docs/superpowers/specs/2026-06-04-dashboard-ui-lift-design.md`

---

## File Structure

- `static/style.css` — **rewritten** into the final dark design system (tokens + every component). One cohesive stylesheet; this is the single source of visual truth.
- `templates/dashboard.html` — markup + inline JS rewired to new classes; theme toggle + light-mode JS removed; freshness indicator, section labels, status-card big numbers + trend, prediction badge/kv-grid, Chart.js colors.
- `templates/login.html` — reskin; theme toggle + light JS removed.
- `test_dashboard_ui.py` — **new**; pytest guardrails that the light theme is gone and the new structure/tokens exist (these are easy to regress and worth locking; pixel appearance stays manual).

Why CSS is one task, not split per component: a stylesheet is a single cohesive unit. Splitting it across tasks would leave broken intermediate stylesheets and duplicate rules. The HTML/JS tasks that follow only add markup that consumes classes already defined in Task 1.

Testing approach for a visual reskin: automated tests cover **structural invariants** (light theme removed, tokens present, DOM hooks present, chart colors swapped). **Appearance** (does it look right) is verified manually by running the app and comparing to the companion mockups (`.superpowers/brainstorm/.../fullpage.html`, `weight.html`). This is honest — pixel assertions would be brittle and low-value.

---

## Task 1: New stylesheet (dark design system, no light theme)

**Files:**
- Modify: `static/style.css` (full rewrite)
- Test: `test_dashboard_ui.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_dashboard_ui.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_dashboard_ui.py -v`
Expected: FAIL — current `style.css` still contains `[data-theme="light"]` and `.theme-toggle`, and lacks the new tokens.

- [ ] **Step 3: Replace `static/style.css` entirely with the new design system**

Overwrite the whole file with:

```css
/* Claude Usage Dashboard — Cyan Terminal (dark-only, data-dense, airy) */
:root{
  --bg:#0a0e12;
  --panel:#0f161d;
  --panel-2:#0a1018;
  --border:#15202b;
  --text:#e8f0f5;
  --mid:#8499ab;
  --label:#5b7083;
  --accent:#22d3ee;
  --accent-soft:rgba(34,211,238,.10);
  --accent-border:rgba(34,211,238,.35);
  --ok:#10b981;
  --warn:#f5a524;
  --crit:#f43f5e;
  --line-weekly:#22d3ee;
  --line-session:#10b981;
  --line-sonnet:#a78bfa;
  --reset-marker-color:rgba(120,150,170,.5);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,sans-serif;
}

*{margin:0;padding:0;box-sizing:border-box}
html{background:var(--bg)}
body{
  font-family:var(--sans);
  background:var(--bg);
  min-height:100vh;
  color:var(--text);
  overflow-x:hidden;
  padding-top:env(safe-area-inset-top);
  padding-bottom:env(safe-area-inset-bottom);
  padding-left:env(safe-area-inset-left);
  padding-right:env(safe-area-inset-right);
}

/* Layout */
.dashboard{max-width:1320px;margin:0 auto;padding:22px}

/* Header */
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px;padding-bottom:16px;border-bottom:1px solid var(--border)}
header h1{font-size:1.15rem;font-weight:600;letter-spacing:-.01em}
.header-actions{display:flex;gap:10px;align-items:center}

/* Freshness indicator */
.freshness{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:.72rem;color:var(--mid);border:1px solid var(--border);background:var(--panel);padding:5px 10px;border-radius:7px}
.fresh-dot{width:7px;height:7px;border-radius:50%;background:var(--mid)}
.freshness.fresh{color:var(--ok)}
.freshness.fresh .fresh-dot{background:var(--ok);box-shadow:0 0 7px var(--ok)}
.freshness.stale{color:var(--warn)}
.freshness.stale .fresh-dot{background:var(--warn);box-shadow:0 0 7px var(--warn)}
.freshness.dead{color:var(--crit)}
.freshness.dead .fresh-dot{background:var(--crit);box-shadow:0 0 7px var(--crit)}

/* Buttons */
.btn-refresh,.btn-logout{font-family:var(--mono);padding:6px 13px;border-radius:7px;font-size:.78rem;cursor:pointer;transition:all .15s;text-decoration:none;border:1px solid var(--border);background:var(--panel)}
.btn-refresh{color:var(--accent);border-color:var(--accent-border);background:var(--accent-soft)}
.btn-refresh:hover{background:rgba(34,211,238,.18)}
.btn-logout{color:var(--crit);border-color:rgba(244,63,94,.3)}
.btn-logout:hover{background:rgba(244,63,94,.12)}

/* Section labels */
.section-label{font-family:var(--mono);font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--label);margin:0 0 12px 2px}

/* Status cards */
.current-status{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:26px}
.status-card{background:var(--panel);border-radius:9px;padding:15px 16px}
.status-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:9px}
.status-name{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--label);display:flex;align-items:center}
.status-name .dot{width:7px;height:7px;border-radius:50%;background:var(--mid);margin-right:7px;flex:none}
.status-card.ok .dot{background:var(--ok)}
.status-card.warning .dot{background:var(--warn)}
.status-card.critical .dot{background:var(--crit)}
.status-trend{font-family:var(--mono);font-size:.62rem;color:var(--mid)}
.status-value{font-family:var(--mono);font-weight:700;font-size:1.9rem;letter-spacing:-.02em;color:var(--text);display:flex;align-items:baseline;gap:7px;margin-bottom:11px}
.status-value .u{font-size:.8rem;color:var(--label);font-weight:400}
.progress-container{height:6px;background:var(--panel-2);border-radius:3px;overflow:hidden;margin-bottom:9px}
.progress-bar{height:100%;border-radius:3px;transition:width .5s ease;background:var(--mid)}
.progress-bar.ok{background:var(--ok)}
.progress-bar.warning{background:var(--warn)}
.progress-bar.critical{background:var(--crit)}
.status-details{display:flex;justify-content:space-between;font-family:var(--mono);font-size:.66rem;color:var(--label)}
.remaining{color:var(--mid)}
.reset-time{color:var(--label)}

/* Prediction */
.prediction-panel{margin-bottom:26px}
.predictions{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.prediction-card{background:var(--panel);border-radius:9px;padding:15px 16px}
.prediction-card h4{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--label);margin-bottom:11px}
.pred-badge{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:.7rem;font-weight:600;padding:4px 10px;border-radius:6px;margin-bottom:13px}
.pred-badge.ok{color:var(--ok);background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.25)}
.pred-badge.warning{color:var(--crit);background:rgba(244,63,94,.12);border:1px solid rgba(244,63,94,.25)}
.pred-badge.pending{color:var(--mid);background:rgba(132,153,171,.1);border:1px solid var(--border)}
.pred-kv{display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-family:var(--mono);font-size:.78rem}
.pred-kv dt{color:var(--label)}
.pred-kv dd{color:var(--text);text-align:right;font-weight:600}
.pred-kv dd.warn{color:var(--crit)}
.no-data{color:var(--label);font-style:italic;font-family:var(--mono);font-size:.78rem}

/* Chart controls */
.chart-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px;padding:10px 14px;background:var(--panel);border-radius:9px}
.chart-controls span{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--label);margin-right:4px}
.time-btn{font-family:var(--mono);padding:5px 11px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--mid);cursor:pointer;font-size:.72rem;transition:all .15s}
.time-btn:hover{border-color:var(--accent-border);color:var(--accent)}
.time-btn.active{background:var(--accent-soft);border-color:var(--accent-border);color:var(--accent);font-weight:600}

/* Charts */
.charts{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:26px}
.chart-container{background:var(--panel);border-radius:9px;padding:16px 16px 10px;overflow:hidden}
.chart-container h2{font-family:var(--mono);font-size:.78rem;font-weight:500;color:var(--mid);letter-spacing:.03em;margin-bottom:12px}
.chart-container canvas{height:280px!important;max-width:100%}

/* Footer */
footer{text-align:center;padding:16px 0 4px;font-family:var(--mono);color:var(--label);font-size:.7rem}

/* Login */
.login-page{display:flex;justify-content:center;align-items:center;min-height:100vh}
.login-container{width:100%;max-width:420px;padding:20px}
.login-box{background:var(--panel);border-radius:12px;padding:36px;border:1px solid var(--border)}
.login-box h1{text-align:center;margin-bottom:8px;font-size:1.4rem}
.login-box .subtitle{text-align:center;color:var(--label);margin-bottom:28px;font-family:var(--mono);font-size:.8rem}
.form-group{margin-bottom:18px}
.form-group label{display:block;margin-bottom:8px;color:var(--mid);font-family:var(--mono);font-size:.72rem;letter-spacing:.08em;text-transform:uppercase}
.form-group input{width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:8px;background:var(--panel-2);color:var(--text);font-size:.95rem;transition:border-color .2s}
.form-group input:focus{outline:none;border-color:var(--accent)}
.btn-login{width:100%;padding:13px;background:var(--accent-soft);border:1px solid var(--accent-border);border-radius:8px;color:var(--accent);font-size:.9rem;font-weight:600;font-family:var(--mono);cursor:pointer;transition:all .2s}
.btn-login:hover{background:rgba(34,211,238,.18)}
.error-message{background:rgba(244,63,94,.12);border:1px solid rgba(244,63,94,.3);color:var(--crit);padding:11px;border-radius:8px;margin-bottom:18px;text-align:center;font-family:var(--mono);font-size:.78rem}

/* Responsive */
@media(max-width:768px){
  .dashboard{padding:14px}
  header{flex-direction:column;gap:12px;text-align:center}
  .current-status{grid-template-columns:1fr}
  .predictions{grid-template-columns:1fr}
  .chart-container canvas{height:220px!important}
}
@media(max-width:380px){
  .dashboard{padding:10px}
  .status-value{font-size:1.6rem}
  .chart-container canvas{height:190px!important}
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test_dashboard_ui.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add static/style.css test_dashboard_ui.py
git commit -m "feat(ui): dark-only Cyan Terminal stylesheet"
```

---

## Task 2: Remove theme toggle and light-mode JS from dashboard.html

**Files:**
- Modify: `templates/dashboard.html`
- Test: `test_dashboard_ui.py`

- [ ] **Step 1: Add failing test**

Append to `test_dashboard_ui.py`:

```python
def test_dashboard_has_no_theme_toggle():
    html = _read("templates/dashboard.html")
    assert "theme-toggle" not in html
    assert "toggleTheme" not in html
    assert "data-theme" not in html
    assert 'content="#0a0e12"' in html  # static dark meta color
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest test_dashboard_ui.py::test_dashboard_has_no_theme_toggle -v`
Expected: FAIL — `theme-toggle`/`toggleTheme`/`data-theme` still present.

- [ ] **Step 3: Edit the `<head>` block**

Replace:

```html
    <meta name="theme-color" content="#1a1a2e" id="meta-theme-color">
    <title>Claude Usage Dashboard</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
    <script>
        // Apply theme immediately to prevent flash
        (function() {
            const theme = localStorage.getItem('theme') || 'dark';
            document.documentElement.setAttribute('data-theme', theme);
            var color = theme === 'dark' ? '#1a1a2e' : '#f5f7fa';
            document.getElementById('meta-theme-color').setAttribute('content', color);
        })();
    </script>
```

with:

```html
    <meta name="theme-color" content="#0a0e12">
    <title>Claude Usage Dashboard</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
```

- [ ] **Step 4: Remove the toggle button from the header**

Replace:

```html
            <div class="header-actions">
                <button id="theme-toggle" class="theme-toggle">🌙</button>
                <button id="refresh-btn" class="btn-refresh">🔄 Refresh</button>
                <a href="{{ url_for('logout') }}" class="btn-logout">Logout</a>
            </div>
```

with (note: freshness indicator markup is added in Task 3; for now just drop the toggle):

```html
            <div class="header-actions">
                <button id="refresh-btn" class="btn-refresh">↻ Refresh</button>
                <a href="{{ url_for('logout') }}" class="btn-logout">Logout</a>
            </div>
```

- [ ] **Step 5: Remove the theme JS functions**

Delete this block:

```javascript
        // Theme toggle
        function initTheme() {
            const savedTheme = localStorage.getItem('theme') || 'dark';
            document.documentElement.setAttribute('data-theme', savedTheme);
            updateThemeIcon(savedTheme);
        }

        function toggleTheme() {
            const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeIcon(newTheme);
            document.getElementById('meta-theme-color').setAttribute('content', newTheme === 'dark' ? '#1a1a2e' : '#f5f7fa');
        }

        function updateThemeIcon(theme) {
            const btn = document.getElementById('theme-toggle');
            btn.textContent = theme === 'dark' ? '☀️' : '🌙';
        }

```

- [ ] **Step 6: Remove theme wiring in `DOMContentLoaded`**

In the `document.addEventListener('DOMContentLoaded', ...)` body, delete the line:

```javascript
            initTheme();
```

and delete:

```javascript
            // Theme toggle button
            document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest test_dashboard_ui.py::test_dashboard_has_no_theme_toggle -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add templates/dashboard.html
git commit -m "refactor(ui): drop light theme + toggle from dashboard"
```

---

## Task 3: Header freshness indicator

**Files:**
- Modify: `templates/dashboard.html`
- Test: `test_dashboard_ui.py`

- [ ] **Step 1: Add failing test**

Append to `test_dashboard_ui.py`:

```python
def test_dashboard_has_freshness_indicator():
    html = _read("templates/dashboard.html")
    assert 'id="freshness"' in html
    assert 'id="fresh-text"' in html
    assert "updateFreshness" in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest test_dashboard_ui.py::test_dashboard_has_freshness_indicator -v`
Expected: FAIL.

- [ ] **Step 3: Add freshness markup to the header**

Replace:

```html
            <div class="header-actions">
                <button id="refresh-btn" class="btn-refresh">↻ Refresh</button>
                <a href="{{ url_for('logout') }}" class="btn-logout">Logout</a>
            </div>
```

with:

```html
            <div class="header-actions">
                <span class="freshness" id="freshness" title="Wiek ostatniego snapshotu">
                    <span class="fresh-dot" id="fresh-dot"></span>
                    <span id="fresh-text">--</span>
                </span>
                <button id="refresh-btn" class="btn-refresh">↻ Refresh</button>
                <a href="{{ url_for('logout') }}" class="btn-logout">Logout</a>
            </div>
```

- [ ] **Step 4: Add the `updateFreshness` helper**

Add this function next to the other helper functions (e.g., just before `function formatDate`):

```javascript
        // Freshness indicator: snapshots arrive every ~5 min. Green when fresh,
        // amber when 2+ cycles late, red when the collector has clearly stalled.
        function updateFreshness(ts) {
            const box = document.getElementById('freshness');
            const txt = document.getElementById('fresh-text');
            if (!ts) { box.className = 'freshness'; txt.textContent = '--'; return; }
            const ageMin = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 60000));
            let cls = 'fresh';
            if (ageMin > 30) cls = 'dead';
            else if (ageMin >= 12) cls = 'stale';
            box.className = 'freshness ' + cls;
            txt.textContent = ageMin < 1 ? 'teraz'
                : ageMin < 60 ? `${ageMin}m temu`
                : `${Math.floor(ageMin / 60)}h temu`;
        }
```

- [ ] **Step 5: Call it from `updateCurrentStatus`**

In `updateCurrentStatus`, replace:

```javascript
                document.getElementById('last-update').textContent = data.timestamp ? new Date(data.timestamp).toLocaleString() : '--';
```

with:

```javascript
                document.getElementById('last-update').textContent = data.timestamp ? new Date(data.timestamp).toLocaleString() : '--';
                updateFreshness(data.timestamp);
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest test_dashboard_ui.py::test_dashboard_has_freshness_indicator -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(ui): honest data-freshness indicator in header"
```

---

## Task 4: Section labels

**Files:**
- Modify: `templates/dashboard.html`

- [ ] **Step 1: Add the Status label**

Replace:

```html
        <!-- Current status cards -->
        <section class="current-status">
```

with:

```html
        <!-- Current status cards -->
        <div class="section-label">Status</div>
        <section class="current-status">
```

- [ ] **Step 2: Restyle the Prediction heading**

Replace:

```html
        <section class="prediction-panel">
            <h2>📊 Prediction</h2>
```

with:

```html
        <section class="prediction-panel">
            <div class="section-label">📊 Prediction</div>
```

- [ ] **Step 3: Add the Charts label**

Replace:

```html
        <!-- Charts -->
        <section class="charts">
            <div class="chart-controls">
```

with:

```html
        <!-- Charts -->
        <div class="section-label">Charts</div>
        <section class="charts">
            <div class="chart-controls">
```

- [ ] **Step 4: Manual verification**

Run the app (see Task 9 for the run command). Confirm three mono uppercase labels `STATUS`, `📊 PREDICTION`, `CHARTS` appear above their sections.
Expected: labels render in muted gray mono caps; no layout breakage.

- [ ] **Step 5: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(ui): mono section labels"
```

---

## Task 5: Status cards — big number, status dot, trend

**Files:**
- Modify: `templates/dashboard.html`
- Test: `test_dashboard_ui.py`

- [ ] **Step 1: Add failing test**

Append to `test_dashboard_ui.py`:

```python
def test_status_cards_have_value_and_trend_hooks():
    html = _read("templates/dashboard.html")
    for hook in ('id="weekly-used"', 'id="session-used"', 'id="model-used"',
                 'id="weekly-trend"', 'id="session-trend"', 'id="model-trend"',
                 'id="card-weekly"', 'id="card-session"', 'id="card-model"'):
        assert hook in html, f"missing {hook}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest test_dashboard_ui.py::test_status_cards_have_value_and_trend_hooks -v`
Expected: FAIL.

- [ ] **Step 3: Replace the three status-card markups**

Replace the whole `<section class="current-status"> ... </section>` block with:

```html
        <section class="current-status">
            <div class="status-card" id="card-weekly">
                <div class="status-head">
                    <span class="status-name"><span class="dot"></span>Weekly · 168h</span>
                    <span class="status-trend" id="weekly-trend"></span>
                </div>
                <div class="status-value"><span id="weekly-used">--</span><span class="u">% used</span></div>
                <div class="progress-container">
                    <div class="progress-bar" id="weekly-progress"></div>
                </div>
                <div class="status-details">
                    <span class="remaining" id="weekly-remaining">--</span>
                    <span class="reset-time" id="weekly-reset">reset --</span>
                </div>
            </div>

            <div class="status-card" id="card-session">
                <div class="status-head">
                    <span class="status-name"><span class="dot"></span>Session · 5h</span>
                    <span class="status-trend" id="session-trend"></span>
                </div>
                <div class="status-value"><span id="session-used">--</span><span class="u">% used</span></div>
                <div class="progress-container">
                    <div class="progress-bar" id="session-progress"></div>
                </div>
                <div class="status-details">
                    <span class="remaining" id="session-remaining">--</span>
                    <span class="reset-time" id="session-reset">reset --</span>
                </div>
            </div>

            <div class="status-card" id="card-model">
                <div class="status-head">
                    <span class="status-name"><span class="dot"></span><span id="model-title">Sonnet · 168h</span></span>
                    <span class="status-trend" id="model-trend"></span>
                </div>
                <div class="status-value"><span id="model-used">--</span><span class="u">% used</span></div>
                <div class="progress-container">
                    <div class="progress-bar" id="model-progress"></div>
                </div>
                <div class="status-details">
                    <span class="remaining" id="model-remaining">--</span>
                    <span class="reset-time" id="model-reset">reset --</span>
                </div>
            </div>
        </section>
```

- [ ] **Step 4: Rewrite `updateCurrentStatus` to fill the new markup**

Replace the entire `async function updateCurrentStatus() { ... }` with:

```javascript
        // Fill one status card. `used` drives the big number, the bar width/color
        // (via getProgressClass), the card class (colors the dot), and "% left".
        function setStatusCard(prefix, cardId, q) {
            if (!q) return;
            const used = 100 - (q.percent_remaining || 0);
            const cls = getProgressClass(used);
            document.getElementById(cardId).className = 'status-card ' + cls;
            document.getElementById(prefix + '-used').textContent = used.toFixed(1);
            const bar = document.getElementById(prefix + '-progress');
            bar.style.width = used + '%';
            bar.className = 'progress-bar ' + cls;
            document.getElementById(prefix + '-remaining').textContent = (100 - used).toFixed(1) + '% left';
            document.getElementById(prefix + '-reset').textContent = 'reset ' + formatDate(q.resets_at);
        }

        async function updateCurrentStatus() {
            try {
                const response = await fetch('/api/current');
                const data = await response.json();

                if (data.error) {
                    console.error('Error fetching data:', data.error);
                    return;
                }

                const limits = data.limits || {};

                setStatusCard('weekly', 'card-weekly', limits.weekly);
                setStatusCard('session', 'card-session', limits.session);

                if (limits.model_specific) {
                    const m = limits.model_specific.model;
                    document.getElementById('model-title').textContent =
                        (m ? m.charAt(0).toUpperCase() + m.slice(1) : 'Model') + ' · 168h';
                    setStatusCard('model', 'card-model', limits.model_specific);
                }

                document.getElementById('last-update').textContent = data.timestamp ? new Date(data.timestamp).toLocaleString() : '--';
                updateFreshness(data.timestamp);
            } catch (error) {
                console.error('Error:', error);
            }
        }
```

- [ ] **Step 5: Wire trend onto status cards from the prediction data**

Add this helper just above `function updatePredictionCard`:

```javascript
        // Mirror the prediction trend onto the matching status card. Hidden when
        // there's no usable trend (collecting / stale / null) to avoid noise.
        function setStatusTrend(elId, prediction) {
            const el = document.getElementById(elId);
            if (!el) return;
            if (!prediction || prediction.stale_data || prediction.low_confidence
                || prediction.trend_per_hour === null || prediction.trend_per_hour === undefined) {
                el.textContent = '';
                return;
            }
            const t = prediction.trend_per_hour;
            const arrow = t > 0 ? '▲' : (t < 0 ? '▼' : '■');
            el.textContent = `${arrow} ${Math.abs(t)}%/h`;
        }
```

Then in `updatePredictions`, after the three `updatePredictionCard(...)` calls, add:

```javascript
                setStatusTrend('weekly-trend', predictions.weekly);
                setStatusTrend('session-trend', predictions.session);
                setStatusTrend('model-trend', predictions.model_specific);
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest test_dashboard_ui.py::test_status_cards_have_value_and_trend_hooks -v`
Expected: PASS.

- [ ] **Step 7: Manual verification**

Run the app. Each status card shows: a colored dot + mono caps name, a trend `▲ x%/h` (if data), a large mono number + `% used`, a thin colored bar, and `NN.N% left` + `reset …`. Card dot/bar color matches usage (green/amber/red).

- [ ] **Step 8: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(ui): data-dense status cards with trend"
```

---

## Task 6: Prediction cards — badge + key/value grid

**Files:**
- Modify: `templates/dashboard.html`

- [ ] **Step 1: Rewrite `updatePredictionCard`**

Replace the entire `function updatePredictionCard(elementId, prediction) { ... }` with:

```javascript
        function predKv(prediction, opts) {
            opts = opts || {};
            const atReset = opts.atReset !== undefined ? opts.atReset : (prediction.predicted_at_reset + '%');
            const to100 = opts.to100 !== undefined ? opts.to100
                : (prediction.hours_to_100 ? formatHours(prediction.hours_to_100) : '∞');
            const warn = opts.warn ? ' class="warn"' : '';
            return `
                <dl class="pred-kv">
                    <dt>Current</dt><dd${warn}>${prediction.current_usage}%</dd>
                    <dt>At reset</dt><dd${warn}>${atReset}</dd>
                    <dt>Trend</dt><dd>${opts.trend !== undefined ? opts.trend : prediction.trend_per_hour + '%/h'}</dd>
                    <dt>To 100%</dt><dd${warn}>${to100}</dd>
                    <dt>To reset</dt><dd>${formatHours(prediction.time_to_reset_hours)}</dd>
                </dl>`;
        }

        function updatePredictionCard(elementId, prediction) {
            const card = document.getElementById(elementId);
            const content = card.querySelector('.prediction-content');

            if (!prediction) {
                content.innerHTML = '<span class="no-data">Insufficient data</span>';
                return;
            }

            if (prediction.stale_data) {
                content.innerHTML =
                    '<div class="pred-badge pending">🔄 Reset — waiting for data</div>' +
                    predKv(prediction, { atReset: prediction.current_usage + '%', trend: '0%/h', to100: '∞' });
                return;
            }

            if (prediction.low_confidence) {
                content.innerHTML =
                    '<div class="pred-badge pending">📊 Collecting…</div>' +
                    predKv(prediction, {
                        atReset: prediction.current_usage + '%',
                        trend: (prediction.trend_per_hour !== null ? prediction.trend_per_hour + '%/h' : '0%/h'),
                        to100: '∞'
                    });
                return;
            }

            const cls = prediction.will_exceed ? 'warning' : 'ok';
            const icon = prediction.will_exceed ? '⚠' : '✓';
            const text = prediction.will_exceed ? 'Will exceed limit' : 'On track';
            content.innerHTML =
                `<div class="pred-badge ${cls}">${icon} ${text}</div>` +
                predKv(prediction, { warn: prediction.will_exceed });
        }
```

(The `.prediction-card` / `.prediction-content` markup in the HTML is unchanged — only the injected content changes.)

- [ ] **Step 2: Manual verification**

Run the app. Each prediction card shows a pill badge (`✓ On track` green / `⚠ Will exceed limit` red / `📊 Collecting…` muted) and a mono key/value grid (Current, At reset, Trend, To 100%, To reset). When "will exceed", Current/At reset/To 100% render red.
Expected: matches the companion `fullpage` mockup prediction row.

- [ ] **Step 3: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(ui): prediction cards as badge + kv grid"
```

---

## Task 7: Charts — panel + Chart.js colors for the dark theme

**Files:**
- Modify: `templates/dashboard.html`
- Test: `test_dashboard_ui.py`

- [ ] **Step 1: Add failing test**

Append to `test_dashboard_ui.py`:

```python
def test_chart_colors_swapped_to_palette():
    html = _read("templates/dashboard.html")
    assert "#22d3ee" in html      # weekly line (cyan)
    assert "#a78bfa" in html      # sonnet line (violet)
    assert "#3498db" not in html  # old blue removed
    assert "#9b59b6" not in html  # old purple removed
    assert "Sonnet Usage (% used)" in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest test_dashboard_ui.py::test_chart_colors_swapped_to_palette -v`
Expected: FAIL.

- [ ] **Step 3: Set Chart.js dark defaults**

Immediately after `Chart.register(resetMarkersPlugin);`, add:

```javascript
        // Dark-theme defaults (hex values mirror the CSS tokens; dark-only app).
        Chart.defaults.color = '#8499ab';
        Chart.defaults.font.family = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
        Chart.defaults.borderColor = '#15202b';
```

- [ ] **Step 4: Color the axes/legend/tooltip in `chartConfig`**

Replace the `scales` block:

```javascript
                scales: {
                    x: {
                        type: 'time',
                        time: {
                            unit: 'hour',
                            displayFormats: {
                                hour: 'HH:mm',
                                day: 'dd.MM HH:mm'
                            },
                            tooltipFormat: 'dd.MM.yyyy HH:mm'
                        },
                        title: {
                            display: true,
                            text: 'Time'
                        }
                    },
                    y: {
                        min: 0,
                        max: 100,
                        title: {
                            display: true,
                            text: '% used'
                        }
                    }
                },
```

with:

```javascript
                scales: {
                    x: {
                        type: 'time',
                        time: {
                            unit: 'hour',
                            displayFormats: {
                                hour: 'HH:mm',
                                day: 'dd.MM HH:mm'
                            },
                            tooltipFormat: 'dd.MM.yyyy HH:mm'
                        },
                        grid: { color: '#15202b' },
                        ticks: { color: '#5b7083' },
                        title: { display: true, text: 'Time', color: '#8499ab' }
                    },
                    y: {
                        min: 0,
                        max: 100,
                        grid: { color: '#15202b' },
                        ticks: { color: '#5b7083' },
                        title: { display: true, text: '% used', color: '#8499ab' }
                    }
                },
```

Then in the same `chartConfig`, replace the tooltip's color lines:

```javascript
                    tooltip: {
                        enabled: true,
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        titleColor: '#fff',
                        bodyColor: '#fff',
                        borderColor: 'rgba(255, 255, 255, 0.2)',
                        borderWidth: 1,
```

with:

```javascript
                    tooltip: {
                        enabled: true,
                        backgroundColor: '#0f161d',
                        titleColor: '#e8f0f5',
                        bodyColor: '#e8f0f5',
                        borderColor: '#15202b',
                        borderWidth: 1,
```

- [ ] **Step 5: Swap dataset colors in `initCharts`**

Replace the weekly datasets:

```javascript
                    datasets: [
                        { label: 'Usage', borderColor: '#3498db', backgroundColor: 'rgba(52, 152, 219, 0.1)', fill: true, data: [] },
                        { label: 'Target', borderColor: '#f39c12', borderDash: [2, 2], fill: false, pointRadius: 0, data: [], segment: targetSegment }
                    ]
```

with:

```javascript
                    datasets: [
                        { label: 'Usage', borderColor: '#22d3ee', backgroundColor: 'rgba(34, 211, 238, 0.12)', fill: true, data: [] },
                        { label: 'Target', borderColor: '#f5a524', borderDash: [2, 2], fill: false, pointRadius: 0, data: [], segment: targetSegment }
                    ]
```

Replace the session datasets:

```javascript
                    datasets: [
                        { label: 'Usage', borderColor: '#2ecc71', backgroundColor: 'rgba(46, 204, 113, 0.1)', fill: true, data: [] },
                        { label: 'Target', borderColor: '#f39c12', borderDash: [2, 2], fill: false, pointRadius: 0, data: [], segment: targetSegment }
                    ]
```

with:

```javascript
                    datasets: [
                        { label: 'Usage', borderColor: '#10b981', backgroundColor: 'rgba(16, 185, 129, 0.12)', fill: true, data: [] },
                        { label: 'Target', borderColor: '#f5a524', borderDash: [2, 2], fill: false, pointRadius: 0, data: [], segment: targetSegment }
                    ]
```

Replace the model datasets:

```javascript
                    datasets: [
                        { label: 'Usage', borderColor: '#9b59b6', backgroundColor: 'rgba(155, 89, 182, 0.1)', fill: true, data: [] },
                        { label: 'Target', borderColor: '#f39c12', borderDash: [2, 2], fill: false, pointRadius: 0, data: [], segment: targetSegment }
                    ]
```

with:

```javascript
                    datasets: [
                        { label: 'Usage', borderColor: '#a78bfa', backgroundColor: 'rgba(167, 139, 250, 0.12)', fill: true, data: [] },
                        { label: 'Target', borderColor: '#f5a524', borderDash: [2, 2], fill: false, pointRadius: 0, data: [], segment: targetSegment }
                    ]
```

- [ ] **Step 6: Rename the model chart title**

Replace:

```html
                <h2>Model Specific Usage (% used)</h2>
```

with:

```html
                <h2>Sonnet Usage (% used)</h2>
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest test_dashboard_ui.py::test_chart_colors_swapped_to_palette -v`
Expected: PASS.

- [ ] **Step 8: Manual verification**

Run the app, cycle every range button (1h…1m). Lines render cyan/green/violet with soft fills; Target is an amber dashed line; reset markers are dotted vertical lines; axis/legend text is muted gray mono; charts are ~280px tall.

- [ ] **Step 9: Commit**

```bash
git add templates/dashboard.html test_dashboard_ui.py
git commit -m "feat(ui): chart palette + dark axes/tooltip"
```

---

## Task 8: Login page reskin

**Files:**
- Modify: `templates/login.html`
- Test: `test_dashboard_ui.py`

- [ ] **Step 1: Add failing test**

Append to `test_dashboard_ui.py`:

```python
def test_login_has_no_theme_toggle():
    html = _read("templates/login.html")
    assert "theme-toggle" not in html
    assert "toggleTheme" not in html
    assert "data-theme" not in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest test_dashboard_ui.py::test_login_has_no_theme_toggle -v`
Expected: FAIL.

- [ ] **Step 3: Replace the `<head>` block**

Replace:

```html
    <meta name="theme-color" content="#1a1a2e" id="meta-theme-color">
    <title>Login - Claude Usage Dashboard</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
    <script>
        // Apply theme immediately to prevent flash
        (function() {
            const theme = localStorage.getItem('theme') || 'dark';
            document.documentElement.setAttribute('data-theme', theme);
            var color = theme === 'dark' ? '#1a1a2e' : '#f5f7fa';
            document.getElementById('meta-theme-color').setAttribute('content', color);
        })();
    </script>
```

with:

```html
    <meta name="theme-color" content="#0a0e12">
    <title>Login - Claude Usage Dashboard</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
```

- [ ] **Step 4: Remove the toggle button**

Delete:

```html
    <button id="theme-toggle" class="theme-toggle" style="position: absolute; top: 20px; right: 20px;">🌙</button>
```

- [ ] **Step 5: Remove the trailing theme `<script>`**

Delete the whole block:

```html
    <script>
        function updateThemeIcon(theme) {
            document.getElementById('theme-toggle').textContent = theme === 'dark' ? '☀️' : '🌙';
        }

        function toggleTheme() {
            const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeIcon(newTheme);
            document.getElementById('meta-theme-color').setAttribute('content', newTheme === 'dark' ? '#1a1a2e' : '#f5f7fa');
        }

        document.addEventListener('DOMContentLoaded', () => {
            const theme = localStorage.getItem('theme') || 'dark';
            updateThemeIcon(theme);
            document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
        });
    </script>
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest test_dashboard_ui.py::test_login_has_no_theme_toggle -v`
Expected: PASS.

- [ ] **Step 7: Manual verification**

Visit `/login` (logged out). The login box uses the dark panel, mono uppercase labels, cyan focus border on inputs, and a cyan login button. No theme toggle present.

- [ ] **Step 8: Commit**

```bash
git add templates/login.html test_dashboard_ui.py
git commit -m "feat(ui): reskin login to Cyan Terminal, drop toggle"
```

---

## Task 9: Full verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run: `pytest -q`
Expected: all tests pass, including the new `test_dashboard_ui.py` (7 tests) and the pre-existing backend tests.

- [ ] **Step 2: Grep for light-theme leftovers**

Run: `grep -rn "data-theme\|theme-toggle\|toggleTheme\|initTheme\|meta-theme-color\|#1a1a2e\|#f5f7fa" templates/ static/`
Expected: no matches.

- [ ] **Step 3: Launch the app**

Run: `FLASK_SECRET_KEY=dev DASHBOARD_PASSWORD=dev python app.py`
(Defaults are fail-closed; these env vars let it serve locally. Log in with `admin` / `dev`.)
Open http://127.0.0.1:5000 (or the port app.py prints).

- [ ] **Step 4: Visual checklist (compare to companion mockups)**

Reference: `.superpowers/brainstorm/67676-1780577490/content/fullpage.html` and `weight.html` (W2).
- Header: title + freshness chip (green when data is fresh) + cyan Refresh + red Logout. No theme toggle.
- Status / 📊 Prediction / Charts section labels present.
- Status cards: dot + name, trend, big mono number, thin colored bar, "% left" + reset.
- Prediction cards: badge + kv grid; "Will exceed" shows red values.
- Charts ~280px, cyan/green/violet lines, amber dashed Target, dotted reset markers.
- Footer mono.

- [ ] **Step 5: State + range checks**

- Click every range button (1h, 6h, 24h, 3d, 7d, 14d, 1m) — charts redraw, active button is cyan.
- Confirm at least one card in each color state if data allows (or trust the `getProgressClass` thresholds).

- [ ] **Step 6: Freshness simulation**

In DevTools console: temporarily confirm staleness mapping by checking that an old `data.timestamp` would flip the chip. (Optional: stop the collector / no new snapshot → after >30 min chip turns red.) At minimum confirm the chip is green with live data.

- [ ] **Step 7: Responsive check**

Resize to ≤768px and ≤380px: status/prediction grids collapse to one column; charts shrink to 220/190px; header stacks; no horizontal scroll.

- [ ] **Step 8: Refresh the committed screenshots (optional but nice)**

If keeping `screenshot-dark.png` current: take a fresh screenshot of the new dashboard and replace it. Delete `screenshot-light.png` (light theme is gone) and drop its reference from `README.md` if present.

```bash
# only if you regenerated the screenshot
git add screenshot-dark.png
git rm screenshot-light.png   # light theme removed
git commit -m "docs: refresh dashboard screenshot for dark-only UI"
```

- [ ] **Step 9: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "chore(ui): verification fixes"
```

---

## Self-Review (completed during planning)

**Spec coverage:** tokens → T1; dark-only/remove toggle → T2 (dashboard) + T8 (login); freshness dot → T3; section labels → T4; status cards + trend + Sonnet label → T5; prediction badge/kv → T6; charts (colors/grid/legend/tooltip/280px/reset color/Sonnet title) → T7; login reskin → T8; verification (grep, responsive, states) → T9. Non-goals (no backend/endpoint/DB/light theme) respected.

**Placeholder scan:** every code step contains full code; no TBD/TODO/"handle edge cases".

**Type/name consistency:** id prefixes (`weekly`/`session`/`model`) consistent across markup (T5), `setStatusCard`/`setStatusTrend` (T5), `updateFreshness` (T3). CSS class names consumed by JS (`progress-bar ok|warning|critical`, `pred-badge ok|warning|pending`, `freshness fresh|stale|dead`) match between T1 (CSS) and T2–T8 (JS). `--reset-marker-color` token (T1) matches the existing `resetMarkersPlugin` read in dashboard.html (unchanged).
