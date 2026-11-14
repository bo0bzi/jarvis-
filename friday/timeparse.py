"""Understand "set a timer for 5 minutes", "set an alarm for 7:30 am", "remind me to call John in 10 minutes /
at 5pm / tomorrow at 9" WITHOUT the AI, so they always really get set (a small model used to just say "alarm set"
and set nothing)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "sixty": 60, "half": 0.5}
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DUR = re.compile(r"\b(?:in |for |after )?(?P<n>\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
                  r"fifteen|twenty|thirty|forty|sixty|half(?: an?)?)\s*(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?|"
                  r"days?)\b(?: and a half)?", re.I)
_CLOCK = re.compile(r"\b(?:at|for) (?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?(?![\d:])", re.I)
_CMD = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),?\s*)?(?:please |can you |could you )?"
                  r"(?:(?:set|start|make|create|put)\b.*\b(?:timer|alarm|reminder)\b|wake me(?: up)?\b|remind me\b)", re.I)


def _num(s: str) -> float:
    s = s.lower().replace("half a", "half").replace("half an", "half")
    return float(_WORDS[s]) if s in _WORDS else float(s)


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?")
_AT = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)|\b(?:at\s+)?(\d{1,2}):(\d{2})\b", re.I)


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """A date and time from '2026-11-20 15:30', 'tomorrow at 3pm', 'friday 10:30', 'monday at 9 am'. None if unclear.
    A day with no time means 09:00."""
    t = (text or "").strip().lower()
    now = now or datetime.now()
    m = _ISO.search(t)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 9), int(m[5] or 0))
        except ValueError:
            return None
    day = None
    if "tomorrow" in t:
        day = now.date() + timedelta(days=1)
    elif "today" in t or "tonight" in t:
        day = now.date()
    else:
        for i, name in enumerate(_DAYS):
            if re.search(rf"\b{name}\b|\b{name[:3]}\b", t):
                ahead = (i - now.weekday()) % 7 or 7
                day = now.date() + timedelta(days=ahead)
                break
    h, mi = 9, 0
    a = _AT.search(t)
    if a:
        if a.group(1):
            h, mi, ap = int(a.group(1)), int(a.group(2) or 0), a.group(3).lower().replace(".", "")
            h = h % 12 + (12 if ap.startswith("p") else 0)
        else:
            h, mi = int(a.group(4)), int(a.group(5))
        if h > 23 or mi > 59:
            return None
    elif day is None:
        return None
    due = datetime.combine(day or now.date(), datetime.min.time()).replace(hour=h, minute=mi)
    if day is None and due <= now:  # "at 3pm" said after 3pm means tomorrow
        due += timedelta(days=1)
    return due


def parse(text: str, now: datetime | None = None) -> dict | None:
    """{'kind': 'timer'|'reminder', 'seconds': float, 'message': str, 'label': str} or None if it isn't a clear request."""
    t = (text or "").strip()
    if not _CMD.search(t):
        return None
    now = now or datetime.now()
    low = t.lower()
    seconds = None
    span = None
    m = _DUR.search(t)
    if m:
        seconds = _num(m.group("n")) * _UNIT[m.group("u").lower()[0]]
        span = m.span()
    else:
        c = _CLOCK.search(t)
        if c:
            h, mi, ap = int(c.group("h")), int(c.group("m") or 0), (c.group("ap") or "").lower().replace(".", "")
            if ap.startswith("p") and h < 12:
                h += 12
            if ap.startswith("a") and h == 12:
                h = 0
            if h > 23 or mi > 59:
                return None
            due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if "tomorrow" in low:
                due += timedelta(days=1)
            elif due <= now:
                if not ap and h <= 12 and due + timedelta(hours=12) > now:  # "at 5" said at 3pm means 5pm today
                    due += timedelta(hours=12)
                else:
                    due += timedelta(days=1)
            seconds = (due - now).total_seconds()
            span = c.span()
    if seconds is None or seconds <= 0:
        return None
    is_reminder = bool(re.search(r"\bremind me\b", low))
    msg = ""
    if is_reminder:
        body = t[:span[0]] + " " + t[span[1]:]
        body = re.sub(r"^(?:(?:hey )?(?:jarvis|friday),?\s*)?(?:please |can you |could you )?remind me\s*(?:to |that |about )?",
                      "", body.strip(), flags=re.I)
        msg = re.sub(r"\s+", " ", re.sub(r"\b(?:tomorrow|today|tonight|in|at|on)\s*$", "", body.strip(" ,.!?"), flags=re.I)).strip(" ,.!?")
        msg = re.sub(r"\btomorrow\b", "", msg, flags=re.I).strip(" ,.!?")
    word = "alarm" if re.search(r"\b(alarm|wake me)\b", low) else "timer"
    if not is_reminder:
        return {"kind": "timer", "seconds": seconds, "message": "", "label": word}
    return {"kind": "reminder", "seconds": seconds, "message": msg or "your reminder", "label": "reminder"}
