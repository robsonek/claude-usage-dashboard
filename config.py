"""Claude Dashboard Configuration"""
import os
from werkzeug.security import generate_password_hash

# App version — shown in the dashboard UI (header chip + footer). Single source
# of truth. RULE: bump this (at least the patch level) on EVERY git push, so each
# pushed/deployed state carries a unique version. See memory feedback_bump_version_on_push.
VERSION = '1.6.3'

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_FILE = os.path.join(BASE_DIR, 'usage.db')

# Data retention: rows/files older than this many days are pruned by
# cleanup_old_data.py (invoked once/day from collect_history.sh).
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '60'))

# Secret key for Flask session. A placeholder fallback keeps imports working
# (the collector imports this module too), but app.py refuses to actually serve
# with the default — SECRET_KEY_IS_DEFAULT lets it detect that.
_env_secret = os.environ.get('FLASK_SECRET_KEY')
SECRET_KEY = _env_secret or 'change-this-in-production-12345'
SECRET_KEY_IS_DEFAULT = not _env_secret

# Login credentials (change to your own!). Default admin/claude123 is first-run
# only — app.py refuses to serve with it unless ALLOW_DEFAULT_CREDENTIALS=1.
_env_password = os.environ.get('DASHBOARD_PASSWORD')
USERNAME = os.environ.get('DASHBOARD_USERNAME', 'admin')
PASSWORD_IS_DEFAULT = not _env_password
PASSWORD_HASH = generate_password_hash(_env_password or 'claude123')

# Session
SESSION_LIFETIME_HOURS = 24

# Fernet key for encrypting OAuth tokens at rest in the accounts table.
# Generate once: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Store in .env (never in repo/rsync). Losing it = re-add every account.
TOKEN_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY')
