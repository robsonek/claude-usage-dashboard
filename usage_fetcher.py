"""
Module for fetching Claude usage data directly from CLI.
Based on: https://github.com/MartinLoeper/claude-o-meter
"""
import re
import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

log = logging.getLogger(__name__)
if os.environ.get('DEBUG_USAGE_FETCHER'):
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format='[%(asctime)s] %(message)s')


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

# Raw-buffer marker: Claude prints "N%<ESC>[1Cused" (cursor forward splits % and used).
# Used both to detect completeness before terminal emulation and to pick the right frame.
RAW_PCT_USED_PATTERN = re.compile(r'\d+%\x1b\[1Cused')

# DEC private-mode 2026: synchronized output (begin/end). Claude wraps each render
# in these. We stop reading at the end of the first frame that already contains all
# quota percentages, because subsequent frames are diff updates that overwrite the
# quota labels with blanks.
SYNC_UPDATE_END = re.compile(r'\x1b\[\?2026l')

# Minimum "% used" occurrences we consider a complete render.
MIN_COMPLETE_QUOTAS = 3

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
    """Emulate terminal to properly handle cursor movements.

    Handles CSI final bytes across the full ANSI range (0x40-0x7E), not just the
    handful we actually interpret — previously unlisted finals like 'h'/'l' caused
    the parser to swallow content until it hit one of the recognized bytes.
    """
    lines = {}
    row, col = 0, 0
    i = 0

    while i < len(data):
        c = data[i]

        # ESC sequence
        if c == '\x1b' and i + 1 < len(data):
            if data[i+1] == '[':
                # CSI: consume parameter/intermediate bytes (0x20-0x3F) until final (0x40-0x7E)
                j = i + 2
                while j < len(data) and not (0x40 <= ord(data[j]) <= 0x7E):
                    j += 1
                if j < len(data):
                    seq = data[i+2:j]
                    cmd = data[j]

                    # DEC private-mode (e.g. [?2026h for synchronized output): no cursor effect
                    if seq.startswith('?'):
                        i = j + 1
                        continue

                    # Parse leading number (for cursor movement commands)
                    num = 1
                    first_param = seq.split(';', 1)[0]
                    if first_param.isdigit():
                        num = int(first_param)

                    if cmd == 'C':  # Cursor forward
                        col += num
                    elif cmd == 'D':  # Cursor back
                        col = max(0, col - num)
                    elif cmd == 'A':  # Cursor up
                        row = max(0, row - num)
                    elif cmd == 'B':  # Cursor down
                        row += num
                    elif cmd in ('H', 'f'):  # Cursor position
                        parts = seq.split(';')
                        row = int(parts[0]) - 1 if parts[0] else 0
                        col = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else 0

                    i = j + 1
                    continue
            elif data[i+1] == ']':
                # OSC sequence - find terminator (BEL or ST)
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


def trim_to_complete_frame(raw: str) -> str:
    """Cut raw PTY output to the end of the first synchronized-output frame that
    already contains all quota percentages. Later frames are diff updates that
    overwrite the quota labels with blanks — if we emulate them, labels vanish."""
    for m in SYNC_UPDATE_END.finditer(raw):
        if len(RAW_PCT_USED_PATTERN.findall(raw, 0, m.end())) >= MIN_COMPLETE_QUOTAS:
            return raw[:m.end()]
    return raw


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
    """Parse reset time from lines after the % line, bounded to the current section.

    A section ends at the first blank line or known quota-boundary header. Without
    the blank-line stop, a quota with no own Resets line (e.g. `Current week
    (Sonnet only)` shows just `0% used` and no reset row) would leak the Resets
    text from the next section (e.g. `Extra usage` with `$X / $Y spent · Resets May 1`).
    """
    end_idx = min(start_idx + 14, len(lines))
    for i in range(start_idx + 1, end_idx):
        if not lines[i].strip():
            end_idx = i
            break
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


def _reap(pid: int, master_fd: int) -> None:
    """Shut down the claude child process cleanly: ESC → SIGTERM → SIGKILL,
    always reaping with a blocking waitpid at the end so we don't leave zombies."""
    import time

    # Polite exit request
    try:
        os.write(master_fd, b'\x1b')
    except OSError:
        pass
    time.sleep(0.1)

    def wait_nb(deadline: float) -> bool:
        while time.time() < deadline:
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    return True
            except ChildProcessError:
                return True
            time.sleep(0.05)
        return False

    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        pass
    if not wait_nb(time.time() + 1.0):
        try:
            os.kill(pid, 9)  # SIGKILL
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    try:
        os.close(master_fd)
    except OSError:
        pass


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
            os._exit(127)  # execlp failed

        os.close(slave)

        output = b''
        start_time = time.time()
        last_data_time: Optional[float] = None
        complete_detected = False

        while time.time() - start_time < timeout:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                output += data
                last_data_time = time.time()

                # Detect completeness on the raw buffer — Claude prints
                # "N%<ESC>[1Cused", so a literal "% used" substring never matches.
                # End the loop as soon as we see a sync-update termination after
                # the 3rd "% used"; waiting longer only lets diff frames overwrite labels.
                if not complete_detected:
                    text = output.decode('utf-8', errors='replace')
                    if len(RAW_PCT_USED_PATTERN.findall(text)) >= MIN_COMPLETE_QUOTAS:
                        complete_detected = True

                if complete_detected:
                    text = output.decode('utf-8', errors='replace')
                    if SYNC_UPDATE_END.search(text, _cut_search_start(text)) or last_data_time and time.time() - last_data_time > 0.5:
                        log.debug('complete data detected, %d bytes, %.2fs elapsed',
                                  len(output), time.time() - start_time)
                        break
            elif last_data_time and time.time() - last_data_time > 2.0:
                # Idle timeout — claude went quiet without emitting the markers we
                # expect (auth error, setup wizard, unknown layout). Give up and parse what we have.
                log.debug('idle timeout, %d bytes', len(output))
                break

        if time.time() - start_time >= timeout:
            log.debug('overall timeout reached, %d bytes', len(output))

        _reap(pid, master)

        # Trim to first complete frame to avoid diff-update frames that blank labels
        raw_text = output.decode('utf-8', errors='replace')
        trimmed = trim_to_complete_frame(raw_text)
        if log.isEnabledFor(logging.DEBUG):
            log.debug('trimmed %d → %d bytes', len(raw_text), len(trimmed))
        clean_output = emulate_terminal(trimmed)

        # Check for auth errors first
        auth_error = detect_auth_error(clean_output)
        if auth_error:
            return {
                'error': 'Authentication error',
                'auth_error_type': auth_error,
                'details': f'Claude CLI returned: {auth_error}'
            }

        quotas = parse_quotas(clean_output)

        if not quotas:
            log.debug('no quotas parsed; clean output was:\n%s', clean_output)

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


def _cut_search_start(text: str) -> int:
    """Offset from which to search for sync-update end — after the 3rd '% used' marker."""
    matches = RAW_PCT_USED_PATTERN.findall(text)
    if len(matches) < MIN_COMPLETE_QUOTAS:
        return len(text)
    # Find position of the 3rd match
    count = 0
    for m in RAW_PCT_USED_PATTERN.finditer(text):
        count += 1
        if count == MIN_COMPLETE_QUOTAS:
            return m.end()
    return len(text)


if __name__ == '__main__':
    # Test
    result = fetch_usage()
    print(json.dumps(result, indent=2))
