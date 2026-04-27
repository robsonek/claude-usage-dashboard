# Claude Usage Dashboard

Web dashboard for monitoring Claude Code CLI usage with charts and prediction.

![Dashboard Screenshot](screenshot-dark.png#gh-dark-mode-only)
![Dashboard Screenshot](screenshot-light.png#gh-light-mode-only)

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
- Claude Code CLI (`curl -fsSL https://claude.ai/install.sh | bash`) - after installation run `claude` to authenticate

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
ExecStart=/home/YOUR_USERNAME/claude-dashboard/venv/bin/gunicorn --bind 127.0.0.1:5050 --workers 2 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

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
PATH=/home/YOUR_USERNAME/.local/bin:/usr/local/bin:/usr/bin:/bin
*/5 * * * * cd /home/YOUR_USERNAME/claude-dashboard && ./collect_history.sh >> /home/YOUR_USERNAME/claude-dashboard/cron.log 2>&1
```

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

Set environment variables in the systemd service file (the app does not
read `.env` files) by adding `Environment=KEY=value` lines under
`[Service]`:

| Variable | Description | Default |
|----------|-------------|---------|
| FLASK_SECRET_KEY | Session encryption key — **must be changed** | (insecure placeholder) |
| DASHBOARD_USERNAME | Login username | admin |
| DASHBOARD_PASSWORD | Login password — **must be changed** | claude123 |
| CLAUDE_BIN | Path to Claude CLI | claude |

Generate a secure secret key with:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

## Upgrading an Existing Deployment

The `quotas.period_start_at` column is added automatically on app startup,
but historical rows are left at NULL. Without backfill, the chart's Target
line and reset markers will only render correctly for snapshots captured
*after* the upgrade. Run the one-shot backfill once to populate older
rows using the same shift-vs-reset heuristic as live inserts:

```bash
cd ~/claude-dashboard
venv/bin/python -c '
from database import UsageDatabase
import config
db = UsageDatabase(config.DB_FILE)
print(f"updated {db.backfill_period_start_at()} rows")
'
```

The script is idempotent — re-running it is a no-op once everything is
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

## Claude CLI Authentication

IMPORTANT: The data collection requires Claude CLI to be logged in on the server.

After deployment, SSH into the server and authenticate:

```bash
ssh user@server
claude
```

Follow the OAuth flow to authenticate with your Anthropic account.
The CLI stores credentials in `~/.claude/` directory.

Without authentication, the dashboard will show empty quota data.

## Security Recommendations

1. Change default password immediately after deployment
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
Typical causes and how to diagnose:

1. Claude CLI not authenticated on the server — run `claude` manually and
   complete the OAuth flow.
2. `claude` binary not in the cron job's PATH — verify the `PATH=...`
   line in `crontab -l`.
3. Claude CLI UI changed — run the fetcher by hand with debug output:

   ```bash
   cd ~/claude-dashboard
   DEBUG_USAGE_FETCHER=1 venv/bin/python usage_fetcher.py
   ```

   The collector (`collect_history.sh`) now also logs `WARN: empty quotas`
   or `WARN: fetcher returned error: ...` straight into `cron.log` when
   something goes wrong — check it first:

   ```bash
   tail -n 50 ~/claude-dashboard/cron.log
   ```

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
