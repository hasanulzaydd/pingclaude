import json
import os
import subprocess
from datetime import datetime

import pytest

import pingclaude as pc


def ok_result(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, stdout="Hi!\n", stderr="")


def fail_result(*args, **kwargs):
    return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom\n")


def make_scheduler(clock, interval="1h", count=None, dry_run=False, run=None, **extra):
    settings = pc.Settings.from_dict({"interval": interval, **extra})
    logs = []
    sched = pc.Scheduler(
        settings, ["claude", "-p"], logs.append, dry_run=dry_run, count=count,
        clock=clock.time, wait=clock.wait, run=run or ok_result,
    )
    return sched, logs


def recording_run(clock, results=None, cost=0.0):
    """A fake subprocess.run that records the clock at each call."""
    calls = []
    results = list(results or [])

    def run(argv, **kwargs):
        calls.append(clock.now)
        clock.now += cost
        outcome = results.pop(0) if results else ok_result
        return outcome(argv)

    run.calls = calls
    return run


# --- duration parsing -------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("5h30m", 19800), ("90m", 5400), ("10s", 10), ("3600", 3600), ("1h", 3600),
    ("1d", 86400), ("2h5m7s", 7507), (" 5H30M ", 19800), ("1h 30m", 5400), (45, 45),
])
def test_parse_duration_valid(text, seconds):
    assert pc.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "   ", "abc", "5x", "h", "0", "0s", "-5", "5m30h", "1.5h", "m5", None, True])
def test_parse_duration_invalid(text):
    with pytest.raises(ValueError):
        pc.parse_duration(text)


@pytest.mark.parametrize("seconds,text", [(19800, "5h30m"), (3, "3s"), (0, "0s"), (90061, "1d1h1m1s"), (3600, "1h")])
def test_format_duration(seconds, text):
    assert pc.format_duration(seconds) == text


@pytest.mark.parametrize("text", ["6", "24:00", "06:60", "6:5", "ab:cd", ""])
def test_parse_hhmm_invalid(text):
    with pytest.raises(ValueError):
        pc.parse_hhmm(text)


# --- next_start -------------------------------------------------------------

def test_next_start_later_today():
    now = datetime(2026, 3, 10, 5, 0, 0)
    assert pc.next_start("06:00", now) == datetime(2026, 3, 10, 6, 0)


def test_next_start_already_passed_goes_to_tomorrow():
    now = datetime(2026, 3, 10, 9, 15, 0)
    assert pc.next_start("06:00", now) == datetime(2026, 3, 11, 6, 0)


def test_next_start_exactly_now_goes_to_tomorrow():
    now = datetime(2026, 3, 10, 6, 0, 0)
    assert pc.next_start("06:00", now) == datetime(2026, 3, 11, 6, 0)


def test_next_start_across_month_end():
    now = datetime(2026, 1, 31, 23, 30)
    assert pc.next_start("0:15", now) == datetime(2026, 2, 1, 0, 15)


# --- slot planning ----------------------------------------------------------

def test_plan_on_time_keeps_grid():
    assert pc.plan_after_slot(1000.0, 1000.5, 3600) == (4600.0, False, 0)


def test_plan_late_restarts_from_now_without_burst():
    # slept 5h through an hourly schedule
    next_due, late, missed = pc.plan_after_slot(1000.0, 1000.0 + 5 * 3600, 3600)
    assert late is True
    assert missed == 5
    assert next_due == 1000.0 + 6 * 3600


def test_first_due_respects_recent_last_send():
    assert pc.first_due(5000.0, 3600, None, 4000.0) == 7600.0
    assert pc.first_due(5000.0, 3600, None, 100.0) == 5000.0
    assert pc.first_due(5000.0, 3600, None, None) == 5000.0


# --- scheduler --------------------------------------------------------------

def test_scheduler_sends_on_fixed_slots(clock):
    start = clock.now
    run = recording_run(clock)
    sched, _ = make_scheduler(clock, interval="1h", count=3, run=run)
    assert sched.run() == 0
    assert run.calls == [start, start + 3600, start + 7200]


def test_scheduler_does_not_drift_when_sends_are_slow(clock):
    start = clock.now
    run = recording_run(clock, cost=7.0)  # each send takes 7s
    sched, _ = make_scheduler(clock, interval="1h", count=4, run=run)
    sched.run()
    assert run.calls == [start + i * 3600 for i in range(4)]


def test_scheduler_never_sleeps_more_than_30s(clock):
    sched, _ = make_scheduler(clock, interval="1h", count=2, run=recording_run(clock))
    sched.run()
    assert clock.waits and max(clock.waits) <= pc.MAX_CHUNK


def test_scheduler_late_wake_sends_once_then_reschedules(clock):
    start = clock.now
    jumped = []

    def jump(c, seconds):
        if not jumped and c.now > start + 600:
            jumped.append(True)
            c.now += 5 * 3600  # laptop slept for 5 hours

    clock.on_wait = jump
    run = recording_run(clock)
    sched, logs = make_scheduler(clock, interval="1h", count=3, run=run)
    sched.run()
    first, wake, after = run.calls
    assert first == start
    assert wake > start + 5 * 3600  # one send right after waking...
    assert after == pytest.approx(wake + 3600)  # ...then one interval later, no burst
    assert any("Woke up late" in line for line in logs)
    assert any("Clock jumped" in line for line in logs)


def test_scheduler_start_at(clock, monkeypatch):
    clock.now = datetime(2026, 3, 10, 5, 0).timestamp()
    run = recording_run(clock)
    sched, _ = make_scheduler(clock, interval="5h30m", count=2, run=run, start_at="06:00")
    sched.run()
    assert [datetime.fromtimestamp(t) for t in run.calls] == [
        datetime(2026, 3, 10, 6, 0), datetime(2026, 3, 10, 11, 30),
    ]


def test_scheduler_send_now_and_pause(clock):
    start = clock.now
    run = recording_run(clock)
    sched, _ = make_scheduler(clock, interval="1h", count=2, run=run)
    steps = []

    def control(c, seconds):
        steps.append(c.now)
        if len(steps) == 1:
            sched.pause()
        elif len(steps) == 200:  # ~100 min later, still paused, nothing sent
            assert len(run.calls) == 1
            sched.resume()
            sched.send_now()

    clock.on_wait = control
    sched.run()
    assert run.calls[0] == start
    assert len(run.calls) == 2
    assert run.calls[1] > start + 3600


def test_scheduler_writes_state_and_clears_on_exit(clock):
    sched, _ = make_scheduler(clock, interval="1h", count=1, run=recording_run(clock))
    sched.run()
    state = json.loads(pc.state_path().read_text())
    assert state["pid"] is None
    assert state["last_ok"] is True
    assert state["last_send"] == clock.now


def test_scheduler_resumes_schedule_after_restart(clock):
    pc.write_state({"last_send": clock.now - 600, "last_ok": True})
    run = recording_run(clock)
    sched, _ = make_scheduler(clock, interval="1h", count=1, run=run)
    start = clock.now
    sched.run()
    assert run.calls == [start + 3000]  # waits out the rest of the hour


def test_scheduler_applies_config_change(clock):
    run = recording_run(clock)
    sched, logs = make_scheduler(clock, interval="1h", count=2, run=run)
    start = clock.now
    pending = [pc.Settings.from_dict({"interval": "10m"})]
    sched.reload = lambda: pending.pop() if pending and run.calls else None
    sched.run()
    assert run.calls == [start, start + 600]
    assert any("Settings changed" in line for line in logs)


# --- dry run never touches subprocess -----------------------------------------

def test_dry_run_never_calls_subprocess(clock, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("subprocess.run must not be called in dry run")

    monkeypatch.setattr(subprocess, "run", explode)
    sched, logs = make_scheduler(clock, interval="10s", count=3, dry_run=True, run=explode)
    assert sched.run() == 0
    assert sum("[dry run]" in line for line in logs) == 3
    assert not pc.state_path().exists()


def test_cli_dry_run_never_calls_subprocess(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("subprocess.run must not be called in dry run")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(pc.subprocess, "run", explode)
    assert pc.main(["--dry-run", "--interval", "1s", "--count", "2"]) == 0


# --- retries ----------------------------------------------------------------

def test_retry_fails_then_succeeds():
    results = [fail_result, fail_result, ok_result]
    calls, pauses, logs = [], [], []

    def run(argv, **kwargs):
        calls.append(argv)
        return results.pop(0)(argv)

    def pause(seconds):
        pauses.append(seconds)
        return False

    assert pc.send_with_retries(["claude", "-p"], ".", 3, logs.append, run=run, pause=pause) is True
    assert len(calls) == 3
    assert calls[0] == ["claude", "-p", "."]
    assert pauses == [60, 60]


def test_retry_gives_up_after_all_attempts():
    calls, pauses = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        return fail_result(argv)

    ok = pc.send_with_retries(["c"], ".", 2, lambda m: None, run=run, pause=lambda s: pauses.append(s))
    assert ok is False
    assert len(calls) == 3
    assert len(pauses) == 2


def test_retry_stops_when_asked():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return fail_result(argv)

    ok = pc.send_with_retries(["c"], ".", 5, lambda m: None, run=run, pause=lambda s: True)
    assert ok is False
    assert len(calls) == 1


def test_send_once_handles_timeout_and_missing_program():
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    def missing(argv, **kwargs):
        raise FileNotFoundError("nope")

    assert pc.send_once(["c"], ".", run=timeout)[0] is False
    assert pc.send_once(["c"], ".", run=missing)[0] is False


def test_send_once_uses_argument_list_without_shell():
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return ok_result(argv)

    pc.send_once(["claude", "-p"], "hello world", run=run)
    assert seen["argv"] == ["claude", "-p", "hello world"]
    assert not seen["kwargs"].get("shell")
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL


# --- settings, config, lock, CLI --------------------------------------------

def test_settings_validation():
    with pytest.raises(pc.ConfigError):
        pc.Settings.from_dict({"interval": "soon"})
    with pytest.raises(pc.ConfigError):
        pc.Settings.from_dict({"start_at": "25:00"})
    with pytest.raises(pc.ConfigError):
        pc.Settings.from_dict({"message": "  "})
    with pytest.raises(pc.ConfigError):
        pc.Settings.from_dict({"retries": -1})
    s = pc.Settings.from_dict({"start_at": "6:00", "interval": "330m"})
    assert s.start_at == "06:00" and s.interval_text == "5h30m"
    assert pc.Settings.from_dict({"start_at": "now"}).start_at is None


def test_config_round_trip_and_cli_overrides():
    pc.save_config({"interval": "4h", "message": "hi"})
    assert pc.load_config()["interval"] == "4h"
    s = pc.resolve_settings({"message": "override"})
    assert s.interval == 4 * 3600 and s.message == "override"


def test_bad_config_file_is_reported():
    pc.ensure_home()
    pc.config_path().write_text("{not json")
    with pytest.raises(pc.ConfigError):
        pc.load_config()
    assert pc.main(["--dry-run", "--count", "1"]) == 2


def test_lock_prevents_second_instance():
    first, second = pc.InstanceLock(), pc.InstanceLock()
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_cli_second_instance_is_refused(capsys):
    lock = pc.InstanceLock()
    assert lock.acquire()
    try:
        assert pc.main(["--dry-run", "--count", "1"]) == 1
    finally:
        lock.release()
    assert "already running" in capsys.readouterr().err


def test_cli_missing_claude_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(pc.shutil, "which", lambda name: None)
    assert pc.main(["--count", "1"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_config_command(capsys):
    assert pc.main(["config", "--interval", "2h", "--start-at", "07:30"]) == 0
    saved = pc.load_config()
    assert saved["interval"] == "2h" and saved["start_at"] == "07:30"
    assert pc.main(["config", "--start-at", "now"]) == 0
    assert pc.load_config()["start_at"] is None


def test_read_status_running_flag():
    pc.write_state({"pid": 123, "heartbeat": 1000.0})
    assert pc.read_status(now=1010.0)["running"] is True
    assert pc.read_status(now=1000.0 + pc.HEARTBEAT_STALE + 1)["running"] is False


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe quoting only matters on Windows")
def test_cmd_shim_rejects_special_characters():
    with pytest.raises(pc.ConfigError):
        pc.check_message_safe([r"C:\npm\claude.cmd", "-p"], "a & b")
    pc.check_message_safe([r"C:\npm\claude.cmd", "-p"], "good morning.")
    pc.check_message_safe([r"C:\bin\claude.exe", "-p"], "a & b")


# --- no-history flag ---------------------------------------------------------

def test_default_command_disables_session_history():
    assert pc.NO_HISTORY_FLAG in pc.Settings.from_dict({}).command.split()


def test_old_claude_without_flag_still_sends():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if pc.NO_HISTORY_FLAG in argv:
            return subprocess.CompletedProcess(argv, 1, "", "error: unknown option '--no-session-persistence'\n")
        return ok_result(argv)

    ok, detail = pc.send_once(["claude", "-p", pc.NO_HISTORY_FLAG], ".", run=run)
    assert ok is True
    assert calls == [["claude", "-p", pc.NO_HISTORY_FLAG, "."], ["claude", "-p", "."]]
    assert "too old" in detail


def test_other_failures_are_not_retried_without_flag():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return fail_result(argv)

    assert pc.send_once(["claude", "-p", pc.NO_HISTORY_FLAG], ".", run=run)[0] is False
    assert len(calls) == 1


def test_config_change_works_without_claude_on_path(clock, monkeypatch):
    # CI machines have no claude; a gap change must still apply.
    monkeypatch.setattr(pc.shutil, "which", lambda name: None)
    run = recording_run(clock)
    sched, logs = make_scheduler(clock, interval="1h", count=2, run=run)
    start = clock.now
    pending = [pc.Settings.from_dict({"interval": "10m"})]
    sched.reload = lambda: pending.pop() if pending and run.calls else None
    sched.run()
    assert run.calls == [start, start + 600]


def test_bad_new_command_keeps_old_command_but_applies_rest(clock, monkeypatch):
    monkeypatch.setattr(pc.shutil, "which", lambda name: None)
    run = recording_run(clock)
    sched, logs = make_scheduler(clock, interval="1h", count=2, run=run)
    start = clock.now
    pending = [pc.Settings.from_dict({"interval": "10m", "command": "nope -p"})]
    sched.reload = lambda: pending.pop() if pending and run.calls else None
    sched.run()
    assert run.calls == [start, start + 600]
    assert sched.settings.command == pc.DEFAULTS["command"]
    assert any("Ignoring new command" in line for line in logs)
