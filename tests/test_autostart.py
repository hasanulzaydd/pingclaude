import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import pingclaude as pc
import pingclaude_autostart as autostart

ARGV = [r"C:\Program Files\Python\pythonw.exe", r"C:\Users\Ann Lee\pingclaude.py", "run", "--background"]
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_launch_argv_points_at_real_files():
    argv = autostart.launch_argv()
    assert Path(argv[1]).name == "pingclaude.py" and Path(argv[1]).exists()
    assert argv[2:] == ["run", "--background"]
    assert Path(autostart.launch_argv(tray=True)[1]).name == "pingclaude_tray.py"


def test_windows_task_xml_is_valid_and_laptop_friendly():
    xml = autostart.windows_task_xml(ARGV, "PC\\Ann & Co")
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    settings = root.find("t:Settings", NS)
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    assert settings.find("t:StopIfGoingOnBatteries", NS).text == "false"
    assert settings.find("t:ExecutionTimeLimit", NS).text == "PT0S"
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == "PC\\Ann & Co"
    exec_ = root.find("t:Actions/t:Exec", NS)
    assert exec_.find("t:Command", NS).text == ARGV[0]
    assert exec_.find("t:Arguments", NS).text == '"C:\\Users\\Ann Lee\\pingclaude.py" run --background'


def test_macos_plist_keeps_path_and_runs_at_load():
    data = plistlib.loads(autostart.macos_plist(["/usr/bin/python3", "/x/pingclaude.py", "run"], "/opt/homebrew/bin:/usr/bin"))
    assert data["Label"] == autostart.LABEL
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"] == ["/usr/bin/python3", "/x/pingclaude.py", "run"]
    assert data["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin"
    assert "KeepAlive" not in data  # a second copy exiting must not cause a restart loop


def test_linux_unit_quotes_arguments():
    unit = autostart.linux_unit(["/usr/bin/python3", "/home/ann lee/pingclaude.py", "run", "50%"], "/usr/bin:/bin")
    assert 'ExecStart=/usr/bin/python3 "/home/ann lee/pingclaude.py" run 50%%' in unit
    assert "Environment=PATH=/usr/bin:/bin" in unit
    assert "WantedBy=default.target" in unit


def test_describe_lists_the_command():
    lines = autostart.describe_install(ARGV)
    assert any("pingclaude.py" in line for line in lines)
    assert autostart.describe_uninstall()


def test_install_declined_changes_nothing(monkeypatch, capsys):
    def explode(*a, **k):
        raise AssertionError("must not install without confirmation")

    monkeypatch.setattr(autostart, "install", explode)
    monkeypatch.setattr(pc.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert pc.main(["install", "--start-at", "06:00"]) == 1
    out = capsys.readouterr().out
    assert "This will:" in out and "Nothing changed" in out
    assert not pc.config_path().exists()


def test_install_confirmed_saves_config(monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, "install", lambda argv: calls.append(argv) or ["ok"])
    monkeypatch.setattr(pc.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert pc.main(["install", "--start-at", "06:00", "--interval", "5h"]) == 0
    assert calls and calls[0][-2:] == ["run", "--background"]
    assert pc.load_config()["start_at"] == "06:00"


def test_install_requires_claude(monkeypatch):
    monkeypatch.setattr(pc.shutil, "which", lambda name: None)
    assert pc.main(["install", "-y"]) == 1


def test_uninstall_declined_changes_nothing(monkeypatch):
    monkeypatch.setattr(autostart, "uninstall", lambda: pytest.fail("must ask first"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert pc.main(["uninstall"]) == 1
