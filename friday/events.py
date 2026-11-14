"""Thread-safe event bus + live state shared between the assistant and the dashboard."""
from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque
from datetime import datetime

AGENTS = {
    "voice_input": "Microphone / voice input",
    "stt": "Speech-to-text",
    "listener": "Always-listening / wake word",
    "brain": "AI brain (LLM)",
    "free_router": "Free cloud AI (OmniRoute)",
    "tts": "Voice output (TTS)",
    "tools": "PC tools",
    "scheduler": "Timers & reminders",
    "memory": "Memory",
    "dashboard": "Dashboard",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class EventBus:
    def __init__(self, log_size: int = 400, feed_size: int = 200):
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._ids = itertools.count(1)
        self.log: deque = deque(maxlen=log_size)
        self.feed: deque = deque(maxlen=feed_size)
        self.traces: deque = deque(maxlen=600)
        self.on_trace_entry = None  # optional callback (console printing / saving to disk)
        self.status = {"state": "starting", "detail": "", "since": _now()}
        self.flags = {"mic_muted": False, "voice_muted": False, "conversation_until": 0.0}
        self.level = 0.0
        self.pending_confirm: dict | None = None
        self.agents = {k: {"id": k, "name": v, "health": "unknown", "detail": "", "last_activity": "", "error": ""}
                       for k, v in AGENTS.items()}
        self._last_level_push = 0.0

    # ------------------------------------------------------------ pub/sub
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data: dict) -> None:
        event = {"id": next(self._ids), "type": kind, "time": _now(), **data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:  # slow client: drop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    # ------------------------------------------------------------ helpers used by the assistant
    def set_status(self, state: str, detail: str = "") -> None:
        self.status = {"state": state, "detail": detail, "since": _now()}
        self.publish("status", self.status)
        if state == "error" and detail:
            self.log_event("status", detail, level="error")

    def log_event(self, source: str, message: str, level: str = "info") -> None:
        entry = {"time": _now(), "source": source, "message": message, "level": level}
        self.log.append(entry)
        self.publish("log", entry)

    def chat(self, role: str, text: str, via: str = "") -> None:
        entry = {"time": _now(), "role": role, "text": text, "via": via}
        self.feed.append(entry)
        self.publish("chat", entry)

    def trace(self, kind: str, text: str, detail: str = "", turn: int = 0) -> None:
        """One step of how Jarvis is thinking (heard, lessons, plan, tool, result, reply...). Shown live in the
        dashboard 'Thinking' tab and the console, and saved to data/trace.jsonl."""
        entry = {"time": _now(), "kind": kind, "text": text, "detail": detail, "turn": turn}
        self.traces.append(entry)
        self.publish("trace", entry)
        if self.on_trace_entry is not None:
            try:
                self.on_trace_entry(entry)
            except Exception:
                pass

    def agent(self, key: str, health: str | None = None, detail: str | None = None, error: str | None = None,
              activity: bool = True) -> None:
        a = self.agents.setdefault(key, {"id": key, "name": key, "health": "unknown", "detail": "",
                                         "last_activity": "", "error": ""})
        before = dict(a)
        if health is not None:
            a["health"] = health
        if detail is not None:
            a["detail"] = detail
        if error is not None:
            if error and error != a["error"]:  # log each new error once, not on every refresh
                self.log_event(key, error, level="error")
            a["error"] = error
        if activity:
            a["last_activity"] = _now()
        if a != before:
            self.publish("agent", dict(a))

    def set_level(self, rms: float) -> None:
        self.level = rms
        t = time.time()
        if t - self._last_level_push > 0.1:  # ~10 updates/s max
            self._last_level_push = t
            self.publish("level", {"rms": round(rms, 5)})

    def set_flag(self, name: str, value) -> None:
        self.flags[name] = value
        self.publish("flags", dict(self.flags))

    def snapshot(self) -> dict:
        return {"status": self.status, "flags": self.flags, "agents": list(self.agents.values()),
                "log": list(self.log)[-150:], "feed": list(self.feed)[-100:], "trace": list(self.traces)[-300:],
                "pending_confirm": self.pending_confirm}


class NullBus(EventBus):
    """Used when the dashboard is off; still keeps state (cheap)."""
