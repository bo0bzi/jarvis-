"""What the phone page needs from the assistant, beyond plain ask / speak / listen.

  status(a)                  -> one glance: credits, limits, health, switches, timers, open jobs (never any secret)
  control(a, action)         -> the remote: mute, stop talking, conversation mode, brain mode, restart
  ask_stream(a, text, emit)  -> the answer sentence by sentence while it is written, with the steps taken on the way
  open_events(a)             -> live "status changed" / "approval waiting" events, so the phone shows them at once

Every function takes the Assistant and fails soft: a part that can't be read is left out, the rest still shows."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime

CONTROL_ACTIONS = {"mute_mic", "unmute_mic", "mute_voice", "unmute_voice", "conversation_on", "conversation_off",
                   "stop_speaking", "restart_brain", "restart_friday", "mode:always", "mode:wake", "mode:ptt"}
STEP_KINDS = {"tool", "web", "escalate", "thought"}


def route_name(route: str) -> str:
    """'backup (think hard)' -> 'Abacus': the short name of the brain that answered, for the tag under a reply."""
    low = (route or "main").lower()
    if "claude" in low:
        for tier in ("opus", "sonnet", "haiku"):
            if tier in low:
                return f"Claude {tier.title()}"
        return "Claude"
    if low.startswith("backup"):
        return "Abacus"
    if "free" in low:
        return "Free cloud"
    return "Local AI"


def control(a, action: str) -> str:
    action = str(action or "").strip()
    if action.startswith("brain:"):
        from . import brainmode

        return brainmode.DESCRIPTION[brainmode.set_mode(action.split(":", 1)[1])]  # ValueError on an unknown mode
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"unknown action {action[:40]!r}")
    return a.control(action)


def status(a) -> dict:
    from . import brainmode

    out: dict = {"state": dict(a.bus.status), "credits": {}, "limits": [], "health": [], "problems": [], "timers": [],
                 "jobs": [], "hud": {}, "route": route_name(getattr(a.brain, "last_route", "")),
                 "switches": {"mic_muted": bool(a.bus.flags.get("mic_muted")), "voice_muted": bool(a.tts.muted),
                              "conversation_mode": bool(a.s.conversation_mode), "mode": a.mode,
                              "brain_mode": brainmode.get()}}
    try:
        from . import credits
        from .env import env_int

        gate = credits.get_gate()
        st = gate.status()
        today = datetime.now().strftime("%Y-%m-%d")
        out["credits"] = {
            "spent_today": st["spent_today"], "daily_cap": st["daily_cap"], "spent_month": st["spent_month"],
            "monthly_cap": st["monthly_cap"], "allowance": env_int("CREDIT_PREAPPROVED_PER_DAY", 0),
            "abacus_today": st["abacus_today"], "abacus_total": st["abacus_total"], "abacus_warn_every": st["abacus_warn_every"],
            "allowance_used": round(gate.data.get("preapproved_used", {}).get(today, 0), 1),
            "ledger": [{"time": e.get("time", ""), "task": str(e.get("task", ""))[:90], "provider": e.get("provider", ""),
                        "estimate": e.get("estimate", [0, 0]), "approved": bool(e.get("approved")),
                        "how": e.get("how", ""), "actual": e.get("actual", 0)} for e in st["ledger"][-10:]][::-1]}
    except Exception:
        pass
    try:
        from .dashboard import _timers, hud_data, limits_data

        out["limits"] = limits_data(a)["rows"]
        out["hud"] = hud_data(a)
        out["timers"] = _timers(a.ctx)
    except Exception:
        pass
    try:
        out["health"] = [{"name": c.get("name", c.get("id", "")), "status": c.get("status", "unknown"),
                          "summary": str(c.get("summary", ""))[:160]} for c in a.last_checks.values()]
        out["problems"] = [{"name": g["name"], "error": str(g["error"])[:200]} for g in a.bus.agents.values()
                           if g.get("health") == "error" and g.get("error")]
    except Exception:
        pass
    try:
        from . import projects

        out["projects"] = [{**{k: p[k] for k in ("id", "title", "status", "progress", "seconds")}, "result": p["result"][:300]}
                           for p in projects.MANAGER.snapshot(6)]
    except Exception:
        pass
    try:
        from . import updates

        out["updates"] = updates.recent(8)
    except Exception:
        pass
    try:
        from . import shop

        con, _ = shop._db(a.ctx)
        try:
            out["jobs"] = [dict(r) for r in con.execute(
                f"SELECT id, customer, vehicle, work, status FROM jobs WHERE status IN "
                f"({','.join('?' * len(shop.OPEN))}) ORDER BY id LIMIT 30", shop.OPEN).fetchall()]
        finally:
            con.close()
    except Exception:
        pass
    return out


def ask_stream(a, text: str, emit) -> dict:
    """Answer `text`, calling emit({...}) on the way: {"delta": sentence} as the reply is written, {"step": kind,
    "text": ...} for each tool / web / hand-over step, {"ping": 1} while it is quiet. Returns the final
    {"reply", "spoken", "route", "seconds"}; `spoken` is the part already sent as deltas (the page speaks the rest)."""
    t0 = time.time()
    if a.tts.busy():  # asking from the phone cuts the current PC answer short
        a.tts.stop()
    a.ui.user(text, via="phone")
    q = a.bus.subscribe()
    done = threading.Event()

    def safe(obj: dict) -> None:
        try:
            emit(obj)
        except Exception:  # the phone went away: keep working, there is just nobody to tell
            pass

    def pump() -> None:
        quiet = time.time()
        while True:
            try:
                e = q.get(timeout=0.2)
            except queue.Empty:
                if done.is_set():  # the answer is ready and every step before it has been passed on
                    return
                if time.time() - quiet > 5:
                    quiet = time.time()
                    safe({"ping": 1})
                continue
            if e.get("type") == "trace" and e.get("kind") in STEP_KINDS:
                quiet = time.time()
                safe({"step": e["kind"], "text": str(e.get("text", ""))[:140]})

    steps = threading.Thread(target=pump, daemon=True, name="phone-steps")
    steps.start()
    a._tls.via = "api"
    spoken = ""
    try:
        with a._brain_lock:
            a.brain.stream_to(lambda s: emit({"delta": s}))  # a failed send stops early speech only, not the answer
            try:
                reply = a.brain.chat(text)
            finally:
                spoken = a.brain.streamed_prefix or ""
                a.brain.stream_to(None)
    finally:
        a._tls.via = ""
        done.set()
        steps.join(timeout=2)
        a.bus.unsubscribe(q)
    return {"reply": reply, "spoken": spoken, "route": route_name(getattr(a.brain, "last_route", "")),
            "seconds": round(time.time() - t0, 1)}


def open_events(a):
    """(next_event, close) for one phone connection. next_event(timeout) returns a small dict or None when quiet.
    Only what the phone shows is passed on: the assistant's state and whether something waits for a yes / no."""
    q = a.bus.subscribe()

    def next_event(timeout: float = 15.0):
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                e = q.get(timeout=left)
            except queue.Empty:
                return None
            kind = e.get("type")
            if kind == "status":
                return {"type": "status", "state": e.get("state", ""), "detail": str(e.get("detail", ""))[:160]}
            if kind == "confirm":
                return {"type": "approval", "pending": a.api_approval()}
            if kind == "flags":
                return {"type": "switches", "mic_muted": bool(e.get("mic_muted")), "voice_muted": bool(a.tts.muted)}

    return next_event, lambda: a.bus.unsubscribe(q)
