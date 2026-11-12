#!/usr/bin/env python3
"""pingclaude: send a short message to Claude Code on a fixed schedule.

pingclaude only shells out to the official `claude` CLI, which handles its own
login. It never reads, stores or sends credentials and makes no network calls
of its own. Everything it keeps (settings, state, log) lives in ~/.pingclaude/.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__version__ = "0.1.0"

DEFAULTS = {
    "interval": "5h30m",
    "message": ".",
    "start_at": None,
    "retries": 3,
    "command": "claude -p --no-session-persistence",
}
CONFIG_KEYS = tuple(DEFAULTS)

# Tells claude not to save a session transcript for each ping, so ~/.claude
# doesn't fill up with hundreds of "." sessions.
NO_HISTORY_FLAG = "--no-session-persistence"

RETRY_DELAY = 60  # seconds between attempts
MAX_CHUNK = 30  # never sleep longer than this, so Ctrl+C and wake-ups are noticed
SEND_TIMEOUT = 300  # give one `claude -p` call at most five minutes
HEARTBEAT_STALE = MAX_CHUNK * 2 + 30  # status older than this means "not running"

COMMANDS = ("run", "send", "install", "uninstall", "status", "stop", "config", "gui")

MISSING_CLAUDE_HINT = """\
pingclaude: the `claude` command was not found on your PATH.

pingclaude needs the official Claude Code CLI, logged in with your own account:
  1. Install Claude Code (see Anthropic's Claude Code install guide), e.g.
       npm install -g @anthropic-ai/claude-code
  2. Run `claude` once in a terminal and finish the login.
  3. Run pingclaude again.

If claude is installed somewhere unusual, point to it with
  --command "/full/path/to/claude -p"
"""

# Characters that cmd.exe would interpret when the claude entry point is a
# .cmd/.bat shim (the npm install on Windows). Messages containing them are
# refused instead of being mangled or run as extra commands.
_CMD_SPECIAL = set('&|<>^%!"\r\n')


class ConfigError(Exception):
    """A setting is missing or invalid."""


class StartupError(Exception):
    """The scheduler cannot start; carries the exit code to use."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def home_dir() -> Path:
    """Folder for config, state, lock and log. PINGCLAUDE_HOME overrides it."""
    override = os.environ.get("PINGCLAUDE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".pingclaude"


def ensure_home() -> Path:
    path = home_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home_dir() / "config.json"


def state_path() -> Path:
    return home_dir() / "state.json"


def log_path() -> Path:
    return home_dir() / "log.txt"


def lock_path() -> Path:
    return home_dir() / "pingclaude.lock"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(text) -> int:
    """Turn '5h30m', '90m', '10s', '1d' or plain seconds ('3600') into seconds."""
    if isinstance(text, bool):
        raise ValueError("duration must be text like 5h30m")
    if isinstance(text, int):
        seconds = text
    else:
        s = str(text).strip().lower().replace(" ", "")
        if not s:
            raise ValueError("duration is empty")
        if s.isdigit():
            seconds = int(s)
        else:
            match = _DURATION_RE.match(s)
            if not match or not any(match.groups()):
                raise ValueError(
                    "invalid duration %r (use e.g. 5h30m, 90m, 10s or seconds)" % text
                )
            d, h, m, sec = (int(g) if g else 0 for g in match.groups())
            seconds = d * 86400 + h * 3600 + m * 60 + sec
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def format_duration(seconds: float) -> str:
    """Compact form of a duration: 19800 -> '5h30m', 3 -> '3s'."""
    total = max(0, int(round(seconds)))
    if total == 0:
        return "0s"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    for value, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (secs, "s")):
        if value:
            parts.append("%d%s" % (value, unit))
    return "".join(parts)


_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(text: str) -> Tuple[int, int]:
    match = _HHMM_RE.match(str(text).strip())
    if not match:
        raise ValueError("invalid time %r (use HH:MM, e.g. 06:00)" % text)
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("invalid time %r (hours 0-23, minutes 0-59)" % text)
    return hour, minute


def next_start(hhmm: str, now: datetime) -> datetime:
    """Next local time matching HH:MM that is strictly after `now`."""
    hour, minute = parse_hhmm(hhmm)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def split_command(command: str) -> List[str]:
    if os.name == "nt":
        parts = shlex.split(command, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return shlex.split(command)


def resolve_command(command: str) -> Optional[List[str]]:
    """Command as an argument list with the program resolved on PATH, or None."""
    try:
        parts = split_command(command)
    except ValueError:
        return None
    if not parts:
        return None
    exe = shutil.which(parts[0])
    if not exe:
        return None
    return [exe] + parts[1:]


def check_message_safe(argv: List[str], message: str) -> None:
    exe = argv[0].lower()
    if os.name == "nt" and exe.endswith((".cmd", ".bat")) and set(message) & _CMD_SPECIAL:
        raise ConfigError(
            "your claude command is a .cmd shim, so the message cannot contain any of "
            "& | < > ^ % ! \" or line breaks. Use a simpler message."
        )


def printable(argv: List[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


# --------------------------------------------------------------------------
# Settings and config file
# --------------------------------------------------------------------------

class Settings:
    """Validated settings. `interval` is in seconds."""

    __slots__ = ("interval", "interval_text", "message", "start_at", "retries", "command")

    def __init__(self, interval, interval_text, message, start_at, retries, command):
        self.interval = interval
        self.interval_text = interval_text
        self.message = message
        self.start_at = start_at
        self.retries = retries
        self.command = command

    @classmethod
    def from_dict(cls, raw: Dict) -> "Settings":
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in raw.items() if k in CONFIG_KEYS})
        try:
            interval = parse_duration(merged["interval"])
        except ValueError as exc:
            raise ConfigError("interval: %s" % exc)

        start_at = merged["start_at"]
        if start_at in ("", "now", "none", "None"):
            start_at = None
        if start_at is not None:
            try:
                hour, minute = parse_hhmm(start_at)
            except ValueError as exc:
                raise ConfigError("start_at: %s" % exc)
            start_at = "%02d:%02d" % (hour, minute)

        message = merged["message"]
        if not isinstance(message, str) or not message.strip():
            raise ConfigError("message must not be empty")

        try:
            retries = int(merged["retries"])
        except (TypeError, ValueError):
            raise ConfigError("retries must be a whole number")
        if not 0 <= retries <= 20:
            raise ConfigError("retries must be between 0 and 20")

        command = merged["command"]
        if not isinstance(command, str) or not command.strip():
            raise ConfigError("command must not be empty")

        return cls(interval, format_duration(interval), message, start_at, retries, command.strip())

    def to_config(self) -> Dict:
        return {
            "interval": self.interval_text,
            "message": self.message,
            "start_at": self.start_at,
            "retries": self.retries,
            "command": self.command,
        }

    def __eq__(self, other):
        return isinstance(other, Settings) and self.to_config() == other.to_config()

    def __repr__(self):
        return "Settings(%r)" % self.to_config()


def load_config(path: Optional[Path] = None) -> Dict:
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s must contain a JSON object" % path)
    return {k: v for k, v in data.items() if k in CONFIG_KEYS}


def save_config(values: Dict, path: Optional[Path] = None) -> Path:
    path = path or config_path()
    settings = Settings.from_dict(values)  # validate before writing
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(settings.to_config(), indent=2) + "\n")
    return path


def resolve_settings(overrides: Dict) -> Settings:
    raw = dict(DEFAULTS)
    raw.update(load_config())
    raw.update(overrides)
    return Settings.from_dict(raw)


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def make_config_reloader(overrides: Dict, log: Callable[[str], None]):
    """Return a function that gives new Settings when config.json changes."""
    path = config_path()
    seen = [_mtime(path)]

    def check() -> Optional[Settings]:
        current = _mtime(path)
        if current == seen[0]:
            return None
        seen[0] = current
        try:
            return resolve_settings(overrides)
        except ConfigError as exc:
            log("Ignoring invalid change to %s: %s" % (path, exc))
            return None

    return check


# --------------------------------------------------------------------------
# State file (read by `status`, the settings window and the tray)
# --------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError:  # Windows: a reader has the file open
            if attempt == 4:
                raise
            time.sleep(0.05)


def read_state() -> Dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(data: Dict) -> None:
    try:
        ensure_home()
        _atomic_write(state_path(), json.dumps(data, indent=2) + "\n")
    except OSError:
        pass  # status display is best effort; never stop the scheduler for it


def read_status(now: Optional[float] = None) -> Dict:
    """State plus a `running` flag based on how fresh the heartbeat is."""
    state = read_state()
    now = time.time() if now is None else now
    beat = state.get("heartbeat")
    state["running"] = bool(
        state.get("pid") and isinstance(beat, (int, float)) and now - beat < HEARTBEAT_STALE
    )
    return state


# --------------------------------------------------------------------------
# Single-instance lock
# --------------------------------------------------------------------------

class InstanceLock:
    """An OS-level exclusive lock; released automatically if the process dies."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or lock_path()
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(console: bool = True) -> Callable[[str], None]:
    """Log to ~/.pingclaude/log.txt and, when there is one, the terminal."""
    ensure_home()
    logger = logging.getLogger("pingclaude")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_path()), maxBytes=1000000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    if console and sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        logger.addHandler(stream)
    return logger.info


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send_once(argv: List[str], message: str, run=subprocess.run) -> Tuple[bool, str]:
    """Run the command once. Returns (ok, short detail for the log)."""
    kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        timeout=SEND_TIMEOUT,
        cwd=str(ensure_home()),  # keep claude away from whatever project folder we started in
    )
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        result = run(list(argv) + [message], **kwargs)
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % SEND_TIMEOUT
    except OSError as exc:
        return False, "could not start command: %s" % exc
    out = (result.stdout or "").strip()
    if result.returncode != 0 and NO_HISTORY_FLAG in argv:
        said = ((result.stderr or "") + out).lower()
        if "unknown option" in said and NO_HISTORY_FLAG in said:
            # An older claude without this flag: send anyway, just without it.
            ok, detail = send_once([a for a in argv if a != NO_HISTORY_FLAG], message, run=run)
            return ok, "(claude is too old for %s; sent without it) %s" % (NO_HISTORY_FLAG, detail)
    if result.returncode == 0:
        first = out.splitlines()[0] if out else ""
        return True, (first[:80] + "...") if len(first) > 80 else first
    err = (result.stderr or "").strip() or out
    tail = err.splitlines()[-1] if err else ""
    return False, "exit code %s%s" % (result.returncode, (": " + tail[:200]) if tail else "")


def send_with_retries(
    argv: List[str],
    message: str,
    retries: int,
    log: Callable[[str], None],
    run=subprocess.run,
    pause: Callable[[float], bool] = None,
    delay: float = RETRY_DELAY,
) -> bool:
    """Try up to 1 + retries times. `pause(seconds)` returns True if we should stop."""
    if pause is None:
        def pause(seconds):
            time.sleep(seconds)
            return False
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        ok, detail = send_once(argv, message, run=run)
        if ok:
            log("Sent OK%s" % (" (reply: %s)" % detail if detail else ""))
            return True
        log("Attempt %d/%d failed: %s" % (attempt, attempts, detail))
        if attempt < attempts:
            log("Retrying in %ds..." % delay)
            if pause(delay):
                return False
    log("Giving up on this slot; will try again at the next one.")
    return False


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------

def late_grace(interval: float) -> float:
    """How late a slot may fire and still count as on time."""
    return min(120.0, max(1.0, interval / 2.0))


def plan_after_slot(due: float, now: float, interval: float) -> Tuple[float, bool, int]:
    """Decide the next due time once `now >= due`.

    Returns (next_due, late, missed). On time: keep the fixed grid (due + interval),
    so there is no drift. Late (machine slept): send once and restart the grid
    from now, so missed slots never turn into a burst.
    """
    lateness = now - due
    if lateness > late_grace(interval):
        return now + interval, True, int(lateness // interval)
    return due + interval, False, 0


def first_due(now: float, interval: float, start_at: Optional[str], last_send: Optional[float]) -> float:
    if start_at:
        return next_start(start_at, datetime.fromtimestamp(now)).timestamp()
    if last_send and 0 <= now - last_send < interval:
        return last_send + interval  # e.g. after a restart: don't send again too soon
    return now


class Scheduler:
    """Fixed-slot scheduler. Safe to run in a background thread (the tray does)."""

    def __init__(
        self,
        settings: Settings,
        argv: List[str],
        log: Callable[[str], None],
        dry_run: bool = False,
        count: Optional[int] = None,
        clock: Callable[[], float] = time.time,
        run=subprocess.run,
        wait: Optional[Callable[[float], None]] = None,
        reload: Optional[Callable[[], Optional[Settings]]] = None,
        keep_state: bool = True,
    ):
        self.settings = settings
        self.argv = argv
        self.log = log
        self.dry_run = dry_run
        self.count = count
        self.clock = clock
        self.run_cmd = run
        self.reload = reload
        self.keep_state = keep_state and not dry_run
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._send_now = False
        self.paused = False
        self.due: Optional[float] = None
        self.sends = 0
        self.last_send: Optional[float] = None
        self.last_ok: Optional[bool] = None
        self._wait = wait or self._sliced_wait

    # --- controls (thread safe) ---
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def send_now(self) -> None:
        self._send_now = True
        self._wake.set()

    def pause(self) -> None:
        self.paused = True
        self.log("Paused.")
        self._wake.set()

    def resume(self) -> None:
        self.paused = False
        self.log("Resumed.")
        self._wake.set()

    # --- internals ---
    def _sliced_wait(self, seconds: float) -> None:
        # Wait in 1s slices: on Windows a long Event.wait would delay Ctrl+C.
        end = time.monotonic() + seconds
        while not self._wake.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            self._wake.wait(min(1.0, left))
        self._wake.clear()

    def _pause_for_retry(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while not self._stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return False
            self._stop.wait(min(1.0, left))
        return True

    def _announce_next(self) -> None:
        if self.due is None:
            return
        wait = self.due - self.clock()
        if wait < 1:
            self.log("First send: now")
        else:
            self.log("Next send at %s (in %s)" % (_fmt_time(self.due), format_duration(wait)))

    def _save_state(self, running: bool = True) -> None:
        if not self.keep_state:
            return
        write_state({
            "pid": os.getpid() if running else None,
            "heartbeat": self.clock() if running else None,
            "next_send": self.due if running else None,
            "last_send": self.last_send,
            "last_ok": self.last_ok,
            "paused": self.paused,
            "interval": self.settings.interval_text,
            "start_at": self.settings.start_at,
            "message": self.settings.message,
        })

    def _fire(self) -> None:
        self.sends += 1
        label = "Send %d%s" % (self.sends, "/%d" % self.count if self.count else "")
        if self.dry_run:
            self.log("%s [dry run] would run: %s" % (label, printable(self.argv + [self.settings.message])))
            return
        self.log("%s: running %s" % (label, printable(self.argv + [self.settings.message])))
        ok = send_with_retries(
            self.argv, self.settings.message, self.settings.retries, self.log,
            run=self.run_cmd, pause=self._pause_for_retry,
        )
        self.last_send = self.clock()
        self.last_ok = ok

    def _apply(self, new: Settings, now: float) -> None:
        old = self.settings
        if new == old:
            return
        argv = resolve_command(new.command)
        if argv is None and not self.dry_run:
            self.log("Ignoring new command %r: not found on PATH." % new.command)
            return
        if argv is not None:
            self.argv = argv
        self.settings = new
        changed = ", ".join(
            "%s=%r" % (k, v) for k, v in new.to_config().items() if old.to_config().get(k) != v
        )
        self.log("Settings changed: %s" % changed)
        if new.start_at != old.start_at:
            if new.start_at:
                self.due = next_start(new.start_at, datetime.fromtimestamp(now)).timestamp()
            else:
                self.due = first_due(now, new.interval, None, self.last_send)
        elif new.interval != old.interval and self.last_send:
            self.due = max(now, self.last_send + new.interval)
        self._announce_next()

    def run(self) -> int:
        now = self.clock()
        if self.keep_state:
            previous = read_state()
            last = previous.get("last_send")
            if isinstance(last, (int, float)):
                self.last_send = float(last)
                self.last_ok = previous.get("last_ok")
        self.due = first_due(now, self.settings.interval, self.settings.start_at, self.last_send)
        self.log(
            "pingclaude %s started%s: every %s, message %r%s"
            % (
                __version__,
                " (dry run)" if self.dry_run else "",
                self.settings.interval_text,
                self.settings.message,
                ", %d send%s" % (self.count, "" if self.count == 1 else "s") if self.count else "",
            )
        )
        if self.due > now and self.last_send and not self.settings.start_at:
            self.log("Last send was at %s; keeping that schedule." % _fmt_time(self.last_send))
        self._announce_next()

        try:
            while not self._stop.is_set():
                now = self.clock()
                if self.reload:
                    new = self.reload()
                    if new is not None:
                        self._apply(new, now)

                if self._send_now:
                    self._send_now = False
                    self.log("Send now requested.")
                    self._fire()
                    self.due = self.clock() + self.settings.interval
                    self._announce_next()
                elif not self.paused and now >= self.due:
                    next_due, late, missed = plan_after_slot(self.due, now, self.settings.interval)
                    if late:
                        self.log(
                            "Woke up late: the %s slot was missed%s. Sending once now; "
                            "the schedule restarts from now."
                            % (_fmt_time(self.due), " (plus %d more)" % missed if missed else "")
                        )
                    self._fire()
                    after = self.clock()
                    self.due = next_due if next_due > after else after + self.settings.interval
                    if not (self.count and self.sends >= self.count):
                        self._announce_next()

                if self.count and self.sends >= self.count:
                    self.log("Finished: %d send%s done." % (self.sends, "" if self.sends == 1 else "s"))
                    break
                if self._stop.is_set():
                    break

                self._save_state()
                if self.paused:
                    chunk = MAX_CHUNK
                else:
                    chunk = min(MAX_CHUNK, max(0.0, self.due - self.clock()))
                before = self.clock()
                self._wait(chunk)
                slept = self.clock() - before
                if slept > chunk + 60:
                    self.log(
                        "Clock jumped ahead by %s (the computer was probably asleep)."
                        % format_duration(slept - chunk)
                    )
        finally:
            self._save_state(running=False)
        return 0


# --------------------------------------------------------------------------
# Shared start-up used by `run` and the tray app
# --------------------------------------------------------------------------

def open_scheduler(
    overrides: Dict,
    log: Callable[[str], None],
    dry_run: bool = False,
    count: Optional[int] = None,
) -> Tuple[Scheduler, InstanceLock]:
    """Validate everything, take the single-instance lock and build a Scheduler."""
    settings = resolve_settings(overrides)
    argv = resolve_command(settings.command)
    if argv is None:
        if not dry_run:
            raise StartupError(MISSING_CLAUDE_HINT.rstrip(), 1)
        log("Note: %r is not on PATH; fine for a dry run." % split_command(settings.command)[0])
        argv = split_command(settings.command)
    else:
        check_message_safe(argv, settings.message)
    if settings.interval < 60 and not dry_run:
        log("Warning: an interval under a minute sends a lot of messages.")

    lock = InstanceLock()
    if not lock.acquire():
        status = read_status()
        pid = " (pid %s)" % status["pid"] if status.get("running") else ""
        raise StartupError(
            "pingclaude is already running%s. Only one copy can run at a time.\n"
            "Check it with `pingclaude status` or stop it with `pingclaude stop`." % pid,
            1,
        )
    scheduler = Scheduler(
        settings, argv, log, dry_run=dry_run, count=count,
        reload=make_config_reloader(overrides, log),
    )
    return scheduler, lock


def stop_running(timeout: float = 10.0) -> Optional[int]:
    """Stop the running copy, if any. Returns its pid or None."""
    status = read_status()
    pid = status.get("pid")
    if not status.get("running") or not isinstance(pid, int) or pid == os.getpid():
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
    end = time.time() + timeout
    while time.time() < end:
        probe = InstanceLock()
        if probe.acquire():
            probe.release()
            break
        time.sleep(0.2)
    state = read_state()
    if state.get("pid") == pid:  # killed before it could clean up (e.g. Windows)
        state.update(pid=None, heartbeat=None, next_send=None)
        write_state(state)
    return pid


def open_path(path: Path) -> None:
    """Open a file with the system's default app."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

SUBCOMMAND_HELP = """\
commands:
  pingclaude [run] [options]   run the scheduler in this terminal (default)
  pingclaude send              send one message right now
  pingclaude install           start automatically at log-in (asks first)
  pingclaude uninstall         remove auto-start and stop the background copy
  pingclaude status            show whether it runs and when the next send is
  pingclaude stop              stop the running copy
  pingclaude config [options]  show or change saved settings
  pingclaude gui               open the settings window

Run `pingclaude <command> --help` for a command's options.
"""


def _add_setting_flags(parser: argparse.ArgumentParser, start_at: bool = True) -> None:
    parser.add_argument("--interval", help="time between sends: 5h30m, 90m, 10s or seconds (default 5h30m)")
    parser.add_argument("--message", help="text to send (default '.')")
    if start_at:
        parser.add_argument(
            "--start-at", metavar="HH:MM",
            help="first send at the next occurrence of this local time ('now' = send right away)",
        )
    parser.add_argument("--retries", type=int, help="extra attempts after a failure, 60s apart (default 3)")
    parser.add_argument("--command", dest="claude_command", metavar="CMD", help="command to run (default 'claude -p %s')" % NO_HISTORY_FLAG)


def make_parser(command: str) -> argparse.ArgumentParser:
    prog = "pingclaude" if command == "run" else "pingclaude " + command
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Send a short message to Claude Code on a fixed schedule, using your own claude login.",
        epilog=SUBCOMMAND_HELP if command == "run" else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if command in ("run", "install", "config"):
        _add_setting_flags(parser)
    elif command == "send":
        _add_setting_flags(parser, start_at=False)
    if command == "run":
        parser.add_argument("--count", type=int, metavar="N", help="stop after N sends (default: forever)")
        parser.add_argument("--dry-run", action="store_true", help="log what would happen, send nothing")
        parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--version", action="version", version="pingclaude " + __version__)
    if command in ("install", "uninstall"):
        parser.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    if command == "install":
        parser.add_argument("--tray", action="store_true", help="auto-start the tray app instead of the hidden runner")
    return parser


def overrides_from_args(args: argparse.Namespace) -> Dict:
    pairs = (
        ("interval", "interval"), ("message", "message"), ("start_at", "start_at"),
        ("retries", "retries"), ("command", "claude_command"),
    )
    out = {}
    for key, attr in pairs:
        value = getattr(args, attr, None)
        if value is not None:
            out[key] = None if key == "start_at" and value.lower() in ("now", "none", "") else value
    return out


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_run(args) -> int:
    if args.count is not None and args.count < 1:
        raise ConfigError("--count must be at least 1")
    log = setup_logging(console=not args.background)
    try:
        scheduler, lock = open_scheduler(
            overrides_from_args(args), log, dry_run=args.dry_run, count=args.count
        )
    except StartupError as exc:
        if args.background and "already running" in str(exc):
            log("Auto-start: another copy is already running; this one exits.")
            return 0
        log(str(exc).splitlines()[0])
        print(str(exc), file=sys.stderr)
        return exc.code

    def on_signal(signum, frame):
        log("Stopping (signal %s)..." % signum)
        scheduler.stop()

    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[sig] = signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    try:
        code = scheduler.run()
        if scheduler.stopped:
            log("Stopped.")
        return code
    finally:
        lock.release()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def cmd_send(args) -> int:
    log = setup_logging()
    settings = resolve_settings(overrides_from_args(args))
    argv = resolve_command(settings.command)
    if argv is None:
        print(MISSING_CLAUDE_HINT, file=sys.stderr)
        return 1
    check_message_safe(argv, settings.message)
    log("Manual send: running %s" % printable(argv + [settings.message]))
    ok = send_with_retries(argv, settings.message, settings.retries, log)
    return 0 if ok else 1


def cmd_status(args) -> int:
    import pingclaude_autostart as autostart

    status = read_status()
    try:
        settings = resolve_settings({})
        print("Settings:   every %s, message %r, first send %s, retries %d, command %r" % (
            settings.interval_text, settings.message,
            "at " + settings.start_at if settings.start_at else "right away",
            settings.retries, settings.command,
        ))
    except ConfigError as exc:
        print("Settings:   invalid (%s)" % exc)
    if status["running"]:
        extra = " (paused)" if status.get("paused") else ""
        print("Running:    yes, pid %s%s" % (status["pid"], extra))
        if status.get("next_send"):
            left = status["next_send"] - time.time()
            print("Next send:  %s (in %s)" % (_fmt_time(status["next_send"]), format_duration(left)))
    else:
        print("Running:    no")
    if status.get("last_send"):
        print("Last send:  %s (%s)" % (_fmt_time(status["last_send"]), "ok" if status.get("last_ok") else "failed"))
    try:
        print("Auto-start: %s" % ("on" if autostart.is_installed() else "off"))
    except autostart.AutostartError as exc:
        print("Auto-start: unknown (%s)" % exc)
    print("Files:      %s" % home_dir())
    print("claude:     %s" % (shutil.which("claude") or "NOT FOUND on PATH"))
    return 0


def cmd_stop(args) -> int:
    pid = stop_running()
    if pid is None:
        print("pingclaude is not running.")
    else:
        print("Stopped pingclaude (pid %s)." % pid)
    return 0


def cmd_config(args) -> int:
    changes = overrides_from_args(args)
    if changes:
        values = dict(DEFAULTS)
        values.update(load_config())
        values.update(changes)
        path = save_config(values)
        print("Saved %s" % path)
        if read_status()["running"]:
            print("The running copy will pick up the change within %ds." % MAX_CHUNK)
    settings = resolve_settings({})
    print(json.dumps(settings.to_config(), indent=2))
    return 0


def cmd_install(args) -> int:
    import pingclaude_autostart as autostart

    values = dict(DEFAULTS)
    values.update(load_config())
    values.update(overrides_from_args(args))
    settings = Settings.from_dict(values)
    argv = resolve_command(settings.command)
    if argv is None:
        print(MISSING_CLAUDE_HINT, file=sys.stderr)
        return 1
    check_message_safe(argv, settings.message)

    launch = autostart.launch_argv(tray=args.tray)
    print("Settings that will be saved to %s:" % config_path())
    print("  every %s, message %r, first send %s" % (
        settings.interval_text, settings.message,
        "at next " + settings.start_at if settings.start_at else "right away",
    ))
    print("\nThis will:")
    for line in autostart.describe_install(launch):
        print(("    " if line.startswith(" ") else "  - ") + line)
    if read_status()["running"]:
        print("  - restart the pingclaude that is running now")
    if not args.yes and not _confirm("\nProceed?"):
        print("Nothing changed.")
        return 1
    save_config(values)
    stop_running()
    try:
        for line in autostart.install(launch):
            print(line)
    except autostart.AutostartError as exc:
        print("pingclaude: %s" % exc, file=sys.stderr)
        return 1
    print("Done. Check it any time with `pingclaude status`.")
    return 0


def cmd_uninstall(args) -> int:
    import pingclaude_autostart as autostart

    print("This will:")
    for line in autostart.describe_uninstall():
        print("  - " + line)
    print("  - stop the running pingclaude, if any")
    print("Your settings and log in %s are kept." % home_dir())
    if not args.yes and not _confirm("\nProceed?"):
        print("Nothing changed.")
        return 1
    try:
        for line in autostart.uninstall():
            print(line)
    except autostart.AutostartError as exc:
        print("pingclaude: %s" % exc, file=sys.stderr)
        return 1
    pid = stop_running()
    if pid:
        print("Stopped pingclaude (pid %s)." % pid)
    print("Done.")
    return 0


def cmd_gui(args) -> int:
    import pingclaude_gui

    return pingclaude_gui.main()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = "run"
    if argv and argv[0] in COMMANDS:
        command = argv.pop(0)
    args = make_parser(command).parse_args(argv)
    handlers = {
        "run": cmd_run, "send": cmd_send, "install": cmd_install, "uninstall": cmd_uninstall,
        "status": cmd_status, "stop": cmd_stop, "config": cmd_config, "gui": cmd_gui,
    }
    try:
        return handlers[command](args)
    except ConfigError as exc:
        print("pingclaude: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
