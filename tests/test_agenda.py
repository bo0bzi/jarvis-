from datetime import datetime
from types import SimpleNamespace

from friday import agenda, quick, timeparse
from friday import tools as T

NOW = datetime(2026, 11, 12, 15, 0)

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:1
DTSTART;VALUE=DATE:20261113
SUMMARY:Shop closed
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART:20261112T180000Z
DTEND:20261112T190000Z
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART:20261109T140000Z
DTEND:20261109T150000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Supplier call
END:VEVENT
END:VCALENDAR
"""


def test_parse_when():
    assert timeparse.parse_when("2026-11-20 15:30", NOW) == datetime(2026, 11, 20, 15, 30)
    assert timeparse.parse_when("tomorrow at 3pm", NOW) == datetime(2026, 11, 13, 15, 0)
    assert timeparse.parse_when("friday 10:30", NOW) == datetime(2026, 11, 13, 10, 30)
    assert timeparse.parse_when("monday", NOW) == datetime(2026, 11, 16, 9, 0)
    assert timeparse.parse_when("at 2pm", NOW) == datetime(2026, 11, 13, 14, 0)  # already past today -> tomorrow
    assert timeparse.parse_when("blah", NOW) is None


def test_ics_feed_with_repeats_all_day_and_zones():
    evs = agenda.feed_events(datetime(2026, 11, 12), datetime(2026, 11, 20), ics=ICS)
    titles = sorted(e["title"] for e in evs)
    assert titles.count("Supplier call") == 1  # the weekly Monday call: only Nov 16 falls in this range
    assert "Dentist" in titles and "Shop closed" in titles
    assert next(e for e in evs if e["title"] == "Shop closed")["all_day"]


def test_add_list_cancel_and_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("CALENDAR_ICS_URL", "")
    ctx = T.ToolContext(settings=SimpleNamespace(data_dir=tmp_path), speak=lambda t: None)
    when = (datetime.now().replace(second=0, microsecond=0) + __import__("datetime").timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    out = agenda.add_event(ctx, "Quote for Mike", when, minutes=30)
    assert out.startswith("Added 'Quote for Mike'")
    assert any(t["id"].startswith("ev-") and t.get("transient") for t in ctx.timers)  # alert is armed
    assert "Quote for Mike" in agenda.list_events(ctx, "today") + agenda.list_events(ctx, "tomorrow")
    assert "past" in agenda.add_event(ctx, "Old", "2020-01-01 10:00")
    assert agenda.cancel_event(ctx, "mike").startswith("Removed")
    assert not [t for t in ctx.timers if t["id"].startswith("ev-")]
    for t in ctx.timers:
        t["timer"].cancel()


def test_calendar_questions_are_answered_from_the_calendar(monkeypatch):
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    assert quick.handle(brain, "what's on my calendar tomorrow") == "list_events {'range': 'tomorrow'}"
    assert quick.handle(brain, "what's my day") == "list_events {'range': 'today'}"
    assert quick.handle(brain, "show my schedule this week") == "list_events {'range': 'this week'}"
    assert "list_events" not in str(quick.handle(brain, "what day is it"))  # that's the clock, not the calendar
