"""Calendar: Jarvis's own calendar plus (optionally) your Google/Outlook calendar, read-only.

- Your own events are saved in data/calendar.json (add_event / list_events / cancel_event). Each one alerts you
  15 minutes before (voice, beep, phone notification, text).
- To also see your real calendar, put its private ICS address in Settings > Calendar (CALENDAR_ICS_URL):
    Google Calendar: Settings > your calendar > Integrate calendar > "Secret address in iCal format"
    Outlook: Settings > Calendar > Shared calendars > Publish a calendar > ICS link
  That feed is read-only (Jarvis can read it, never change it). Repeating events are handled.
Nothing here is sent anywhere except the one download of that ICS address."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from . import timeparse
from .env import data_dir, env, env_int
from .tools import tool

_LOCK = threading.RLock()
_ICS_CACHE: dict[str, tuple[float, bytes]] = {}


def _file(base: Path | None = None) -> Path:
    return Path(base or data_dir()) / "calendar.json"


def _load(base: Path | None = None) -> list[dict]:
    try:
        return json.loads(_file(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(rows: list[dict], base: Path | None = None) -> None:
    _file(base).write_text(json.dumps(rows, indent=1), encoding="utf-8")


def _base(ctx) -> Path | None:
    return getattr(getattr(ctx, "settings", None), "data_dir", None)


# ------------------------------------------------------------------ the read-only ICS feed (Google / Outlook)
def _download(url: str) -> bytes:
    url = url.strip().replace("webcal://", "https://", 1)
    if not url.startswith("https://"):
        raise ValueError("the calendar address must start with https:// (or webcal://)")
    hit = _ICS_CACHE.get(url)
    ttl = env_int("CALENDAR_CACHE_SECONDS", 300)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-calendar"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(8_000_000)
    _ICS_CACHE[url] = (time.time(), data)
    return data


def _as_local(v) -> tuple[datetime, bool]:
    """(naive local datetime, is_all_day) from an ICS date or datetime."""
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone().replace(tzinfo=None)
        return v, False
    return datetime.combine(v, datetime.min.time()), True


def feed_events(start: datetime, end: datetime, ics: bytes | None = None) -> list[dict]:
    """Events from the ICS feed between start and end (naive local times). [] if no feed is set up."""
    if ics is None:
        url = env("CALENDAR_ICS_URL")
        if not url:
            return []
        ics = _download(url)
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in recurring_ical_events.of(cal).between(start.astimezone(), end.astimezone()):
        s, all_day = _as_local(ev.decoded("DTSTART"))
        try:
            e, _ = _as_local(ev.decoded("DTEND"))
        except KeyError:
            e = s + (timedelta(days=1) if all_day else timedelta(hours=1))
        out.append({"title": str(ev.get("SUMMARY", "(no title)")), "start": s, "end": e, "all_day": all_day,
                    "where": str(ev.get("LOCATION", "")), "source": "calendar"})
    return out


# ------------------------------------------------------------------ helpers
def window(text: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """'today' / 'tomorrow' / 'week' / a date or weekday -> (start, end, label)."""
    now = now or datetime.now()
    t = (text or "today").strip().lower()
    midnight = datetime.combine(now.date(), datetime.min.time())
    if t in ("", "today", "tonight", "now"):
        return midnight, midnight + timedelta(days=1), "today"
    if t == "tomorrow":
        return midnight + timedelta(days=1), midnight + timedelta(days=2), "tomorrow"
    if t in ("week", "this week", "the week", "next 7 days", "7 days"):
        return midnight, midnight + timedelta(days=7), "the next 7 days"
    d = timeparse.parse_when(t, now)
    if d:
        day = datetime.combine(d.date(), datetime.min.time())
        return day, day + timedelta(days=1), f"{day:%A %B} {day.day}"
    return midnight, midnight + timedelta(days=1), "today"


def _fmt(e: dict) -> str:
    if e["all_day"]:
        when = "all day"
    else:
        when = f"{e['start']:%I:%M %p}".lstrip("0") + "-" + f"{e['end']:%I:%M %p}".lstrip("0")
    where = f" at {e['where']}" if e.get("where") else ""
    return f"{when}: {e['title']}{where}"


def events_between(start: datetime, end: datetime, base: Path | None = None) -> list[dict]:
    rows = []
    for r in _load(base):
        s = datetime.fromisoformat(r["start"])
        e = s + timedelta(minutes=int(r.get("minutes", 60)))
        if s < end and e > start:
            rows.append({"title": r["title"], "start": s, "end": e, "all_day": False, "where": r.get("notes", ""),
                         "source": "jarvis", "id": r["id"]})
    try:
        rows += feed_events(start, end)
    except Exception as ex:  # a broken feed must never break the rest
        rows.append({"title": f"(could not read your calendar feed: {type(ex).__name__}: {str(ex)[:80]})",
                     "start": start, "end": start, "all_day": True, "where": "", "source": "error"})
    return sorted(rows, key=lambda e: (not e["all_day"], e["start"]))


def summary(text: str = "today", base: Path | None = None, now: datetime | None = None) -> str:
    start, end, label = window(text, now)
    evs = events_between(start, end, base)
    if not evs:
        return f"Nothing on the calendar for {label}."
    lines = [f"- {(e['start'].strftime('%a ') if label.startswith('the next') else '')}{_fmt(e)}" for e in evs]
    return f"Calendar for {label} ({len(evs)}):\n" + "\n".join(lines)


# ------------------------------------------------------------------ alerts
def schedule_reminders(ctx) -> int:
    """(Re)create the 'coming up' alerts for my own events in the next 14 days. Called at startup and after changes."""
    from . import tools as T

    n, now = 0, datetime.now()
    for r in _load(_base(ctx)):
        T.cancel_timer(ctx, f"ev-{r['id']}")
        start = datetime.fromisoformat(r["start"])
        lead = int(r.get("remind_minutes", 15))
        fire = start - timedelta(minutes=lead)
        if fire <= now or fire > now + timedelta(days=14):
            continue
        msg = f"Coming up in {lead} minutes: {r['title']}" + (f" ({r['notes']})" if r.get("notes") else "")
        T._schedule(ctx, (fire - now).total_seconds(), msg, timer_id=f"ev-{r['id']}", transient=True)
        n += 1
    return n


# ------------------------------------------------------------------ tools
@tool("add_event", "Put an event on the calendar. 'when' is the start, like 2026-11-20 15:30 (use the clock on the "
      "message to work out 'tomorrow at 3pm'). Jarvis alerts the owner before it starts.",
      {"type": "object", "properties": {"title": {"type": "string"}, "when": {"type": "string"},
                                        "minutes": {"type": "integer", "description": "length, default 60"},
                                        "notes": {"type": "string"},
                                        "remind_minutes": {"type": "integer", "description": "alert this long before, "
                                                                                              "default 15"}},
       "required": ["title", "when"]})
def add_event(ctx, title: str, when: str, minutes: int = 60, notes: str = "", remind_minutes: int = 15):
    start = timeparse.parse_when(when)
    if start is None:
        return "I couldn't understand the date/time. Give it like 2026-11-20 15:30."
    if start < datetime.now() - timedelta(minutes=5):
        return f"That time ({start:%A %B} {start.day}, {start:%I:%M %p}) is already in the past. Which day did you mean?"
    title = (title or "").strip()[:120]
    if not title:
        return "What should the event be called?"
    with _LOCK:
        rows = _load(_base(ctx))
        rid = f"e{int(time.time() * 1000) % 10**10}"
        rows.append({"id": rid, "title": title, "start": start.isoformat(timespec="minutes"),
                     "minutes": max(5, min(int(minutes or 60), 24 * 60)), "notes": (notes or "")[:200],
                     "remind_minutes": max(0, min(int(remind_minutes or 0), 1440))})
        _save(rows, _base(ctx))
    schedule_reminders(ctx)
    clash = [e for e in events_between(start, start + timedelta(minutes=max(5, int(minutes or 60))), _base(ctx))
             if e.get("id") != rid and not e["all_day"] and e["source"] != "error"]
    warn = f" Heads up, that overlaps with {clash[0]['title']} ({_fmt(clash[0]).split(':')[0]})." if clash else ""
    return f"Added '{title}' on {start:%A %B} {start.day} at {start:%I:%M %p}.{warn}"


@tool("list_events", "Read the calendar. range: today, tomorrow, week, or a date/weekday. Use it for 'what's my day', "
      "'what's on tomorrow', 'am I free Friday at 3'.",
      {"type": "object", "properties": {"range": {"type": "string"}}})
def list_events(ctx, range: str = "today"):  # noqa: A002 - 'range' is the argument name the model sees
    return summary(range, _base(ctx))


@tool("cancel_event", "Remove one of Jarvis's own calendar events by (part of) its title. Asks first.",
      {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
      dangerous=True, risk="delete")
def cancel_event(ctx, title: str):
    with _LOCK:
        rows = _load(_base(ctx))
        hits = [r for r in rows if title.lower().strip() in r["title"].lower()]
        if not hits:
            return f"I have no event of mine matching '{title}'. (Events from your Google/Outlook calendar can't be changed here.)"
        if len(hits) > 1:
            return "More than one matches: " + "; ".join(f"{h['title']} ({h['start'][:16]})" for h in hits) + ". Be more specific."
        _save([r for r in rows if r is not hits[0]], _base(ctx))
    from . import tools as T

    T.cancel_timer(ctx, f"ev-{hits[0]['id']}")
    return f"Removed '{hits[0]['title']}'."
