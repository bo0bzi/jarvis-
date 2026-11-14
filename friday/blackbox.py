"""The black box: a full record of what Jarvis does, with passwords and keys blanked out before anything is saved.

Why: a stronger online AI studies this record every night (distill.py) and teaches the local AI how it did each job.
What is recorded, per request: what you asked, which brain worked on it, EVERY tool the brain ran with its exact
arguments (that includes each key press, click, typed text, shell command and app it opened), the result, the window
that was in front when it acted, and the answer. Only what the AI itself did is recorded. Your own keystrokes are
never recorded.
Files: data/blackbox/YYYY-MM-DD.jsonl, kept BLACKBOX_KEEP_DAYS (60). Switch off with BLACKBOX_ENABLED=false."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .env import data_dir, env_bool, env_int

_LOCK = threading.Lock()


class _PerThread(threading.local):
    """A dict-like value that every thread (the chat, each background project) has its own copy of."""

    def __init__(self, **start):
        self.d = dict(start)

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v


_CUR = _PerThread(turn="")          # the request this thread is working on
_LAST = {"key": None, "t": 0.0}
_USED = _PerThread(turn="", seen=None)
_QUIET = threading.local()  # set while the AI is being TESTED at night: those prompts and calls are not real use
_PRUNED = {"day": ""}
RESULT_CHARS = 1500
ARG_CHARS = 1500
# tools whose "which window was in front" matters (the same keys do different things in different windows)
WINDOW_TOOLS = {"press_keys", "keyboard_type", "mouse_click", "open_app", "close_app", "take_screenshot",
                "look_at_screen", "read_screen_text", "run_shell_command"}

_PAIR = re.compile(r"(?i)\b(password|passwd|pwd|passcode|secret|api[_ -]?key|token|bearer|authorization)\b"
                   r"(\s*[:=]\s*|\s+is\s+)[\"']?[^\s\"',;]{3,}")
_KEYS = re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abpr]|AKIA)[-_]?[A-Za-z0-9_\-]{16,}\b|\bfreellmapi-[A-Za-z0-9]{12,}\b|"
                   r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b")
_LONG = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{28,}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SECRET_ARG = re.compile(r"(?i)pass|secret|token|api.?key|auth|credential|\bpin\b")


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
        flip = not flip
    return total % 10 == 0


def redact(text) -> str:
    """Blank out anything that looks like a password, API key, token, card number or SSN."""
    s = str(text)
    s = _PAIR.sub(lambda m: f"{m.group(1)} [hidden]", s)
    s = _KEYS.sub("[key hidden]", s)
    s = _LONG.sub("[token hidden]", s)
    s = _SSN.sub("[id hidden]", s)

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        return "[card hidden]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    return _CARD.sub(card, s)


def secret_like(text: str) -> bool:
    """One word with 8+ characters mixing three of: lower, upper, digit, symbol (and not a web address / path)."""
    t = str(text).strip()
    if len(t) < 8 or len(t) > 80 or re.search(r"\s", t) or re.match(r"(?i)(https?://|www\.)", t) or "/" in t or "\\" in t:
        return False
    classes = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3


def redact_args(tool: str, args) -> dict:
    if not isinstance(args, dict):
        return {"raw": redact(str(args))[:ARG_CHARS]}
    out = {}
    for k, v in args.items():
        if _SECRET_ARG.search(str(k)):
            out[k] = "[hidden]"
        elif tool == "keyboard_type" and isinstance(v, str) and secret_like(v):
            out[k] = f"[typed a secret-like word, {len(v)} characters]"
        elif isinstance(v, str):
            out[k] = redact(v)[:ARG_CHARS]
        else:
            out[k] = v if isinstance(v, (int, float, bool, type(None))) else redact(json.dumps(v, default=str))[:ARG_CHARS]
    return out


def enabled() -> bool:
    return env_bool("BLACKBOX_ENABLED", True) and not getattr(_QUIET, "on", False)


@contextmanager
def quiet():
    """In this thread nothing is recorded (night-time tests of the local AI must not look like real requests)."""
    before = getattr(_QUIET, "on", False)
    _QUIET.on = True
    try:
        yield
    finally:
        _QUIET.on = before


def _dir() -> Path:
    p = data_dir() / "blackbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _prune() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if _PRUNED["day"] == today:
        return
    _PRUNED["day"] = today
    cutoff = (datetime.now() - timedelta(days=max(1, env_int("BLACKBOX_KEEP_DAYS", 60)))).strftime("%Y-%m-%d")
    for f in _dir().glob("*.jsonl"):
        if f.stem < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


def _write(ev: dict) -> None:
    ev = {"t": datetime.now().isoformat(timespec="seconds"), **ev}
    try:
        with _LOCK:
            _prune()
            with open(_dir() / f"{ev['t'][:10]}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def foreground_title() -> str:
    """The title of the window that is in front right now (Windows only; '' anywhere else or on any problem)."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes

        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return redact(buf.value)[:160]
    except Exception:
        return ""


# ------------------------------------------------------------------ recording (called by the Brain; never raises)
def begin_turn(text: str, brain: str = "") -> str:
    if not enabled():
        return ""
    tid = f"{datetime.now():%H%M%S}-{uuid.uuid4().hex[:4]}"
    _CUR["turn"] = tid
    _write({"k": "turn", "turn": tid, "text": redact(text)[:800], "brain": brain})
    return tid


def record_action(tool: str, args, result: str, secs: float, brain: str = "", cloud: bool = False) -> None:
    if not enabled() or not _CUR["turn"]:
        return
    res = redact(result)
    bad = res.startswith(("Error", "Not allowed", "Cancelled")) or "did not confirm" in res[:120]
    ev = {"k": "act", "turn": _CUR["turn"], "tool": tool, "args": redact_args(tool, args), "result": res[:RESULT_CHARS],
          "ok": not bad, "secs": round(secs, 2), "brain": brain, "cloud": bool(cloud)}
    if tool in WINDOW_TOOLS:
        ev["window"] = foreground_title()
    _write(ev)


def note(kind: str, text: str, brain: str = "", cloud: bool = False) -> None:
    """escalate / guard / reply / thought / interrupt. The same note arriving twice in a row (a helper brain reports to
    the main brain, which reports again) is saved once, under the helper's name."""
    if not enabled() or not _CUR["turn"] or kind not in {"escalate", "guard", "reply", "thought", "interrupt"}:
        return
    key = (_CUR["turn"], kind, text[:200])  # per request: the same words in a later request are a new note
    if _LAST["key"] == key and time.time() - _LAST["t"] < 2:
        return
    _LAST["key"], _LAST["t"] = key, time.time()
    _write({"k": kind, "turn": _CUR["turn"], "text": redact(text)[:900], "brain": brain, "cloud": bool(cloud)})


def mark_used(kind: str, ids: list) -> None:
    """A playbook ("playbook") or a conditional rule ("lesson") was shown to the AI for the request being worked on.
    Saved once per request, so the night-time check can ask: did the jobs where it was shown go well?"""
    if not enabled() or not _CUR["turn"] or not ids:
        return
    if _USED["turn"] != _CUR["turn"]:
        _USED["turn"], _USED["seen"] = _CUR["turn"], set()
    new = [i for i in ids if (kind, i) not in _USED["seen"]]
    if new:
        _USED["seen"].update((kind, i) for i in new)
        _write({"k": "used", "turn": _CUR["turn"], "kind": kind, "ids": new})


# ------------------------------------------------------------------ reading
def _events(hours: float) -> list[dict]:
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    out: list[dict] = []
    d = _dir()
    day = datetime.now().date()
    for back in range(int(hours // 24) + 2):
        f = d / f"{day - timedelta(days=back):%Y-%m-%d}.jsonl"
        if not f.exists():
            continue
        try:
            rows = []
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            out = rows + out
        except OSError:
            continue
    return [e for e in out if e.get("t", "") >= since]


def episodes(hours: float = 24) -> list[dict]:
    """One dict per request: {id, text, t, steps[...], replies[...], flags[...], brains[...], final_brain, cloud_final}."""
    from .evolve import _CORRECTION

    eps: dict[str, dict] = {}
    order: list[str] = []
    for e in _events(hours):
        tid = e.get("turn")
        if not tid:
            continue
        if e["k"] == "turn":
            eps[tid] = {"id": tid, "text": e.get("text", ""), "t": e.get("t", ""), "steps": [], "replies": [], "flags": [],
                        "brains": [e.get("brain", "")], "final_brain": "", "cloud_final": False,
                        "used": {"playbook": [], "lesson": []}}
            order.append(tid)
            continue
        ep = eps.get(tid)
        if ep is None:
            continue
        if e["k"] == "used":
            ep["used"].setdefault(e.get("kind", ""), []).extend(i for i in e.get("ids", []) if i not in ep["used"].get(e.get("kind", ""), []))
        elif e["k"] == "act":
            ep["steps"].append({k: e.get(k) for k in ("tool", "args", "result", "ok", "secs", "brain", "cloud", "window")})
        elif e["k"] == "reply":
            ep["replies"].append(e.get("text", ""))
            ep["final_brain"], ep["cloud_final"] = e.get("brain", ""), bool(e.get("cloud"))
        elif e["k"] == "guard":
            ep["flags"].append("a claim with no action behind it was caught")
        elif e["k"] == "escalate":
            ep["flags"].append("the local AI could not answer: " + e.get("text", "")[:90])
        elif e["k"] == "interrupt":
            ep["flags"].append("the user talked over it")
        if e.get("brain") and e["brain"] not in ep["brains"]:
            ep["brains"].append(e["brain"])
    result = [eps[i] for i in order]
    for prev, nxt in zip(result, result[1:]):  # the next message complains about the previous answer
        if _CORRECTION.search(nxt["text"]):
            prev["flags"].append(f"the user reacted badly: \"{nxt['text'][:90]}\"")
    for ep in result:
        if any(not s["ok"] for s in ep["steps"]):
            ep["flags"].append("a tool step failed")
    return result


def stats(hours: float = 24) -> dict:
    eps = episodes(hours)
    return {"requests": len(eps), "actions": sum(len(e["steps"]) for e in eps),
            "online": sum(1 for e in eps if e["cloud_final"] and e["steps"]), "problems": sum(1 for e in eps if e["flags"])}
