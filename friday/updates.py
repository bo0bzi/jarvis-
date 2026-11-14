"""Updates without being asked: Jarvis tells you things, you do not have to ask.

What he tells you, by himself:
  * a credit warning each time Abacus use passes another 1,000 credits (there is no limit, only the warning),
  * a problem the moment it lasts (the local AI stopped answering, the microphone was lost, the brain keeps erroring),
    and when it is fixed again. If the local AI (Ollama) is down he first tries to start it,
  * a check-in every UPDATES_EVERY_HOURS (default 4) when something happened: requests, who answered, credits, problems,
    what he learned; nothing to say = he says nothing,
  * overnight results (nightly run, full review, self-changes).
How: every update is written to data/updates.jsonl (dashboard and phone show the list), pushed to your phone, and spoken
out loud ONLY when it is safe to: not in the quiet hours (UPDATES_QUIET_HOURS, default 22:00-07:00), not while you are
talking or in a conversation with him, and not while he is speaking. Otherwise it waits (up to 30 minutes) or goes to
the phone only. Settings: UPDATES_ENABLED, UPDATES_SPEAK, UPDATES_QUIET_HOURS, UPDATES_EVERY_HOURS."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import activity
from .env import data_dir, env, env_bool, env_int
from .tools import tool

_LOCK = threading.Lock()
_PENDING: list[tuple[float, str]] = []   # (queued at, text): things to say out loud when it is a good moment
_STATE = {"health_at": 0.0, "ollama_at": 0.0, "fails": {}, "bad": set()}
HEALTH_EVERY = 60.0
PENDING_MAX_AGE = 30 * 60


def enabled() -> bool:
    return env_bool("UPDATES_ENABLED", True)


def _log_path() -> Path:
    return data_dir() / "updates.jsonl"


def _state_path() -> Path:
    return data_dir() / "updates_state.json"


def recent(n: int = 10) -> list[dict]:
    try:
        rows = [json.loads(x) for x in _log_path().read_text(encoding="utf-8").splitlines()[-200:] if x.strip()]
    except (OSError, ValueError):
        return []
    return rows[-n:][::-1]


def quiet_now(now: datetime | None = None) -> bool:
    """Inside UPDATES_QUIET_HOURS ('22:00-07:00', may wrap past midnight): nothing is spoken."""
    now = now or datetime.now()
    spec = env("UPDATES_QUIET_HOURS", "22:00-07:00").strip()
    try:
        a, b = (x.strip() for x in spec.split("-"))
        start = int(a[:2]) * 60 + int(a[3:5])
        end = int(b[:2]) * 60 + int(b[3:5])
    except (ValueError, IndexError):
        return False
    cur = now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def send(kind: str, text: str, title: str = "Jarvis", speak: bool = True, important: bool = False) -> dict | None:
    """Record an update, push it to the phone, and queue it to be spoken at a good moment. Never raises."""
    if not enabled() or not text:
        return None
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text[:600]}
    try:
        with _LOCK, open(_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    activity.log("update", f"{kind}: {text[:200]}")
    try:
        from . import notify

        notify.push(title, text[:300], text_too=True if important else False)
    except Exception:
        pass
    if speak and env_bool("UPDATES_SPEAK", True) and not quiet_now():
        with _LOCK:
            _PENDING.append((time.time(), text))
            del _PENDING[:-3]  # never a pile of old news
    return entry


def can_speak_now(a) -> bool:
    """A good moment: idle, nobody talking, no conversation going, not already speaking, voice and mic available."""
    try:
        if a.tts.muted or a.tts.busy() or a.recorder is None and a.mode in ("always", "wake"):
            return False
        if a.bus.status.get("state") not in ("sleeping", "idle", "conversation") or a.bus.pending_confirm is not None:
            return False
        if a.bus.flags.get("mic_muted") or time.time() < float(a.bus.flags.get("conversation_until", 0) or 0):
            return False
        return float(a.bus.level) < max(0.02, float(getattr(a.recorder, "threshold", 0.02) or 0.02))
    except Exception:
        return False


def deliver_pending(a) -> bool:
    with _LOCK:
        _PENDING[:] = [p for p in _PENDING if time.time() - p[0] < PENDING_MAX_AGE]
        item = _PENDING[0] if _PENDING else None
    if item is None or quiet_now() or not can_speak_now(a):
        return False
    with _LOCK:
        if _PENDING and _PENDING[0] is item:
            _PENDING.pop(0)
    a._announce(item[1])
    return True


# ------------------------------------------------------------------ what happened (used by the check-in and by "give me an update")
def build_update(hours: float = 4.0) -> str:
    """A short plain-language summary of the last `hours`, or '' when nothing happened."""
    from . import blackbox, credits

    parts = []
    try:
        st = blackbox.stats(hours)
        if st["requests"]:
            line = f"{st['requests']} request(s), {st['actions']} action(s)"
            if st["online"]:
                line += f", {st['online']} done by Abacus"
            parts.append(line + (f", {st['problems']} with a problem" if st["problems"] else ""))
    except Exception:
        pass
    try:
        g = credits.get_gate().status()
        if g["abacus_today"]:
            parts.append(f"Abacus credits today: {g['abacus_today']:g} ({g['abacus_total']:g} in total, no limit)")
    except Exception:
        pass
    try:
        since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        errs = [r for r in activity.recent(500, kind="error") if r.get("time", "") >= since]
        if errs:
            parts.append(f"{len(errs)} error(s), latest: {str(errs[-1].get('message', ''))[:100]}")
        learned = [r for r in activity.recent(500) if r.get("time", "") >= since and r.get("kind") in ("evolve", "distill")]
        if learned:
            parts.append("learning: " + str(learned[-1].get("message", ""))[:100])
        changed = [r for r in activity.recent(500, kind="selfmod") if r.get("time", "") >= since]
        if changed:
            parts.append("I changed myself: " + str(changed[-1].get("message", ""))[:100])
    except Exception:
        pass
    return "Since my last update: " + "; ".join(parts) + "." if parts else ""


@tool("give_update", "Give the owner an update right now: what happened lately (requests, which AI did them, Abacus credits, "
      "problems, what was learned). Use for 'give me an update', 'catch me up', 'what is going on', 'any news'.",
      {"type": "object", "properties": {"hours": {"type": "number"}}})
def give_update(ctx, hours: float = 12):
    return build_update(max(1.0, min(72.0, float(hours or 12)))) or "Nothing new: all quiet, no problems."


# ------------------------------------------------------------------ health
def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _start_ollama() -> bool:
    """Start the local AI server if it is installed but not running (hidden, detached)."""
    exe = shutil.which("ollama") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe")
    if not Path(exe).exists() and not shutil.which("ollama"):
        return False
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([exe, "serve"], creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def check_health(a) -> list[tuple[str, bool, str]]:
    """[(key, ok, what to say when it is not ok)]. Cheap: runs about once a minute."""
    out = []
    s = a.s
    base = (getattr(s, "base_url", "") or "") if s.provider == "custom" else env("CUSTOM_BASE_URL", "http://localhost:11434/v1")
    if base and ("localhost" in base or "127.0.0.1" in base):
        out.append(("local_ai", _http_ok(base.rstrip("/") + "/models"),
                    "My local AI (Ollama) stopped answering, so I can't take over jobs I have learned until it is back."))
    if a.mode in ("always", "wake"):
        out.append(("microphone", a.recorder is not None, "I lost the microphone. I keep trying to get it back."))
    err = str(getattr(a.brain, "last_error", "") or "")
    out.append(("brain", not err, f"My AI brain is reporting a problem: {err[:140]}"))
    return out


def health_tick(a) -> list[str]:
    """Compare with last time; say something only when a problem lasts (2 checks in a row) and when it is fixed."""
    said = []
    for key, ok, bad_text in check_health(a):
        fails = _STATE["fails"]
        if ok:
            fails[key] = 0
            if key in _STATE["bad"]:
                _STATE["bad"].discard(key)
                said.append(f"Good news: that is fixed ({key.replace('_', ' ')}).")
                send("health", said[-1])
            continue
        fails[key] = fails.get(key, 0) + 1
        if key == "local_ai" and fails[key] >= 1 and time.time() - _STATE["ollama_at"] > 600:
            _STATE["ollama_at"] = time.time()
            if _start_ollama():
                activity.log("update", "The local AI was not running: I started it.")
        if fails[key] == 2 and key not in _STATE["bad"]:
            _STATE["bad"].add(key)
            said.append(bad_text)
            send("health", bad_text, important=True)
    return said


def _last_checkin() -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(_state_path().read_text(encoding="utf-8"))["checkin"])
    except (OSError, ValueError, KeyError):
        return None


def checkin_tick(now: datetime | None = None) -> str:
    """Every UPDATES_EVERY_HOURS (outside the quiet hours): a short check-in, only if something happened."""
    every = env_int("UPDATES_EVERY_HOURS", 4)
    now = now or datetime.now()
    if every <= 0 or quiet_now(now):
        return ""
    last = _last_checkin()
    if last is None:  # first run: start the clock, say nothing yet
        _save_checkin(now)
        return ""
    if now - last < timedelta(hours=every):
        return ""
    text = build_update((now - last).total_seconds() / 3600)
    _save_checkin(now)
    if text:
        send("checkin", text)
    return text


def _save_checkin(now: datetime) -> None:
    try:
        _state_path().write_text(json.dumps({"checkin": now.isoformat(timespec="seconds")}), encoding="utf-8")
    except OSError:
        pass


def tick(a) -> None:
    """Called every few seconds by Friday's background loop."""
    if not enabled():
        return
    try:
        deliver_pending(a)
        if time.time() - _STATE["health_at"] >= HEALTH_EVERY:
            _STATE["health_at"] = time.time()
            health_tick(a)
            checkin_tick()
    except Exception as e:
        activity.log("error", f"updates tick failed: {type(e).__name__}: {e}")
