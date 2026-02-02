"""Claude Dashboard Configuration"""
import os
from werkzeug.security import generate_password_hash

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_FILE = os.path.join(BASE_DIR, 'usage.db')

# Secret key for Flask session
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'change-this-in-production-12345')

# Login credentials (change to your own!)
# Default: admin / claude123 - CHANGE THIS!
USERNAME = os.environ.get('DASHBOARD_USERNAME', 'admin')
PASSWORD_HASH = generate_password_hash(os.environ.get('DASHBOARD_PASSWORD', 'claude123'))

# Session
SESSION_LIFETIME_HOURS = 24

# Path to Claude CLI
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
