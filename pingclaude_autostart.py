"""Start pingclaude automatically when you log in.

- Windows: a Task Scheduler task that runs at log-on (falls back to the
  per-user "Run" registry key if Task Scheduler refuses).
- macOS: a LaunchAgent in ~/Library/LaunchAgents.
- Linux: a systemd user service.

Every function that changes something has a matching describe_* function that
returns the same steps as text, so callers can show them and ask first.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import List
from xml.sax.saxutils import escape

import pingclaude as pc

TASK_NAME = "PingClaude"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LABEL = "io.github.pingclaude"
UNIT_NAME = "pingclaude.service"

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AutostartError(Exception):
    pass


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise AutostartError("auto-start is not supported on %s" % sys.platform)


def background_python() -> str:
    """On Windows prefer pythonw.exe so no console window appears."""
    exe = Path(sys.executable)
    if os.name == "nt":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(exe)


def launch_argv(tray: bool = False) -> List[str]:
    here = Path(__file__).resolve().parent
    if tray:
        return [background_python(), str(here / "pingclaude_tray.py")]
    return [background_python(), str(here / "pingclaude.py"), "run", "--background"]


def _run(argv: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, errors="replace", creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _output(result: subprocess.CompletedProcess) -> str:
    return ((result.stderr or "") + (result.stdout or "")).strip()


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def windows_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME", "")
    return "%s\\%s" % (domain, user) if domain else user


def windows_task_xml(argv: List[str], user: str) -> str:
    """Task definition. Set via XML because the schtasks flags can't turn off the
    3-day time limit or the "don't run on battery" defaults."""
    command, args = argv[0], subprocess.list2cmdline(argv[1:])
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>pingclaude: sends a short message to Claude Code on a schedule.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".format(user=escape(user), command=escape(command), args=escape(args), cwd=escape(str(pc.home_dir())))


def _run_key_value() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, TASK_NAME)[0]
    except OSError:
        return ""


def _windows_start_detached(argv: List[str]) -> None:
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, cwd=str(pc.ensure_home()),
    )


def _windows_install(argv: List[str]) -> List[str]:
    home = pc.ensure_home()
    xml_path = home / "task.xml"
    xml_path.write_text(windows_task_xml(argv, windows_user()), encoding="utf-16")
    try:
        result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass
    if result.returncode == 0:
        started = _run(["schtasks", "/Run", "/TN", TASK_NAME])
        if started.returncode != 0:
            _windows_start_detached(argv)
        return ["Created Task Scheduler task %r (runs at log-on) and started it." % TASK_NAME]

    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(argv))
    _windows_start_detached(argv)
    return [
        "Task Scheduler refused (%s)." % (_output(result) or "unknown error"),
        "Used the per-user Run registry key instead (HKCU\\%s\\%s) and started it." % (RUN_KEY, TASK_NAME),
    ]


def _windows_uninstall() -> List[str]:
    lines = []
    if _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0:
        _run(["schtasks", "/End", "/TN", TASK_NAME])
        result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if result.returncode != 0:
            raise AutostartError("could not delete task: %s" % _output(result))
        lines.append("Deleted Task Scheduler task %r." % TASK_NAME)
    if _run_key_value():
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, TASK_NAME)
        lines.append("Removed the Run registry entry %r." % TASK_NAME)
    return lines or ["Auto-start was not set up; nothing to remove."]


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------

def macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def macos_plist(argv: List[str], path_env: str) -> bytes:
    data = {
        "Label": LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        # launchd starts agents with a bare PATH; keep the one claude was found on.
        "EnvironmentVariables": {"PATH": path_env},
        "WorkingDirectory": str(pc.home_dir()),
        "LimitLoadToSessionType": "Aqua",
    }
    return plistlib.dumps(data)


def _gui_domain() -> str:
    return "gui/%d" % os.getuid()


def _macos_install(argv: List[str]) -> List[str]:
    path = macos_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pc.ensure_home()
    _run(["launchctl", "bootout", "%s/%s" % (_gui_domain(), LABEL)])  # reload if it exists
    path.write_bytes(macos_plist(argv, os.environ.get("PATH", "/usr/bin:/bin")))
    result = _run(["launchctl", "bootstrap", _gui_domain(), str(path)])
    if result.returncode != 0:
        legacy = _run(["launchctl", "load", "-w", str(path)])
        if legacy.returncode != 0:
            raise AutostartError("launchctl could not load %s: %s" % (path, _output(result)))
    return ["Created LaunchAgent %s and started it." % path]


def _macos_uninstall() -> List[str]:
    path = macos_plist_path()
    if not path.exists():
        return ["Auto-start was not set up; nothing to remove."]
    result = _run(["launchctl", "bootout", "%s/%s" % (_gui_domain(), LABEL)])
    if result.returncode != 0:
        _run(["launchctl", "unload", "-w", str(path)])
    path.unlink()
    return ["Unloaded and deleted LaunchAgent %s." % path]


# --------------------------------------------------------------------------
# Linux
# --------------------------------------------------------------------------

def linux_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT_NAME


def _systemd_quote(arg: str) -> str:
    arg = arg.replace("%", "%%").replace("$", "$$")
    if any(c in arg for c in ' \t"\'\\'):
        return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"')
    return arg


def linux_unit(argv: List[str], path_env: str) -> str:
    return """[Unit]
Description=pingclaude - send a short message to Claude Code on a schedule
After=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Environment={path}
WorkingDirectory={cwd}
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
""".format(
        exec_start=" ".join(_systemd_quote(a) for a in argv),
        path=_systemd_quote("PATH=" + path_env),
        cwd=_systemd_quote(str(pc.home_dir())),
    )


def _linux_install(argv: List[str]) -> List[str]:
    path = linux_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pc.ensure_home()
    path.write_text(linux_unit(argv, os.environ.get("PATH", "/usr/bin:/bin")), encoding="utf-8")
    for step in (["daemon-reload"], ["enable", UNIT_NAME], ["restart", UNIT_NAME]):
        result = _run(["systemctl", "--user"] + step)
        if result.returncode != 0:
            raise AutostartError("systemctl --user %s failed: %s" % (" ".join(step), _output(result)))
    return ["Created systemd user service %s, enabled and started it." % path]


def _linux_uninstall() -> List[str]:
    path = linux_unit_path()
    if not path.exists():
        return ["Auto-start was not set up; nothing to remove."]
    _run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
    path.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    return ["Stopped, disabled and deleted systemd user service %s." % path]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def describe_install(argv: List[str]) -> List[str]:
    cmd = pc.printable(argv)
    name = platform_name()
    if name == "windows":
        return [
            "create a Task Scheduler task %r for %s that runs at log-on:" % (TASK_NAME, windows_user()),
            "    " + cmd,
            "  (no admin rights; runs on battery; no time limit)",
            "start it now",
        ]
    if name == "macos":
        return ["write LaunchAgent %s that runs at log-in:" % macos_plist_path(), "    " + cmd, "load it now with launchctl"]
    return [
        "write systemd user service %s:" % linux_unit_path(),
        "    " + cmd,
        "run: systemctl --user daemon-reload && systemctl --user enable %s && systemctl --user restart %s" % (UNIT_NAME, UNIT_NAME),
    ]


def describe_uninstall() -> List[str]:
    name = platform_name()
    if name == "windows":
        return ["delete Task Scheduler task %r (and the Run registry entry, if one was used)" % TASK_NAME]
    if name == "macos":
        return ["unload and delete %s" % macos_plist_path()]
    return ["stop, disable and delete %s" % linux_unit_path()]


def install(argv: List[str]) -> List[str]:
    return {"windows": _windows_install, "macos": _macos_install, "linux": _linux_install}[platform_name()](argv)


def uninstall() -> List[str]:
    return {"windows": _windows_uninstall, "macos": _macos_uninstall, "linux": _linux_uninstall}[platform_name()]()


def is_installed() -> bool:
    name = platform_name()
    if name == "windows":
        return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0 or bool(_run_key_value())
    if name == "macos":
        return macos_plist_path().exists()
    return linux_unit_path().exists()


def start() -> None:
    """Start the installed auto-start entry now (used when it was stopped)."""
    name = platform_name()
    if name == "windows":
        if _run(["schtasks", "/Run", "/TN", TASK_NAME]).returncode == 0:
            return
        value = _run_key_value()
        if not value:
            raise AutostartError("auto-start is not set up")
        _windows_start_detached(launch_argv("pingclaude_tray" in value))
    elif name == "macos":
        result = _run(["launchctl", "kickstart", "%s/%s" % (_gui_domain(), LABEL)])
        if result.returncode != 0:
            raise AutostartError("launchctl kickstart failed: %s" % _output(result))
    else:
        result = _run(["systemctl", "--user", "start", UNIT_NAME])
        if result.returncode != 0:
            raise AutostartError("systemctl --user start failed: %s" % _output(result))
