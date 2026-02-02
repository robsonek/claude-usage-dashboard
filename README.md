# Claude Usage Dashboard

Web dashboard for monitoring Claude Code CLI usage with charts and prediction.

![Dashboard Screenshot](screenshot-dark.png#gh-dark-mode-only)
![Dashboard Screenshot](screenshot-light.png#gh-light-mode-only)

## Deployment Guide

### Overview

This document describes the deployment process for Claude Usage Dashboard
on a fresh Debian 13 (Trixie) server.

## Prerequisites

- Debian 13 (Trixie) server with root/sudo access
- SSH access to the server
- Python 3.11+ (Debian 13 ships with Python 3.13)
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
echo "PATH=/home/YOUR_USERNAME/.local/bin:/usr/local/bin:/usr/bin:/bin
*/5 * * * * cd /home/YOUR_USERNAME/claude-dashboard && ./collect_history.sh >> /home/YOUR_USERNAME/claude-dashboard/cron.log 2>&1" | crontab -
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

Set environment variables in `/home/YOUR_USERNAME/claude-dashboard/.env` or
in the systemd service file:

| Variable | Description | Default |
|----------|-------------|---------|
| FLASK_SECRET_KEY | Session encryption key | (auto-generated) |
| DASHBOARD_USERNAME | Login username | admin |
| DASHBOARD_PASSWORD | Login password | claude123 |
| CLAUDE_BIN | Path to Claude CLI | claude |

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

---
Deployment completed successfully.
