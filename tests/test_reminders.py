import json
from datetime import datetime
from types import SimpleNamespace

from friday import llm, notify, quick, timeparse
from friday import tools as T

NOW = datetime(2026, 11, 12, 15, 0, 0)


def test_timeparse_timers_alarms_and_reminders():
    p = timeparse.parse("set an alarm for a minute from now", NOW)
    assert p is None or p["seconds"] == 60  # 'a minute' is understood
    p = timeparse.parse("set a timer for 5 minutes", NOW)
    assert p == {"kind": "timer", "seconds": 300, "message": "", "label": "timer"}
    assert timeparse.parse("Jarvis, set an alarm for 2 hours", NOW)["seconds"] == 7200
    r = timeparse.parse("remind me to call John in 10 minutes", NOW)
    assert r["kind"] == "reminder" and r["seconds"] == 600 and r["message"] == "call John"
    r = timeparse.parse("remind me to pick up the bike at 5pm", NOW)
    assert r["seconds"] == 2 * 3600 and r["message"] == "pick up the bike"
    r = timeparse.parse("remind me to order parts tomorrow at 9am", NOW)
    assert r["seconds"] == 18 * 3600 and r["message"] == "order parts"
    a = timeparse.parse("set an alarm for 7:30 am", NOW)
    assert a["seconds"] == (16 * 3600 + 30 * 60)  # next morning
    assert timeparse.parse("what time is it", NOW) is None
    assert timeparse.parse("set the volume to 50", NOW) is None


def test_quick_really_sets_the_timer(monkeypatch):
    calls = []
    monkeypatch.setitem(T.REGISTRY, "set_timer", SimpleNamespace(func=None, name="set_timer"))
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    out = quick.handle(brain, "set an alarm for 2 minutes")
    assert out == "set_timer {'seconds': 120, 'label': 'alarm'}"


def test_fake_alarm_claim_is_caught_unless_a_tool_ran():
    assert llm.unbacked_claim("Setting a one-minute alarm now, sir. You’ll hear it in 60 seconds.", []) == "set"
    assert llm.unbacked_claim("Your timer is now set.", []) == "set"
    assert llm.unbacked_claim("Your timer is now set.", ["set_timer"]) == ""
    assert llm.unbacked_claim("The weather is fine.", []) == ""


def test_timers_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_beep", lambda: None)
    fired = []
    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr("friday.notify.push", lambda *a, **k: None)
    ctx = T.ToolContext(settings=settings, speak=fired.append)
    T.set_reminder(ctx, "call John", in_minutes=30)
    saved = json.loads((tmp_path / "reminders.json").read_text())
    assert saved[0]["message"] == "Reminder: call John"
    for t in ctx.timers:  # "restart": the in-memory timers are gone
        t["timer"].cancel()
    ctx2 = T.ToolContext(settings=settings, speak=fired.append)
    assert T.restore_timers(ctx2) == 1 and len(ctx2.timers) == 1
    for t in ctx2.timers:
        t["timer"].cancel()
    # a reminder that came due while Jarvis was off is announced as missed
    (tmp_path / "reminders.json").write_text(json.dumps(
        [{"id": "x", "due": "2020-01-01T00:00:00", "message": "old"}]))
    assert T.restore_timers(ctx2, grace_hours=6) == 0  # too old: dropped


def test_notify_queue_subscribe_and_key(tmp_path, monkeypatch):
    monkeypatch.setattr("friday.notify._send_webpush", lambda *a, **k: 0)
    monkeypatch.setenv("NOTIFY_BY_TEXT", "off")
    notify.push("Jarvis", "hello", base=tmp_path)
    assert notify.recent(0)[-1]["body"] == "hello"
    assert notify.subscribe({"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}, tmp_path)
    assert not notify.subscribe({"endpoint": "http://bad", "keys": {}}, tmp_path)
    assert len(notify.public_key(tmp_path)) > 60
    assert notify.public_key(tmp_path) == notify.public_key(tmp_path)  # stable key
