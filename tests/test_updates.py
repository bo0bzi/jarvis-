"""Abacus has no limit (only a warning every 1,000 credits), Jarvis tells you things without being asked, he stays running,
and he can fix himself when asked."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from friday import blackbox, credits, selfmod, updates
from friday import tools as T
from friday.credits import CreditGate

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_activity():
    from friday import activity

    activity._recent.clear()  # the in-memory activity list is shared by every test in the run
    yield
    activity._recent.clear()


# ------------------------------------------------------------------ Abacus: no limit, only a warning
@pytest.fixture
def gate_env(monkeypatch):
    monkeypatch.setenv("CREDITS_PER_1K_TOKENS", "1")
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "0")
    monkeypatch.setenv("CREDIT_DAILY_CAP", "1")
    monkeypatch.setenv("CREDIT_MONTHLY_CAP", "1")
    monkeypatch.setenv("ABACUS_WARN_EVERY", "1000")


def test_abacus_has_no_cap_no_question_and_warns_each_1000_credits(gate_env):
    asked, warns = [], []
    g = CreditGate(confirm=lambda p: asked.append(p) or False)
    g.on_warn = warns.append
    with g.task("a long job", "x", 1, 2, "abacus/route-llm") as ok:
        assert ok
        g.before_call("abacus/route-llm")
        g.charge(600_000, 0)
        assert warns == []                                   # 600 so far
        g.charge(500_000, 0)                                 # 1,100: past 1,000
        assert len(warns) == 1 and "1,100" in warns[0] and "past 1,000" in warns[0]
        g.before_call("abacus/route-llm")                    # way past its estimate and the caps: nothing is asked
        g.charge(1_000_000, 0)                               # 2,100: past 2,000
    assert len(warns) == 2 and "past 2,000" in warns[1] and asked == []
    assert g.spent_today() == 0 and g.abacus_today() == 2100       # the caps (for Claude) are untouched
    st = g.status()
    assert st["abacus_total"] == 2100 and st["abacus_warn_every"] == 1000 and st["ledger"][-1]["how"].startswith("Abacus has no limit")


def test_claude_and_other_cloud_ais_are_still_capped_and_ask(gate_env):
    asked = []
    g = CreditGate(confirm=lambda p: asked.append(p) or True)
    with g.task("a Claude job", "x", 5, 9, "anthropic/claude-sonnet") as ok:
        assert not ok                                        # estimate 5 passes today's cap of 1
    assert asked == [] and "cap" in g.last_denial
    with g.task("a Claude question", "x", 1, 1, "anthropic/claude-sonnet", always_ask=True) as ok:
        assert ok and len(asked) == 1                        # and Claude always asks


def test_warnings_can_be_switched_off_and_old_spending_does_not_trigger_one(gate_env, monkeypatch, tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "credits.json").write_text(json.dumps({"days": {today: 5400}}), encoding="utf-8")
    warns = []
    g = CreditGate(confirm=lambda p: False)
    g.on_warn = warns.append
    with g.task("job", "x", 1, 2, "abacus/route-llm"):
        g.charge(50_000, 0)                                  # 5,450: what was spent before is not warned about again
        assert warns == []
        g.charge(600_000, 0)                                 # 6,050: a new multiple of 1,000
        assert len(warns) == 1 and "past 6,000" in warns[0]
    monkeypatch.setenv("ABACUS_WARN_EVERY", "0")
    with g.task("job", "x", 1, 2, "abacus/route-llm"):
        g.charge(5_000_000, 0)
    assert len(warns) == 1                                   # 0 = never warn


def test_credit_status_tool_says_abacus_is_unlimited(gate_env):
    import friday.main  # noqa: F401

    credits.GATE = CreditGate(confirm=lambda p: False)
    with credits.GATE.task("job", "x", 1, 2, "abacus/route-llm"):
        credits.GATE.charge(10_000, 0)
    out = T.dispatch("credit_status", {}, T.ToolContext())
    assert "Abacus: 10 credits today" in out and "No limit" in out and "Other cloud AIs (Claude)" in out


# ------------------------------------------------------------------ updates without being asked
class Bus:
    def __init__(self):
        self.status, self.flags, self.pending_confirm, self.level = {"state": "sleeping"}, {}, None, 0.001


def _assistant(**kw):
    a = NS(tts=NS(muted=False, busy=lambda: False), bus=Bus(), recorder=NS(threshold=0.03), mode="always", said=[],
           s=NS(provider="custom", base_url="http://localhost:11434/v1"), brain=NS(last_error=""))
    a._announce = a.said.append
    for k, v in kw.items():
        setattr(a, k, v)
    return a


@pytest.fixture
def pushes(monkeypatch):
    from friday import notify

    got = []
    monkeypatch.setattr(notify, "push", lambda title, body="", text_too=None, **k: got.append((title, body, text_too)))
    updates._PENDING.clear()
    updates._STATE.update(health_at=0.0, ollama_at=0.0, fails={}, bad=set())
    return got


def test_quiet_hours_wrap_past_midnight(monkeypatch):
    monkeypatch.setenv("UPDATES_QUIET_HOURS", "22:00-07:00")
    d = lambda h, m=0: datetime(2026, 11, 14, h, m)  # noqa: E731
    assert updates.quiet_now(d(23)) and updates.quiet_now(d(3)) and updates.quiet_now(d(6, 59))
    assert not updates.quiet_now(d(7)) and not updates.quiet_now(d(12)) and not updates.quiet_now(d(21, 59))
    monkeypatch.setenv("UPDATES_QUIET_HOURS", "garbage")
    assert not updates.quiet_now(d(3))


def test_an_update_is_logged_pushed_and_spoken_only_at_a_good_moment(pushes, monkeypatch):
    monkeypatch.setenv("UPDATES_QUIET_HOURS", "00:00-00:00")
    updates.send("credits", "Abacus credits used so far: 1,100.", important=True)
    assert updates.recent(1)[0]["kind"] == "credits" and pushes == [("Jarvis", "Abacus credits used so far: 1,100.", True)]
    a = _assistant()
    assert updates.deliver_pending(a) and a.said == ["Abacus credits used so far: 1,100."]
    for busy in (dict(tts=NS(muted=False, busy=lambda: True)), dict(tts=NS(muted=True, busy=lambda: False))):
        updates.send("x", "hello there")
        b = _assistant(**busy)
        assert not updates.deliver_pending(b) and b.said == []
        updates._PENDING.clear()
    talking = _assistant()
    talking.bus.status = {"state": "thinking"}
    updates.send("x", "later")
    assert not updates.deliver_pending(talking)
    talking.bus.status = {"state": "sleeping"}
    talking.bus.flags = {"conversation_until": 9e12}               # in a conversation with him
    assert not updates.deliver_pending(talking)
    talking.bus.flags, talking.bus.level = {}, 0.5                  # someone is talking right now
    assert not updates.deliver_pending(talking)
    talking.bus.level = 0.001
    assert updates.deliver_pending(talking) and talking.said == ["later"]


def test_in_the_quiet_hours_it_goes_to_the_phone_but_is_not_spoken(pushes, monkeypatch):
    monkeypatch.setattr(updates, "quiet_now", lambda now=None: True)
    updates.send("health", "My local AI stopped answering.", important=True)
    assert pushes and not updates._PENDING
    assert not updates.deliver_pending(_assistant())
    monkeypatch.setenv("UPDATES_ENABLED", "false")
    assert updates.send("x", "nothing") is None


def test_a_problem_is_reported_once_it_lasts_and_again_when_fixed(pushes, monkeypatch):
    monkeypatch.setattr(updates, "quiet_now", lambda now=None: False)
    started = []
    monkeypatch.setattr(updates, "_start_ollama", lambda: started.append(1) or True)
    state = {"ok": False}
    monkeypatch.setattr(updates, "check_health", lambda a: [("local_ai", state["ok"], "My local AI (Ollama) stopped answering.")])
    a = _assistant()
    assert updates.health_tick(a) == [] and started == [1]          # first miss: try to start it, say nothing yet
    assert updates.health_tick(a) == ["My local AI (Ollama) stopped answering."]
    assert updates.health_tick(a) == [] and len(pushes) == 1       # not repeated every minute
    state["ok"] = True
    assert "fixed" in updates.health_tick(a)[0] and len(pushes) == 2
    assert updates.health_tick(a) == []


def test_health_checks_cover_the_local_ai_microphone_and_brain(monkeypatch):
    monkeypatch.setattr(updates, "_http_ok", lambda url, timeout=2.0: "localhost:11434" in url)
    a = _assistant()
    got = {k: ok for k, ok, _ in updates.check_health(a)}
    assert got == {"local_ai": True, "microphone": True, "brain": True}
    a.recorder, a.brain.last_error = None, "Connection error"
    got = {k: ok for k, ok, _ in updates.check_health(a)}
    assert got["microphone"] is False and got["brain"] is False


def test_a_checkin_happens_only_when_something_did_and_never_in_quiet_hours(pushes, monkeypatch):
    monkeypatch.setenv("UPDATES_EVERY_HOURS", "4")
    monkeypatch.setattr(updates, "quiet_now", lambda now=None: False)
    now = datetime.now()
    assert updates.checkin_tick(now) == ""                          # first time: starts the clock
    assert updates.checkin_tick(now + timedelta(hours=1)) == ""     # too soon
    assert updates.checkin_tick(now + timedelta(hours=5)) == ""     # nothing happened: nothing said
    blackbox.begin_turn("what motherboard do i have", "abacus/route-llm")
    blackbox.record_action("run_shell_command", {"command": "wmic baseboard get product"}, "X370", 1.0, "abacus/route-llm", True)
    blackbox.note("reply", "A Gigabyte X370.", brain="abacus/route-llm", cloud=True)
    text = updates.checkin_tick(now + timedelta(hours=10))
    assert "1 request(s), 1 action(s), 1 done by Abacus" in text and pushes[-1][1].startswith("Since my last update")
    monkeypatch.setattr(updates, "quiet_now", lambda now=None: True)
    assert updates.checkin_tick(now + timedelta(hours=20)) == ""


def test_give_update_tool_and_the_credit_warning_reaches_the_phone(pushes, gate_env):
    import friday.main  # noqa: F401

    assert "all quiet" in T.dispatch("give_update", {}, T.ToolContext()).lower()
    g = CreditGate(confirm=lambda p: False)
    g.on_warn = lambda msg: updates.send("credits", msg, important=True)
    with g.task("job", "x", 1, 2, "abacus/route-llm"):
        g.charge(1_200_000, 0)
    assert pushes and "Abacus credits used so far: 1,200" in pushes[0][1] and pushes[0][2] is True


# ------------------------------------------------------------------ always running
def test_the_keep_alive_task_scripts_exist_and_the_launchers_use_them():
    install = (ROOT / "scripts" / "install_keepalive.ps1").read_text(encoding="utf-8")
    keep = (ROOT / "scripts" / "keep_friday_running.ps1").read_text(encoding="utf-8")
    assert "Friday keep-alive" in install and "-AtLogOn" in install and "RepetitionInterval" in install and "-Remove" in install
    assert "run_friday.bat" in keep and "ollama" in keep.lower() and "-DryRun" in keep and "-m\\s+friday" in keep
    for name, needle in (("enable_start_on_boot.bat", b"install_keepalive.ps1"), ("disable_start_on_boot.bat", b"-Remove")):
        raw = (ROOT / name).read_bytes()
        assert b"\r\n" in raw and needle in raw


# ------------------------------------------------------------------ fixing himself when asked
def test_change_myself_starts_without_a_first_question_and_always_uses_the_strongest_ai(monkeypatch):
    import friday.main  # noqa: F401

    assert T.REGISTRY["change_myself"].dangerous is False and T.REGISTRY["undo_last_change"].dangerous is True
    seen = {}
    monkeypatch.setattr(selfmod, "propose", lambda ctx, request, strong, progress=None: seen.update(strong=strong) or (None, None, "No."))
    monkeypatch.setattr(selfmod.jobs, "start", lambda name, work, notify=None: (work(lambda t: None), NS(id=7))[1])
    out = T.dispatch("change_myself", {"request": "fix the microphone so it lets me finish talking"}, T.ToolContext())
    assert "job 7" in out and seen == {"strong": True}


def test_the_drafting_ai_is_shown_the_recent_errors_to_find_what_is_broken():
    from friday import activity

    activity.log("error", "listen: the microphone stopped: PortAudioError -9999")
    seen = []

    def ask(system, user, **k):
        seen.append(user)
        return json.dumps({"explanation": "x", "edits": [], "new_files": [], "new_tests": []}), "test-ai"

    selfmod.draft(NS(ask_ai=ask), "fix the microphone problem that keeps happening")
    assert "Recent errors Jarvis logged" in seen[0] and "PortAudioError -9999" in seen[0]
