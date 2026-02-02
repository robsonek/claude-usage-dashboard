"""
Module for fetching Claude usage data directly from CLI.
Based on: https://github.com/MartinLoeper/claude-o-meter
"""
import re
import os
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import pexpect


try:
    import config
    CLAUDE_BIN = config.CLAUDE_BIN
except ImportError:
    CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')

# Regex patterns
ANSI_PATTERN = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\))')
PERCENT_PATTERN = re.compile(r'(\d{1,3})\s*%\s*(used|left)', re.IGNORECASE)
DAYS_PATTERN = re.compile(r'(\d+)\s*d(?:ays?)?', re.IGNORECASE)
HOURS_PATTERN = re.compile(r'(\d+)\s*h(?:ours?|r)?', re.IGNORECASE)
MINUTES_PATTERN = re.compile(r'(\d+)\s*m(?:in(?:utes?)?)?', re.IGNORECASE)

# Absolute time patterns
TIME_ONLY_PATTERN = re.compile(r'(\d{1,2})(?::(\d{2}))?(am|pm)\b', re.IGNORECASE)
DATE_NO_YEAR_PATTERN = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(1[0-2]|[1-9])(?::(\d{2}))?(am|pm)\b',
    re.IGNORECASE
)

# Account type patterns
PRO_PATTERN = re.compile(r'(?i)(?:·\s*)?claude\s+pro')
MAX_PATTERN = re.compile(r'(?i)(?:·\s*)?claude\s+max')

# Email pattern - without apostrophes and trailing spaces
EMAIL_PATTERN = re.compile(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})')

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


def strip_ansi(text: str) -> str:
    """Remove ANSI codes from text and join split words."""
    # Remove ANSI codes
    text = ANSI_PATTERN.sub('', text)
    # Replace cursor forward (\x1b[nC) with space - these codes split words
    text = re.sub(r'\x1b\[\d*C', ' ', text)
    # Remove remaining escape sequences
    text = re.sub(r'\x1b[^a-zA-Z]*[a-zA-Z]', '', text)
    # Join "Rese s" -> "Resets" (artifact from cursor movement)
    text = re.sub(r'Rese\s+s\s+', 'Resets ', text)
    return text


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
    """Parse relative time (e.g. '2d 3h 45m') to seconds."""
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
    """Parse reset time from lines."""
    search_text = ' '.join(lines[start_idx:min(start_idx + 5, len(lines))])

    # Try to parse relative time
    duration_seconds = parse_relative_time(search_text)

    reset_time = None
    if duration_seconds:
        reset_time = datetime.now() + timedelta(seconds=duration_seconds)

    # Try to find absolute time with date
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

        now = datetime.now()
        year = now.year
        reset_time = datetime(year, month, int(day), hour, int(minute))
        if reset_time < now:
            reset_time = datetime(year + 1, month, int(day), hour, int(minute))
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

            now = datetime.now()
            reset_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If time has passed, it will be tomorrow
            if reset_time < now:
                reset_time += timedelta(days=1)

            # Calculate duration_seconds
            duration_seconds = int((reset_time - now).total_seconds())

    # Find reset text
    reset_text = ''
    for line in lines[start_idx:min(start_idx + 5, len(lines))]:
        if 'reset' in line.lower():
            reset_text = line.strip()
            break

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
        # Run claude /usage in PTY from script directory (must be trusted folder)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        child = pexpect.spawn(
            CLAUDE_BIN,
            ['/usage'],
            timeout=timeout,
            encoding='utf-8',
            env={**os.environ, 'TERM': 'xterm-256color'},
            cwd=script_dir
        )

        # Wait for usage data to load (may take several seconds)
        import time
        output = ''

        # Collect output for up to 25 seconds, looking for multiple % patterns
        start_time = time.time()
        percent_count = 0

        while time.time() - start_time < 25:
            try:
                idx = child.expect(['% used', '% left', pexpect.TIMEOUT], timeout=3)
                output += child.before or ''
                if idx in [0, 1]:
                    output += child.after or ''
                    percent_count += 1
                    # We expect at least 3 sections: session, weekly, model-specific
                    if percent_count >= 3:
                        # Wait a bit more for any remaining data
                        time.sleep(1)
                        break
            except pexpect.EOF:
                break
            except:
                pass

        # Collect any remaining output
        time.sleep(0.5)
        try:
            while True:
                chunk = child.read_nonblocking(size=10000, timeout=1)
                if chunk:
                    output += chunk
                else:
                    break
        except:
            pass

        child.terminate(force=True)

        # Parse output
        clean_output = strip_ansi(output)

        quotas = parse_quotas(clean_output)

        return {
            'account_type': detect_account_type(clean_output),
            'email': parse_email(clean_output),
            'quotas': quotas,
            'captured_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
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
