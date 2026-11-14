"""v2.0: backup/restore, morning briefing, nightly self-improvement (local AI only)."""
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from friday import activity, backup, routines, skills
from friday import tools as T
from friday.env import data_dir


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    (tmp_path / "skills").mkdir()
    from friday import config
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("SHOP_NAME=Test\n")
    return tmp_path


def test_backup_and_restore(dirs, monkeypatch):
    (data_dir() / "notes.txt").write_text("first note\n")
    (dirs / "skills" / "my_skill.py").write_text("# skill v1\n")
    p = backup.make_backup(include_env=True)  # keys are opt-in now; this test restores .env, so ask for it
    names = __import__("zipfile").ZipFile(p).namelist()
    assert {"data/notes.txt", "skills/my_skill.py", ".env"} <= set(names)
    (data_dir() / "notes.txt").write_text("CHANGED\n")
    (dirs / ".env").write_text("BROKEN\n")
    time.sleep(1.1)  # distinct backup timestamps
    no = T.ToolContext(confirm=lambda p: False)
    assert "Cancelled" in T.dispatch("restore_backup", {}, no)  # risk=delete always asks
    out = T.dispatch("restore_backup", {"name": p.name}, T.ToolContext(confirm=lambda p: True))
    assert "Restored" in out
    assert (data_dir() / "notes.txt").read_text() == "first note\n"
    assert (dirs / ".env").read_text() == "SHOP_NAME=Test\n"
    assert any("before-restore" in b.name for b in backup.list_all())


def test_backup_keeps_last_n(dirs, monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "2")
    for i in range(3):
        backup.make_backup(label=f"n{i}")
    assert len(backup.list_all()) == 2


def test_routine_due_window_and_once_a_day(monkeypatch):
    monkeypatch.setenv("BRIEFING_TIME", "07:30")
    assert not routines.is_due("briefing", "07:30", "07:30", datetime(2026, 11, 10, 7, 0))
    assert routines.is_due("briefing", "07:30", "07:30", datetime(2026, 11, 10, 7, 45))
    assert not routines.is_due("briefing", "07:30", "07:30", datetime(2026, 11, 10, 15, 0))  # too late
    routines._mark("briefing", datetime(2026, 11, 10, 7, 45))
    assert not routines.is_due("briefing", "07:30", "07:30", datetime(2026, 11, 10, 8, 0))
    assert routines.is_due("briefing", "bad", "07:30", datetime(2026, 11, 11, 7, 31))


def test_tick_delivers_briefing_once(monkeypatch):
    monkeypatch.setenv("NIGHTLY_ENABLED", "false")
    monkeypatch.setattr(T.REGISTRY["get_weather"], "func", lambda ctx, city="": "Sunny, 12 C.")
    said = []
    T.dispatch("add_job", {"customer": "Bob", "vehicle": "RSV4", "work": "service"}, T.ToolContext())
    a = SimpleNamespace(ctx=T.ToolContext(), _announce=said.append)
    now = datetime.now().replace(hour=7, minute=40)
    monkeypatch.setenv("BRIEFING_TIME", "07:30")
    assert routines.tick(a, now) == ["briefing"]
    for _ in range(50):
        if said:
            break
        time.sleep(0.05)
    assert "Good morning" in said[0] and "Sunny" in said[0] and "1 booked" in said[0]
    assert routines.tick(a, now) == []


GOOD = '''# Request: say hello
from friday.tools import tool

@tool("say_hello", "Say hello.")
def say_hello(ctx):
    return "hello"
'''


class FakeLocal:
    label = "ollama/qwen2.5-coder:14b"

    def __init__(self):
        self.calls = 0

    def _raw_complete(self, system, user):
        self.calls += 1
        assert "broken" in user
        return "```python\n" + GOOD.split("\n", 1)[1] + "```"


def test_nightly_repairs_broken_skill_locally(dirs, monkeypatch):
    monkeypatch.setenv("AUTONOMY_LEVEL", "1")
    (dirs / "skills" / "say_hello.py").write_text(GOOD.replace('return "hello"', "return undefined_name"))
    skills.load_all()
    assert "say_hello" in skills.ERRORS
    local = FakeLocal()
    brain = SimpleNamespace(_local_brain=lambda model="": local)
    activity.log("error", "say_hello: boom", tool="x")
    report = routines.run_nightly(T.ToolContext(), brain)
    assert local.calls >= 1
    assert "waiting for your OK" in report and "Summary:" in report
    assert (routines.fixes_dir() / "say_hello.py").exists()
    assert list(backup.list_all())
    out = T.dispatch("install_nightly_fixes", {}, T.ToolContext(confirm=lambda p: True))
    assert "say_hello" in out and T.dispatch("say_hello", {}, T.ToolContext()) == "hello"
    assert "Overnight" in routines.build_briefing(T.ToolContext())
    skills.load_all()


def test_nightly_never_uses_cloud(dirs, monkeypatch):
    (dirs / "skills" / "say_hello.py").write_text(GOOD.replace('return "hello"', "return undefined_name"))
    skills.load_all()
    brain = SimpleNamespace(_local_brain=lambda model="": None,
                            complete=lambda *a, **k: pytest.fail("cloud must not be used at night"))
    report = routines.run_nightly(T.ToolContext(), brain)
    assert "Repair of 'say_hello' failed" in report and "no local AI" in report
    for p in (dirs / "skills").glob("say_hello.py"):
        p.unlink()
    skills.load_all()
