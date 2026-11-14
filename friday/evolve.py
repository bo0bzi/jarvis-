"""Learn and evolve: Jarvis reviews its own conversations and turns its mistakes into new rules ("lessons").

Runs every night inside the nightly routine (02:00) and any time you say "evolve" / "learn from today".
What it does:
  1. Reads the last 24 hours of what you said and what Jarvis answered (data/trace.jsonl), plus errors.
  2. Flags the bad moments: you corrected it, a fake claim was caught, a tool failed, the local AI gave up.
  3. Asks an AI for up to 4 short new behaviour rules that would have prevented them.
  4. Checks each rule (short, safe, not a copy of an old one) and saves it as an auto-learned lesson, which Jarvis
     follows from then on. You see them in the dashboard (Thinking & teaching) and can delete any of them.
Credits are NEVER spent: it uses the free OmniRoute tier if set up, otherwise the local AI. Nothing is sent
anywhere except that one question. Web page text is never included, so a web page can't teach it anything.
Settings: EVOLVE_ENABLED (default true), EVOLVE_MAX_NEW (4), EVOLVE_MAX_AUTO (20 auto-learned lessons kept)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, lessons
from . import tools as T
from .env import data_dir, env_bool, env_int

MIN_EXCHANGES = 4  # fewer conversations than this: nothing worth learning from yet
_CORRECTION = re.compile(r"\b(no[, .]|wrong|that'?s not|not what i|you made that up|didn'?t (work|get|receive|happen)|"
                         r"still (didn'?t|not|no)|doesn'?t make sense|makes no sense|i (said|asked)|stop (making|guessing)|"
                         r"why (did|didn'?t|are you)|try again|again\b|you (didn'?t|did not|forgot|ignored))", re.I)
_UNSAFE = re.compile(r"https?://|www\.|api[ _-]?key|password|passcode|\btoken\b|secret|\.env\b|\brm\s|\bdel\s|format |"
                     r"powershell|\bcmd\b|sudo|ignore (all|any|previous|the above)|disregard|override|jailbreak|"
                     r"send (a )?(text|sms|email|message) to|credit card|social security", re.I)


def _report_path() -> Path:
    return data_dir() / "evolve_report.txt"


def last_report() -> str:
    p = _report_path()
    return p.read_text(encoding="utf-8") if p.exists() else "I haven't run a learning session yet. Say 'evolve'."


def _read_trace(hours: int) -> list[dict]:
    p = data_dir() / "trace.jsonl"
    if not p.exists():
        return []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-6000:]:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("time", "") >= cutoff and e.get("kind") in ("heard", "reply", "guard", "escalate", "tool", "interrupt"):
                out.append(e)
    except OSError:
        pass
    return out


def build_episodes(hours: int = 24) -> tuple[list[str], int]:
    """(compact text episodes, number of exchanges). Flagged (bad) episodes come first."""
    ev = _read_trace(hours)
    episodes, cur = [], None
    for e in ev:
        if e["kind"] == "heard":
            if cur:
                episodes.append(cur)
            cur = {"user": e["text"][:200], "reply": "", "flags": []}
        elif cur is None:
            continue
        elif e["kind"] == "reply" and not cur["reply"]:
            cur["reply"] = e["text"][:220]
        elif e["kind"] == "guard":
            cur["flags"].append("a claim with no action behind it was caught")
        elif e["kind"] == "escalate":
            cur["flags"].append("the local AI could not answer (" + e["text"][:70] + ")")
        elif e["kind"] == "interrupt":
            cur["flags"].append("the user talked over it")
    if cur:
        episodes.append(cur)
    for prev, nxt in zip(episodes, episodes[1:]):  # the next message is a complaint about the previous answer
        if _CORRECTION.search(nxt["user"]):
            prev["flags"].append(f"the user reacted badly: \"{nxt['user'][:90]}\"")
    for e in activity.recent(3000, kind="error", hours=hours)[-12:]:
        episodes.append({"user": "(a tool failed)", "reply": str(e.get("message", ""))[:160], "flags": ["tool error"]})
    bad = [x for x in episodes if x["flags"]]
    ok = [x for x in episodes if not x["flags"]][-6:]
    text = []
    for x in (bad[-22:] + ok):
        flag = " | PROBLEM: " + "; ".join(dict.fromkeys(x["flags"])) if x["flags"] else ""
        text.append(f"USER: {x['user']}\nJARVIS: {x['reply'] or '(no reply)'}{flag}")
    return text, len([x for x in episodes if x["user"] != "(a tool failed)"])


SYSTEM = (
    "You help improve a voice assistant named Jarvis that runs a motorcycle/powersports shop's PC. You are shown "
    "yesterday's exchanges; the ones marked PROBLEM went badly. Write at most {n} NEW short behaviour rules that "
    "would have prevented those problems. Each rule: one imperative sentence under 220 characters, general (useful "
    "for future conversations, not tied to one detail), about HOW to think, check, ask or answer. Never include "
    "URLs, code, commands, names, numbers, passwords or keys. Do not repeat the existing rules. If nothing "
    "useful can be learned, return an empty list. Reply with ONLY this JSON: "
    '{{"lessons":[{{"title":"3-6 words","how":"the rule","when":"optional comma separated keywords"}}],'
    '"notes":"one sentence on what went wrong overall"}}')


def _ask(brain, system: str, user: str) -> tuple[str, str]:
    """(answer, who answered). Free OmniRoute tier first, then the local AI. Never the paid backup."""
    if brain is not None and getattr(brain, "free_router", None) is not None and brain._free_allowed():
        try:
            brain._count_free_use()
            return brain.free_router._raw_complete(system, user), "free cloud tier (OmniRoute)"
        except Exception:
            pass
    local = brain._local_brain("") if brain is not None else None
    if local is None:
        raise RuntimeError("no free AI is available right now (the paid cloud is never used for this)")
    return local._raw_complete(system, user), f"local AI ({getattr(local, 'label', '')})"


def parse_lessons(raw: str) -> tuple[list[dict], str]:
    m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S), re.S)
    if not m:
        return [], ""
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return [], ""
    items = data.get("lessons") if isinstance(data, dict) else None
    out = [x for x in (items or []) if isinstance(x, dict) and isinstance(x.get("how"), str)]
    return out, str(data.get("notes", ""))[:300] if isinstance(data, dict) else ""


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (s or "").lower())}


def acceptable(item: dict, existing: list[dict]) -> str:
    """'' if the rule can be saved, else why not."""
    how = " ".join(str(item.get("how", "")).split())
    if not 15 <= len(how) <= 260:
        return "wrong length"
    if _UNSAFE.search(how) or _UNSAFE.search(str(item.get("title", ""))) or _UNSAFE.search(str(item.get("when", ""))):
        return "unsafe wording"
    a = _words(how)
    for e in existing:
        b = _words(e.get("how", ""))
        if a and b and len(a & b) / len(a | b) > 0.55:
            return "too similar to an old rule"
    return ""


def run(ctx, brain=None, hours: int = 24) -> str:
    """One learning session. Returns a plain-language report (also saved for 'what did you learn')."""
    now = datetime.now()
    head = f"Learning session - {now:%Y-%m-%d %H:%M}"
    if not env_bool("EVOLVE_ENABLED", True):
        return "Learning is switched off (EVOLVE_ENABLED=false)."
    episodes, exchanges = build_episodes(hours)
    if exchanges < MIN_EXCHANGES:
        rep = f"{head}\nOnly {exchanges} conversation(s) in the last {hours} hours - not enough to learn from yet."
        _report_path().write_text(rep, encoding="utf-8")
        return rep
    existing = lessons.load(ctx)
    maxn = max(1, min(8, env_int("EVOLVE_MAX_NEW", 4)))
    user = ("Existing rules (do not repeat):\n" + ("\n".join(f"- {e['how']}" for e in existing[-25:]) or "(none)")
            + "\n\nYesterday's exchanges:\n\n" + "\n\n".join(episodes))
    try:
        raw, who = _ask(brain, SYSTEM.format(n=maxn), user[:9000])
    except Exception as e:
        rep = f"{head}\nCouldn't learn tonight: {type(e).__name__}: {str(e)[:160]}"
        _report_path().write_text(rep, encoding="utf-8")
        activity.log("evolve", rep.splitlines()[-1])
        return rep
    proposed, notes = parse_lessons(raw)
    saved, skipped = [], []
    for item in proposed[:maxn + 2]:
        if len(saved) >= maxn:
            break
        why = acceptable(item, lessons.load(ctx))
        if why:
            skipped.append(f"'{str(item.get('how', ''))[:60]}' ({why})")
            continue
        it = lessons.add(str(item.get("title", "")), " ".join(str(item["how"]).split()),
                         str(item.get("when", ""))[:100], ctx, source="evolve")
        saved.append(it)
    lessons.prune_auto(env_int("EVOLVE_MAX_AUTO", 20), ctx)
    flagged = sum(1 for e in episodes if "PROBLEM" in e)
    lines = [head, f"Reviewed {exchanges} conversation(s), {flagged} went badly. Thought it over with the {who}."]
    if notes:
        lines.append("What went wrong: " + notes)
    if saved:
        lines.append(f"Learned {len(saved)} new rule(s):")
        lines += [f"  #{s['id']} {s['how']}" for s in saved]
    else:
        lines.append("No new rules this time.")
    if skipped:
        lines.append("Skipped: " + "; ".join(skipped))
    rep = "\n".join(lines)
    _report_path().write_text(rep, encoding="utf-8")
    activity.log("evolve", f"Learned {len(saved)} rule(s) from {exchanges} conversation(s)")
    try:  # refresh the "Learned skills" status check + agents page right away
        brain_owner = getattr(ctx, "brain", None)
        refresh = getattr(getattr(brain_owner, "ctx", None), "refresh_status", None) or getattr(ctx, "refresh_status", None)
        if callable(refresh):
            refresh()
    except Exception:
        pass
    return rep


# ------------------------------------------------------------------ tools
@T.tool("evolve_now", "Learn from the last 24 hours: review what went badly in yesterday's conversations and save "
        "new behaviour rules so the same mistakes stop. Free (never uses credits). Say 'evolve' or 'learn from today'.")
def evolve_now(ctx):
    return run(ctx, getattr(ctx, "brain", None))


@T.tool("what_did_you_learn", "Read the report of the last learning session: what went wrong and which new rules "
        "were learned.")
def what_did_you_learn(ctx):
    return last_report()
