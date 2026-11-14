"""Token usage tracking (counted from the AI service's own responses), persisted per day."""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path


def extract_tokens(resp) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from an OpenAI- or Anthropic-style response (0, 0 if absent)."""
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0
    if isinstance(u, dict):
        get = u.get
    else:
        def get(k, d=None):
            return getattr(u, k, d)
    inp = get("prompt_tokens") or get("input_tokens") or 0
    out = get("completion_tokens") or get("output_tokens") or 0
    try:
        return int(inp), int(out)
    except (TypeError, ValueError):
        return 0, 0


class UsageTracker:
    def __init__(self, data_dir: Path | None):
        self.path = Path(data_dir) / "usage.json" if data_dir else None
        self._lock = threading.Lock()
        self.session = {"requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "last_latency_ms": None}
        self.days: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self.days = json.loads(self.path.read_text(encoding="utf-8")).get("days", {})
            except (OSError, json.JSONDecodeError):
                self.days = {}

    def record(self, resp=None, latency_ms: float | None = None, error: bool = False) -> None:
        inp, out = extract_tokens(resp) if resp is not None else (0, 0)
        today = date.today().isoformat()
        with self._lock:
            d = self.days.setdefault(today, {"requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0})
            for bucket in (self.session, d):
                bucket["requests"] += 1
                bucket["errors"] += int(error)
                bucket["input_tokens"] += inp
                bucket["output_tokens"] += out
            if latency_ms is not None:
                self.session["last_latency_ms"] = round(latency_ms)
            # keep 60 days
            for k in sorted(self.days)[:-60]:
                del self.days[k]
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"days": self.days}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def summary(self) -> dict:
        today = date.today().isoformat()
        with self._lock:
            month = date.today().strftime("%Y-%m")
            m = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
            for k, v in self.days.items():
                if k.startswith(month):
                    for f in m:
                        m[f] += v.get(f, 0)
            return {"session": dict(self.session),
                    "today": dict(self.days.get(today, {"requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0})),
                    "month": m,
                    "history": [{"date": k, **v} for k, v in sorted(self.days.items())[-14:]]}
