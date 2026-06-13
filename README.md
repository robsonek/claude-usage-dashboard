# Claude Usage Dashboard

[![tests](https://github.com/robsonek/claude-usage-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/robsonek/claude-usage-dashboard/actions/workflows/tests.yml)
[![secret-scan](https://github.com/robsonek/claude-usage-dashboard/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/robsonek/claude-usage-dashboard/actions/workflows/secret-scan.yml)

Web dashboard for monitoring Claude subscription usage (5h / 7d / per-model limits)
across multiple accounts, with charts and prediction.

![Dashboard Screenshot](screenshot-dark.png)

## Deployment Guide

### Overview

This document describes the deployment process for Claude Usage Dashboard
on a Linux server. Examples use Debian 13 (Trixie) but any modern Linux
distribution with Python 3.11+ works.

## Prerequisites

- Linux server with root/sudo access (tested on Debian 13 / Trixie)
- SSH access to the server
- Python 3.11+ with `venv`
- Git
- A Claude subscription (Pro / Max / Team / Enterprise). Accounts are added later
  through the dashboard's OAuth flow — **no Claude CLI is needed on the server.**

## Deployment Steps

### 1. Clone Repository

```bash
ssh user@server
cd ~
git clone https://github.com/robsonek/claude-usage-dashboard.git claude-dashboard
cd claude-dashboard
```

### 2. Install System Dependencies

```bash
sudo apt update
sudo apt install -y python3.13-venv nginx git
```

### 3. Create Python Virtual Environment

```bash
cd ~/claude-dashboard
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create Systemd Service

Create `/etc/systemd/system/claude-dashboard.service`:

```ini
[Unit]
Description=Claude Usage Dashboard
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/claude-dashboard
Environment=PATH=/home/YOUR_USERNAME/claude-dashboard/venv/bin:/usr/bin
EnvironmentFile=/home/YOUR_USERNAME/claude-dashboard/.env
ExecStart=/home/YOUR_USERNAME/claude-dashboard/venv/bin/gunicorn --bind 127.0.0.1:5050 --workers 2 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **Required before first start:** the dashboard *fails closed* and will refuse
> to boot until `FLASK_SECRET_KEY` and `DASHBOARD_PASSWORD` are provided — via the
> `EnvironmentFile` above (a `.env` of `KEY=value` lines) or `Environment=` lines.
> See [Configuration](#configuration). If gunicorn keeps restarting right after
> deploy, this is almost always the cause (`journalctl -u claude-dashboard -e`
> shows `RuntimeError: Refusing to start ...`).

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable claude-dashboard
sudo systemctl start claude-dashboard
sudo systemctl status claude-dashboard
```

### 5. Configure Nginx Reverse Proxy

Create `/etc/nginx/sites-available/claude-dashboard`:

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable the configuration:

```bash
sudo ln -sf /etc/nginx/sites-available/claude-dashboard /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 6. Install Cron and Configure Data Collection

Install cron daemon:

```bash
sudo apt install -y cron
sudo systemctl enable cron
sudo systemctl start cron
```

Set up cron job to collect data every 5 minutes:

```bash
crontab -e
```

Add the following lines at the end:

```cron
*/5 * * * * cd /home/YOUR_USERNAME/claude-dashboard && ./collect_history.sh >> /home/YOUR_USERNAME/claude-dashboard/cron.log 2>&1
```

`collect_history.sh` loads `TOKEN_ENCRYPTION_KEY` from `.env` itself (cron does not
inherit systemd's `EnvironmentFile`), so no extra `PATH`/env setup is needed here.

Verify cron is set:

```bash
crontab -l
```

### 7. Verify Installation

```bash
curl -I http://localhost/
```

Expected response: HTTP 302 redirect to login page.

## Configuration

The app reads configuration from environment variables. It does **not** parse
`.env` files itself, so hand them to the gunicorn process either with
`Environment=KEY=value` lines under `[Service]`, or by pointing the unit at an env
file (`EnvironmentFile=/home/YOUR_USERNAME/claude-dashboard/.env`, see step 4)
containing `KEY=value` lines — no `export`, no quotes needed.

Copy [`.env.example`](.env.example) to `.env` as a starting point and fill in real
values (`cp .env.example .env`); the table below documents every variable.

> **Fail-closed:** the dashboard refuses to start unless **both**
> `FLASK_SECRET_KEY` and `DASHBOARD_PASSWORD` are set — otherwise it would run on
> the built-in defaults and anyone could forge a logged-in session. For a throwaway
> local run only, set `ALLOW_DEFAULT_CREDENTIALS=1` to bypass the guard.

| Variable | Description | Default |
|----------|-------------|---------|
| FLASK_SECRET_KEY | Session signing key — **required** | (refuses to start) |
| DASHBOARD_PASSWORD | Login password — **required** | (refuses to start) |
| DASHBOARD_USERNAME | Login username | admin |
| SESSION_COOKIE_SECURE | Set to `1` when served over HTTPS (Secure cookie) | 0 |
| ALLOW_DEFAULT_CREDENTIALS | Set to `1` to allow built-in defaults (local dev only) | unset |
| TOKEN_ENCRYPTION_KEY | Fernet key encrypting account OAuth tokens at rest — **required to add accounts** (v1.3.0+) | (adding accounts fails without it) |
| RETENTION_DAYS | Days of history to keep; older snapshots/quotas, `data/YYYY-MM-DD/` dirs and legacy `data/raw_debug/` files are pruned daily (the dashboard only charts up to 1 month) | 60 |

Generate a secure secret key with:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Generate the token-encryption key with:

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Add Claude accounts from the **Accounts** page in the dashboard (OAuth, paste-the-code
flow). Each account is polled every 5 minutes; the account bar lets you switch between
them. Losing `TOKEN_ENCRYPTION_KEY` means re-adding every account.

After changing any variable, restart the service:
`sudo systemctl restart claude-dashboard`.

### Data retention

`collect_history.sh` runs `cleanup_old_data.py` at most once per day (gated by a
`data/.cleanup_done` date marker). It deletes snapshots+quotas older than
`RETENTION_DAYS`, removes `data/YYYY-MM-DD/` JSON dirs and `data/raw_debug/`
files past the same window, and `VACUUM`s the database only when rows were
removed. Run it manually anytime, and use `--dry-run` to preview:

```bash
venv/bin/python cleanup_old_data.py --dry-run
```

## Upgrading an Existing Deployment

The `quotas.period_start_at` column is added automatically on app startup,
but historical rows are left at NULL. Without backfill, the chart's Target
line and reset markers will only render correctly for snapshots captured
*after* the upgrade. Run the one-shot backfill once to populate older rows
(it also repairs the occasional +24h reset-time glitch some Claude CLI
versions emit at a reset boundary) using the same shift-vs-reset heuristic as
live inserts:

```bash
cd ~/claude-dashboard
venv/bin/python -c '
from database import UsageDatabase
import config
db = UsageDatabase(config.DB_FILE)
r = db.backfill_period_start_at()
print(f"period_start updated: {r[\"period_start_updates\"]}, "
      f"resets_at sanitized: {r[\"resets_at_sanitized\"]}")
'
```

The pass is idempotent — re-running it is a no-op once everything is
consistent. Back up `usage.db` first if the deployment carries data you
cannot afford to lose.

## Management Commands

```bash
# Check service status
sudo systemctl status claude-dashboard

# View logs
sudo journalctl -u claude-dashboard -f

# Restart service
sudo systemctl restart claude-dashboard

# Stop service
sudo systemctl stop claude-dashboard
```

## Adding Claude Accounts

Accounts are added entirely through the dashboard — there is **no Claude CLI on
the server** and nothing to log into over SSH. Tokens are stored encrypted in the
database (Fernet), so `TOKEN_ENCRYPTION_KEY` must be set in `.env` first (see
[Configuration](#configuration)).

1. Open the dashboard, log in, and go to the **Accounts** page.
2. Click **Generate link**, then **Open authorization page** (or use **Copy URL**
   to open it on another device / in a browser already signed into the right
   Claude account) and approve access.
3. Claude shows a `code#state` string — paste it back on the **Accounts** page and
   click **Add account**.

The account is then polled every 5 minutes. Add as many accounts as you like; the
account bar on the dashboard switches between them. Multi-account support requires v1.3.0+.

Each account row on the **Accounts** page offers these actions:

- **Start 5h window** (v1.6.1+) — sends a minimal `"Hi"` message to Claude Haiku to
  deliberately anchor the start of that account's rolling 5-hour usage window (for
  example, at the beginning of your workday so the block lines up with when you actually
  work). It first reads the current usage: if a 5-hour window is already active it does
  nothing and just reports the reset time, so it never wastes quota. The message is sent
  with the account's own subscription token — no separate API credits are billed.
- **Refresh session** — re-runs the authorization flow to repair an account whose token
  was revoked (e.g. logout or CLI reinstall). It matches by e-mail, so history is kept.
- **Enable / Disable** — pause or resume polling for an account without deleting its history.
- **Rename / Delete** — relabel an account, or remove it (its snapshots are retained).

> The OAuth token endpoint is fronted by Cloudflare and is sensitive to the
> `User-Agent`; the app already sends the value that works. If a code exchange
> returns 403/429, wait a few minutes and retry — repeated failed attempts extend
> a per-IP cooldown.

Until at least one active account is added, the dashboard shows no usage data.

## Security Recommendations

1. Set a strong `DASHBOARD_PASSWORD` and `FLASK_SECRET_KEY` before the first start — the app fails closed and refuses to run on the built-in defaults (see [Configuration](#configuration))
2. Configure HTTPS with Let's Encrypt (certbot)
3. Set up firewall rules (ufw or iptables)
4. Restrict SSH access

## Troubleshooting

### Service won't start
- Check logs: `sudo journalctl -u claude-dashboard -e`
- Verify venv path exists
- Check file permissions

### 502 Bad Gateway
- Verify gunicorn is running: `sudo systemctl status claude-dashboard`
- Check if port 5050 is in use: `ss -tlnp | grep 5050`

### Database errors
- Ensure write permissions on working directory
- Check disk space: `df -h`

### Dashboard shows empty quotas
Data comes from accounts added on the **Accounts** page (multi-account OAuth) —
the collector polls every *active* account. Typical causes and how to diagnose:

1. No active account — open the **Accounts** page and check that at least one
   account is added and active (re-authorize it there if its tokens were
   revoked).
2. Collector missing `TOKEN_ENCRYPTION_KEY` — cron does not load systemd's
   `EnvironmentFile`, so `collect_history.sh` reads the key itself from `.env`.
   Verify `.env` contains `TOKEN_ENCRYPTION_KEY=...`, then run the collector by
   hand with the key:

   ```bash
   cd ~/claude-dashboard
   TOKEN_ENCRYPTION_KEY=$(grep -E '^TOKEN_ENCRYPTION_KEY=' .env | cut -d= -f2-) venv/bin/python collect_all.py; echo "exit=$?"
   ```

   Exit codes: `0` OK, `1` no active accounts, `2` at least one account
   failed, `3` crash (e.g. missing/wrong encryption key).
3. Check `cron.log` for what went wrong:

   ```bash
   tail -n 50 ~/claude-dashboard/cron.log
   ```

   `WARN: at least one account failed` means a per-account error (details in
   the lines above it and in the account's `last_error` on the Accounts page);
   `no active accounts to poll` means case 1.

### `cron.log` grows indefinitely
Add a logrotate rule at `/etc/logrotate.d/claude-dashboard`:

```
/home/YOUR_USERNAME/claude-dashboard/cron.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
```

Or truncate manually from time to time: `: > ~/claude-dashboard/cron.log`.

---
Deployment completed successfully.
