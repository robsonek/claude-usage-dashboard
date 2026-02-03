"""
Module for fetching Claude usage data directly from CLI.
Based on: https://github.com/MartinLoeper/claude-o-meter
"""
import re
import os
import json
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List


try:
    import config
    CLAUDE_BIN = config.CLAUDE_BIN
except ImportError:
    CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')

# Regex patterns
PERCENT_PATTERN = re.compile(r'(\d{1,3})\s*%\s*(used|left)', re.IGNORECASE)
DAYS_PATTERN = re.compile(r'(\d+)\s*d(?:ays?)?', re.IGNORECASE)
HOURS_PATTERN = re.compile(r'(\d+)\s*h(?:ours?|r)?', re.IGNORECASE)
MINUTES_PATTERN = re.compile(r'(\d+)\s*m(?:in(?:utes?)?)?', re.IGNORECASE)

# Absolute time patterns
TIME_ONLY_PATTERN = re.compile(r'(\d{1,2})(?::(\d{1,2}))?(am|pm)\b', re.IGNORECASE)
DATE_NO_YEAR_PATTERN = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:,?\s*(?:at\s+)?)?(1[0-2]|[1-9])(?::(\d{1,2}))?(am|pm)\b',
    re.IGNORECASE
)

# Account type patterns
PRO_PATTERN = re.compile(r'(?i)(?:·\s*)?claude\s+pro')
MAX_PATTERN = re.compile(r'(?i)(?:·\s*)?claude\s+max')

# Email pattern - without apostrophes and trailing spaces
EMAIL_PATTERN = re.compile(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})')

# Auth error patterns
AUTH_ERROR_PATTERNS = {
    'setup_required': re.compile(r"let.?s\s+get\s+started", re.IGNORECASE),
    'token_expired': re.compile(r"(token|session)\s*(has\s+)?expired", re.IGNORECASE),
    'not_logged_in': re.compile(r"not\s+logged\s+in|please\s+(log|sign)\s*in", re.IGNORECASE),
    'no_subscription': re.compile(r"free\s+tier|no\s+(active\s+)?subscription", re.IGNORECASE),
}

# Quota section boundaries (to stop searching for reset time)
QUOTA_BOUNDARIES = ['current session', 'current week', 'opus', 'sonnet']

# Quota labels - lowercase for matching
QUOTA_LABELS = {
    'current session': ('session', ''),
    'current week (all models)': ('weekly', ''),
    'current week (opus)': ('model_specific', 'opus'),
    'current week (sonnet)': ('model_specific', 'sonnet'),
    'current week (opus only)': ('model_specific', 'opus'),
    'current week (sonnet only)': ('model_specific', 'sonnet'),
    # Additional variants
    'session': ('session', ''),
    'week (all models)': ('weekly', ''),
    'week (sonnet only)': ('model_specific', 'sonnet'),
    'week (opus only)': ('model_specific', 'opus'),
}


def emulate_terminal(data: str, width: int = 120) -> str:
    """Emulate terminal to properly handle cursor movements."""
    lines = {}
    row, col = 0, 0
    i = 0

    while i < len(data):
        c = data[i]

        # ESC sequence
        if c == '\x1b' and i + 1 < len(data):
            if data[i+1] == '[':
                # Find end of CSI sequence
                j = i + 2
                while j < len(data) and data[j] not in 'ABCDHJKfmnsu':
                    j += 1
                if j < len(data):
                    seq = data[i+2:j]
                    cmd = data[j]

                    # Parse number
                    num = 1
                    if seq.isdigit():
                        num = int(seq)

                    if cmd == 'C':  # Cursor forward
                        col += num
                    elif cmd == 'D':  # Cursor back
                        col = max(0, col - num)
                    elif cmd == 'A':  # Cursor up
                        row = max(0, row - num)
                    elif cmd == 'B':  # Cursor down
                        row += num
                    elif cmd == 'H' or cmd == 'f':  # Cursor position
                        parts = seq.split(';')
                        row = int(parts[0]) - 1 if parts[0] else 0
                        col = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else 0

                    i = j + 1
                    continue
            elif data[i+1] == ']':
                # OSC sequence - find terminator
                j = i + 2
                while j < len(data) and data[j] != '\x07' and not (data[j] == '\x1b' and j+1 < len(data) and data[j+1] == '\\'):
                    j += 1
                i = j + 1
                continue
            else:
                i += 2
                continue

        # Regular characters
        if c == '\r':
            col = 0
        elif c == '\n':
            row += 1
            col = 0
        elif c >= ' ' or c == '\t':
            if row not in lines:
                lines[row] = [' '] * width
            if col < width:
                lines[row][col] = c
            col += 1

        i += 1

    result = []
    for r in sorted(lines.keys()):
        result.append(''.join(lines[r]).rstrip())
    return '\n'.join(result)


def parse_percentage(line: str) -> Optional[float]:
    """Parse percentage from line."""
    match = PERCENT_PATTERN.search(line)
    if match:
        value = int(match.group(1))
        direction = match.group(2).lower()
        if direction == 'used':
            return 100 - value  # Convert "used" to "remaining"
        return value
    return None


def parse_relative_time(text: str) -> Optional[int]:
    """Parse relative time (e.g. '2d 3h 45m') to seconds.
    Only parses if 'reset' keyword is nearby to avoid false positives."""
    # Check if 'reset' is in text (to avoid parsing random numbers like '2m' from corrupted text)
    if 'reset' not in text.lower():
        return None

    total_seconds = 0

    days_match = DAYS_PATTERN.search(text)
    if days_match:
        total_seconds += int(days_match.group(1)) * 86400

    hours_match = HOURS_PATTERN.search(text)
    if hours_match:
        total_seconds += int(hours_match.group(1)) * 3600

    minutes_match = MINUTES_PATTERN.search(text)
    if minutes_match:
        total_seconds += int(minutes_match.group(1)) * 60

    return total_seconds if total_seconds > 0 else None


def parse_reset_time(lines: List[str], start_idx: int) -> tuple:
    """Parse reset time from lines. Searches up to 14 lines, stops at quota boundaries."""
    # Find end index - either 14 lines or until we hit a quota boundary
    end_idx = min(start_idx + 14, len(lines))
    for i in range(start_idx + 1, end_idx):
        line_lower = lines[i].lower()
        for boundary in QUOTA_BOUNDARIES:
            if boundary in line_lower:
                end_idx = i
                break
        if end_idx == i:
            break

    search_text = ' '.join(lines[start_idx:end_idx])

    # Extract timezone from text (e.g. "Europe/Warsaw", "UTC")
    tz = timezone.utc
    tz_match = re.search(r'\(([A-Za-z_/]+)\)', search_text)
    if tz_match:
        tz_name = tz_match.group(1)
        if tz_name.upper() != 'UTC':
            try:
                tz = ZoneInfo(tz_name)
            except:
                pass

    # Try to parse relative time
    duration_seconds = parse_relative_time(search_text)

    reset_time = None
    now_utc = datetime.now(timezone.utc)

    if duration_seconds:
        reset_time = now_utc + timedelta(seconds=duration_seconds)

    # Try to find absolute time with date (preferred over relative time)
    date_match = DATE_NO_YEAR_PATTERN.search(search_text)
    if date_match:
        month_str, day, hour, minute, ampm = date_match.groups()
        minute = minute or '0'
        months = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                  'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
        month = months.get(month_str.lower(), 1)
        hour = int(hour)
        if ampm.lower() == 'pm' and hour != 12:
            hour += 12
        elif ampm.lower() == 'am' and hour == 12:
            hour = 0

        year = now_utc.year
        # Create datetime in the detected timezone
        reset_time = datetime(year, month, int(day), hour, int(minute), tzinfo=tz)
        # Convert to UTC
        reset_time = reset_time.astimezone(timezone.utc)
        if reset_time < now_utc:
            reset_time = datetime(year + 1, month, int(day), hour, int(minute), tzinfo=tz)
            reset_time = reset_time.astimezone(timezone.utc)
        # Recalculate duration from absolute time
        duration_seconds = int((reset_time - now_utc).total_seconds())
    else:
        # Try to find time only (e.g. "4pm", "3:59pm")
        time_match = TIME_ONLY_PATTERN.search(search_text)
        if time_match:
            hour_str, minute_str, ampm = time_match.groups()
            hour = int(hour_str)
            minute = int(minute_str) if minute_str else 0
            if ampm.lower() == 'pm' and hour != 12:
                hour += 12
            elif ampm.lower() == 'am' and hour == 12:
                hour = 0

            # Get current time in the detected timezone
            now_tz = now_utc.astimezone(tz)
            reset_time = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If time has passed, it will be tomorrow
            if reset_time < now_tz:
                reset_time += timedelta(days=1)
            # Convert to UTC
            reset_time = reset_time.astimezone(timezone.utc)

            # Calculate duration_seconds
            duration_seconds = int((reset_time - now_utc).total_seconds())

    # Find reset text
    reset_text = ''
    for line in lines[start_idx:end_idx]:
        if 'reset' in line.lower():
            reset_text = line.strip()
            break

    # Always calculate duration_seconds if we have reset_time
    if reset_time and not duration_seconds:
        duration_seconds = int((reset_time - now_utc).total_seconds())

    return reset_text, reset_time, duration_seconds


def format_duration(seconds: int) -> str:
    """Format seconds as human-readable text."""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")

    return ' '.join(parts) if parts else '0m'


def parse_quotas(text: str) -> List[Dict[str, Any]]:
    """Parse limits from claude /usage output."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    quotas = []

    for i, line in enumerate(lines):
        line_lower = line.lower()

        for label, (quota_type, model) in QUOTA_LABELS.items():
            if label in line_lower:
                # Look for percentage in this and next lines
                for j in range(i, min(i + 5, len(lines))):
                    percent = parse_percentage(lines[j])
                    if percent is not None:
                        reset_text, reset_time, duration_seconds = parse_reset_time(lines, j)

                        quota = {
                            'type': quota_type,
                            'percent_remaining': percent,
                        }

                        if model:
                            quota['model'] = model

                        if reset_time:
                            quota['resets_at'] = reset_time.strftime('%Y-%m-%dT%H:%M:%SZ')

                        if reset_text:
                            quota['reset_text'] = reset_text

                        if duration_seconds:
                            quota['time_remaining_seconds'] = duration_seconds
                            quota['time_remaining_human'] = format_duration(duration_seconds)

                        quotas.append(quota)
                        break
                break

    return quotas


def detect_account_type(text: str) -> str:
    """Detect account type."""
    if MAX_PATTERN.search(text):
        return 'max'
    if PRO_PATTERN.search(text):
        return 'pro'
    return 'unknown'


def detect_auth_error(text: str) -> Optional[str]:
    """Detect authentication errors in output."""
    text_lower = text.lower()
    for error_type, pattern in AUTH_ERROR_PATTERNS.items():
        if pattern.search(text_lower):
            return error_type
    return None


def parse_email(text: str) -> Optional[str]:
    """Parse email from output."""
    match = EMAIL_PATTERN.search(text)
    return match.group(1) if match else None


def fetch_usage(timeout: int = 30) -> Dict[str, Any]:
    """
    Fetch usage data from claude CLI.

    Returns:
        Dict with usage data
    """
    try:
        import pty
        import select
        import time

        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Create PTY for proper terminal emulation
        master, slave = pty.openpty()

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            os.chdir(script_dir)
            os.execlp(CLAUDE_BIN, CLAUDE_BIN, '/usage')

        os.close(slave)

        output = b''
        start_time = time.time()
        last_data_time = None
        got_usage_data = False

        while time.time() - start_time < timeout:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    data = os.read(master, 4096)
                    if data:
                        output += data
                        last_data_time = time.time()
                        if b'% used' in data or b'% left' in data:
                            got_usage_data = True
                except:
                    break
            elif last_data_time:
                idle = time.time() - last_data_time
                if got_usage_data and idle > 1.0:
                    # Usage data detected - check if we have all quotas
                    clean = emulate_terminal(output.decode('utf-8', errors='replace'))
                    found = clean.count('% used') + clean.count('% left')
                    if found >= 3:
                        break
                # No usage data yet - keep waiting until overall timeout

        # Send Escape to exit cleanly, then kill
        try:
            os.write(master, b'\x1b')
        except:
            pass
        try:
            os.kill(pid, 9)
        except:
            pass
        try:
            os.close(master)
        except:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except:
            pass

        # Parse output with terminal emulation
        text = output.decode('utf-8', errors='replace')
        clean_output = emulate_terminal(text)

        # Check for auth errors first
        auth_error = detect_auth_error(clean_output)
        if auth_error:
            return {
                'error': 'Authentication error',
                'auth_error_type': auth_error,
                'details': f'Claude CLI returned: {auth_error}'
            }

        quotas = parse_quotas(clean_output)

        return {
            'account_type': detect_account_type(clean_output),
            'email': parse_email(clean_output),
            'quotas': quotas,
            'captured_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        }

    except Exception as e:
        return {
            'error': 'Failed to get usage data',
            'details': str(e)
        }


if __name__ == '__main__':
    # Test
    result = fetch_usage()
    print(json.dumps(result, indent=2))
