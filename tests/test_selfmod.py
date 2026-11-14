import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from friday import jobs, quick, selfmod
from friday import tools as T


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    """A tiny fake Jarvis project + data folder, so nothing real is touched."""
    root = tmp_path / "proj"
    (root / "friday").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "friday" / "hello.py").write_text('GREETING = "Hello"\n\n\ndef greet():\n    return GREETING\n', encoding="utf-8")
    (root / "friday" / "autonomy.py").write_text("LEVEL = 1\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("notes", encoding="utf-8")
    data = tmp_path / "selfmod_data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(selfmod, "ROOT", root)
    monkeypatch.setenv("DATA_DIR", str(data))
    return root


GOOD = {"explanation": "Greets with Good day instead of Hello.",
        "edits": [{"path": "friday/hello.py", "old": 'GREETING = "Hello"', "new": 'GREETING = "Good day"'}],
        "new_files": [], "new_tests": [{"path": "tests/test_hello.py", "content": "def test_x():\n    assert True\n"}]}


def test_parse_proposal_from_fenced_or_chatty_reply():
    p = selfmod.parse_proposal("Sure!\n```json\n" + json.dumps(GOOD) + "\n```")
    assert p["edits"][0]["path"] == "friday/hello.py" and p["new_files"] == []
    assert selfmod.parse_proposal("here: " + json.dumps(GOOD) + " done")["explanation"].startswith("Greets")
    with pytest.raises(ValueError):
        selfmod.parse_proposal("I can't do that")


def test_safety_files_and_outside_paths_are_refused():
    assert "guards Jarvis's safety" in selfmod.check_path("friday/autonomy.py")
    assert "guards" in selfmod.check_path("friday/credits.py") and "guards" in selfmod.check_path("friday\\selfmod.py")
    assert "outside" in selfmod.check_path(".env") and "outside" in selfmod.check_path("scripts/selfmod_guard.py")
    assert "valid" in selfmod.check_path("friday/../.env")
    assert selfmod.check_path("friday/hello.py") == "" and selfmod.check_path("tests/test_x.py") == ""


def test_apply_to_exact_once_compile_and_all_or_nothing(proj):
    work = selfmod.make_workspace(proj)
    mod, created, problem = selfmod.apply_to(work, selfmod.parse_proposal(json.dumps(GOOD)))
    assert (mod, created, problem) == (["friday/hello.py"], ["tests/test_hello.py"], "")
    assert "Good day" in (work / "friday/hello.py").read_text() and "Hello" in (proj / "friday/hello.py").read_text()
    w2 = selfmod.make_workspace(proj)
    bad = dict(GOOD, edits=[{"path": "friday/hello.py", "old": "NOT THERE", "new": "x"}], new_tests=[])
    assert "0 times" in selfmod.apply_to(w2, bad)[2]
    broken = dict(GOOD, edits=[{"path": "friday/hello.py", "old": "return GREETING", "new": "return ("}], new_tests=[])
    assert "would not compile" in selfmod.apply_to(w2, broken)[2]
    assert 'return GREETING' in (w2 / "friday/hello.py").read_text()  # nothing was written
    prot = dict(GOOD, edits=[{"path": "friday/autonomy.py", "old": "LEVEL = 1", "new": "LEVEL = 2"}], new_tests=[])
    assert "guards" in selfmod.apply_to(w2, prot)[2]


def test_risky_code_is_flagged(proj):
    work = selfmod.make_workspace(proj)
    evil = dict(GOOD, edits=[{"path": "friday/hello.py", "old": "return GREETING", "new": "return eval(GREETING)"}], new_tests=[])
    mod, created, problem = selfmod.apply_to(work, evil)
    assert problem == "" and selfmod.risky_lines(proj, work, mod, created)
    ok = selfmod.make_workspace(proj)
    mod, created, _ = selfmod.apply_to(ok, selfmod.parse_proposal(json.dumps(GOOD)))
    assert selfmod.risky_lines(proj, ok, mod, created) == []


def _ctx(reply, approve=True):
    log = SimpleNamespace(confirmed=[], restarted=[], spoken=[])
    ctx = T.ToolContext(speak=log.spoken.append, confirm=lambda p: log.confirmed.append(p) or approve,
                        control=lambda a: log.restarted.append(a) or "ok")
    ctx.ask_ai = lambda system, user, **k: (reply, "test-ai")
    return ctx, log


def test_full_flow_approved_applies_backs_up_and_restarts(proj, monkeypatch):
    monkeypatch.setattr(selfmod, "run_tests", lambda work, timeout=0: (True, "5 passed"))
    monkeypatch.setattr(selfmod, "arm_watcher", lambda minutes=3: None)
    monkeypatch.setattr(jobs, "start", lambda title, work, notify=None, wait=False: (work(lambda t: None), SimpleNamespace(id=1))[1])
    ctx, log = _ctx(json.dumps(GOOD))
    selfmod.change_myself(ctx, "greet me with Good day instead of Hello")
    assert "Good day" in (proj / "friday/hello.py").read_text() and (proj / "tests/test_hello.py").exists()
    assert log.restarted == ["restart_friday"] and "5 passed" in log.confirmed[0] and "Greets with Good day" in log.confirmed[0]
    st = selfmod.state()
    assert st["status"] == "applied" and not st["verified"] and "Hello" in (Path(st["backup"]) / "friday/hello.py").read_text()
    # undo puts the old file back and removes the new test
    out = selfmod.restore()
    assert "Hello" in (proj / "friday/hello.py").read_text() and not (proj / "tests/test_hello.py").exists() and "previous" in out
    assert selfmod.state()["status"] == "undone"


def test_declined_or_failing_changes_leave_everything_alone(proj, monkeypatch):
    monkeypatch.setattr(jobs, "start", lambda title, work, notify=None, wait=False: (work(lambda t: None), SimpleNamespace(id=1))[1])
    monkeypatch.setattr(selfmod, "run_tests", lambda work, timeout=0: (True, "ok"))
    ctx, log = _ctx(json.dumps(GOOD), approve=False)
    selfmod.change_myself(ctx, "greet me with Good day instead of Hello")
    assert "Hello" in (proj / "friday/hello.py").read_text() and log.restarted == [] and selfmod.state() == {}
    monkeypatch.setattr(selfmod, "run_tests", lambda work, timeout=0: (False, "1 failed"))
    ctx2, log2 = _ctx(json.dumps(GOOD))
    selfmod.change_myself(ctx2, "greet me with Good day instead of Hello")
    assert log2.confirmed == [] and "Hello" in (proj / "friday/hello.py").read_text()  # never even asked: tests failed


def test_guard_script_restores_when_the_new_version_never_became_healthy(proj, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("guard", Path(selfmod.__file__).parent.parent / "scripts" / "selfmod_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    monkeypatch.setattr(guard, "ROOT", proj)
    backup = tmp_path / "bk"
    (backup / "friday").mkdir(parents=True)
    (backup / "friday/hello.py").write_text('GREETING = "Hello"\n', encoding="utf-8")
    (proj / "friday/hello.py").write_text("BROKEN(", encoding="utf-8")
    (proj / "friday/newmod.py").write_text("x = 1\n", encoding="utf-8")
    selfmod._save_state({"status": "applied", "verified": False, "backup": str(backup),
                         "modified": ["friday/hello.py"], "created": ["friday/newmod.py"]})
    assert guard.main([]) == 0
    assert "Hello" in (proj / "friday/hello.py").read_text() and not (proj / "friday/newmod.py").exists()
    assert selfmod.state()["status"] == "rolled_back"
    selfmod._save_state({"status": "applied", "verified": False, "backup": str(backup), "modified": [], "created": []})
    selfmod.mark_verified()
    assert selfmod.state()["status"] == "ok" and not guard.needs_rollback(selfmod.state())


def test_quick_routes_for_self_change(monkeypatch):
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name}")
    assert quick.handle(brain, "Jarvis, change yourself so that you always greet me by name") == "change_myself"
    assert quick.handle(brain, "undo your last change") == "undo_last_change"
    assert quick.handle(brain, "reprogram yourself to answer faster on weekends") == "change_myself"
    assert quick.handle(brain, "change the volume to 50") != "change_myself"
