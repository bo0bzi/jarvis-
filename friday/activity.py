"""Activity log: everything Friday did on its own, installed, approved or spent (data/activity.jsonl)."""
from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timedelta

from .env import data_dir
from .tools import tool

_lock = threading.Lock()
_recent: deque = deque(maxlen=500)
LISTENERS: list = []  # callables(entry) - the assistant forwards entries to the dashboard


def _path():
    return data_dir() / "activity.jsonl"


def log(kind: str, message: str, **extra) -> dict:
    """kind: tool, credits, approval, skill, sms, nightly, backup, auto, error..."""
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "message": message[:500], **extra}
    with _lock:
        _recent.append(entry)
        try:
            with _path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass
    for fn in list(LISTENERS):
        try:
            fn(entry)
        except Exception:
            pass
    return entry


def recent(limit: int = 100, kind: str = "", hours: float = 0) -> list[dict]:
    rows: list[dict] = []
    try:
        with _path().open(encoding="utf-8") as f:
            lines = deque(f, maxlen=5000)
        rows = [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        rows = list(_recent)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if hours:
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = [r for r in rows if r.get("time", "") >= since]
    return rows[-limit:]


@tool("what_did_you_do", "Summarise what Friday did recently on its own: tools used, skills installed, credits spent, "
      "texts, nightly work. Use when the user asks what you did / what happened.",
      {"type": "object", "properties": {"hours": {"type": "number", "description": "Look back this many hours (default 24)"}}})
def what_did_you_do(ctx, hours: float = 24) -> str:
    rows = recent(60, hours=hours or 24)
    if not rows:
        return "Nothing logged in that time."
    return "\n".join(f"{r['time'][11:16]} [{r['kind']}] {r['message']}" for r in rows[-40:])
