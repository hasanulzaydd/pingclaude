import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.pingclaude so real settings are never touched."""
    home = tmp_path / "pchome"
    monkeypatch.setenv("PINGCLAUDE_HOME", str(home))
    return home


class FakeClock:
    """A clock whose `wait` advances time instantly. Hooks can jump time."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        self.waits = []
        self.on_wait = None  # optional callback(clock, seconds)

    def time(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        if self.on_wait:
            self.on_wait(self, seconds)


@pytest.fixture
def clock():
    return FakeClock()
