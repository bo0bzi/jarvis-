"""Persistent conversation memory + long-term facts, stored as JSON in data/."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path


class Memory:
    def __init__(self, data_dir: Path, max_messages: int = 40):
        self.path = Path(data_dir) / "memory.json"
        self.max_messages = max(2, max_messages)
        self._lock = threading.Lock()
        self.messages: list[dict] = []
        self.facts: list[str] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.messages = [m for m in data.get("messages", []) if m.get("role") in ("user", "assistant")]
            self.facts = list(data.get("facts", []))
        except (json.JSONDecodeError, OSError):
            # Corrupt file: keep a backup and start fresh instead of crashing.
            try:
                self.path.rename(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            self.messages, self.facts = [], []

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        """Write the file. Caller must already hold self._lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"messages": self.messages, "facts": self.facts}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, role: str, content: str) -> None:
        # Hold the lock across the whole mutation: append/slice/pop used to run unlocked while save() locked,
        # so two turns arriving together could interleave and drop one.
        with self._lock:
            self.messages.append({"role": role, "content": content,
                                  "time": datetime.now().isoformat(timespec="seconds")})
            self.messages = self.messages[-self.max_messages:]
            # Never start history with an assistant turn (Anthropic requires user first).
            while self.messages and self.messages[0]["role"] != "user":
                self.messages.pop(0)
            self._save_unlocked()
        try:  # also make it searchable (data/friday.db)
            from .memory_db import index

            index("chat", f"{role}: {content}", base=self.path.parent)
        except Exception:
            pass

    # Replies where a small model invented things (asked for Twilio, claimed "configured"/"sent" with no tool).
    # Showing those back to it makes it repeat the mistake, so they (and the question they answered) are left out.
    _POISON = re.compile(r"twilio|account sid|auth(entication)? token|sms gateway|quo mcp (skill|server) (has|is)|"
                         r"configured successfully|(has|have) been sent to your|should receive the (message|text)",
                         re.I)

    def history(self) -> list[dict]:
        out: list[dict] = []
        for m in self.messages:
            if m["role"] == "assistant" and self._POISON.search(m["content"] or ""):
                if out and out[-1]["role"] == "user":
                    out.pop()  # drop the question it answered so roles still alternate
                continue
            out.append({"role": m["role"], "content": m["content"]})
        return out

    def remember(self, fact: str) -> None:
        fact = fact.strip()
        if fact and fact not in self.facts:
            self.facts.append(fact)
            self.save()
            try:
                from .memory_db import index

                index("fact", fact, base=self.path.parent)
            except Exception:
                pass

    def forget(self, text: str) -> int:
        before = len(self.facts)
        self.facts = [f for f in self.facts if text.lower() not in f.lower()]
        self.save()
        return before - len(self.facts)

    def clear(self) -> None:
        self.messages = []
        self.save()
