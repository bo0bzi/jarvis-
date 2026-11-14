"""Night-time check of what Jarvis learned: is it really helping, and is it still right?

Two checks, run every night after the full review (and when you say "check what you learned"):
  1. REAL USE. The black box remembers which playbooks (and rules that only apply to some requests) were shown to the AI
     for each request. The online AI grades those requests during the full review - right or wrong, judged by the tool
     results and your reaction, not by how the answer sounds. A playbook that was used at least 3 times and went well
     less than half the time is switched off ("failing"). A night-learned rule that was used at least 4 times and went well
     less than 40% of the time is paused. Rules YOU taught are never touched: they are only reported.
  2. RE-TEST. Every playbook keeps the recorded cases it came from. A few each night are replayed on the local AI
     (distill.evaluate): if the local AI now does the job right without it, the playbook is switched off (redundant); if
     it no longer helps, it is switched off; if it helps again it is switched back on.
Nothing here runs a tool or touches the PC: acting out a learned job at night would press keys and run commands while
you sleep. What the AI really did during the day, graded the next night, is the real test.
Numbers: data/learned_stats.json."""
from __future__ import annotations

import json
import threading
from datetime import datetime

from . import lessons, playbooks
from . import tools as T
from .env import data_dir

MIN_PB_USES = 3
MIN_RULE_USES = 4
PB_FAIL_BELOW = 0.5
PB_CONFIRM_FROM = 0.75
RULE_FAIL_BELOW = 0.4
RETEST_PER_NIGHT = 6


def _path():
    return data_dir() / "learned_stats.json"


def load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("playbooks", {})
    d.setdefault("lessons", {})
    d.setdefault("seen", [])
    return d


def _save(d: dict) -> None:
    try:
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        tmp.replace(_path())
    except OSError:
        pass


def reset(kind: str, key) -> None:
    """A playbook was rewritten: its old results no longer say anything about the new version."""
    d = load()
    d[kind].pop(str(key), None)
    _save(d)


def record(eps: list[dict], grades: dict | None = None) -> int:
    """Count how the requests that used a playbook / rule went. grades: {episode id: (correct, why)} from the online AI;
    a request it did not grade counts as good unless something went wrong (a failed step, a caught claim, a complaint)."""
    grades = grades or {}
    d = load()
    seen = set(d["seen"])
    counted = 0
    for ep in eps:
        used = ep.get("used") or {}
        if ep["id"] in seen or not (used.get("playbook") or used.get("lesson")):
            continue
        ok, why = grades.get(ep["id"]) or (not ep["flags"], "; ".join(dict.fromkeys(ep["flags"]))[:160])
        for kind, store in (("playbook", "playbooks"), ("lesson", "lessons")):
            for i in used.get(kind, []):
                s = d[store].setdefault(str(i), {"uses": 0, "good": 0})
                s["uses"] += 1
                s["good"] += bool(ok)
                s["last"] = ep["t"]
                if not ok:
                    s["last_bad"] = f"{ep['text'][:80]} - {why}"[:220]
        seen.add(ep["id"])
        counted += 1
    d["seen"] = list(seen)[-600:]
    _save(d)
    return counted


def judge(ctx=None) -> list[str]:
    """Turn the counted results into decisions. Returns what changed, in plain words."""
    d = load()
    lines: list[str] = []
    for p in playbooks.load():
        s = d["playbooks"].get(str(p["id"]))
        if not s:
            continue
        rate = s["good"] / s["uses"]
        live = {"uses": s["uses"], "good": s["good"], "last_bad": s.get("last_bad", "")}
        changes: dict = {"live": live}
        if s["uses"] >= MIN_PB_USES and rate < PB_FAIL_BELOW and p.get("enabled"):
            changes.update(enabled=False, status="failing")
            lines.append(f"Switched off playbook #{p['id']} '{p['title']}': only {s['good']} of {s['uses']} jobs went well "
                         f"when it was used." + (f" Last problem: {s['last_bad']}" if s.get("last_bad") else ""))
        elif s["uses"] >= MIN_PB_USES and rate >= PB_CONFIRM_FROM and p.get("live") != live:
            lines.append(f"Playbook #{p['id']} '{p['title']}' is working: {s['good']} of {s['uses']} jobs went well.")
        playbooks.update(p["id"], **changes)
    for l in lessons.load(ctx):
        s = d["lessons"].get(str(l.get("id")))
        if not s or not (l.get("when") or "").strip() or l.get("paused") or s["uses"] < MIN_RULE_USES:
            continue
        if s["good"] / s["uses"] >= RULE_FAIL_BELOW or s.get("told"):
            continue
        if l.get("source") == "evolve":
            lessons.set_paused(l["id"], True, f"only {s['good']} of {s['uses']} jobs went well", ctx)
            lines.append(f"Paused the rule I wrote myself, '{l['title']}': only {s['good']} of {s['uses']} jobs went well "
                         "when it applied. (Dashboard > Learning: you can resume or forget it.)")
        else:
            lines.append(f"Your rule '{l['title']}' applied {s['uses']} times and only {s['good']} went well. Worth a look: "
                         "I left it as it is.")
        s["told"] = True
    _save(d)
    return lines


def retest(brain, limit: int = RETEST_PER_NIGHT) -> list[str]:
    """Replay a few playbooks' recorded cases on the local AI (nothing is run) and switch them on or off accordingly."""
    from . import blackbox, distill

    local = brain._local_brain("") if brain is not None and hasattr(brain, "_local_brain") else None
    if local is None:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    due = sorted((p for p in playbooks.load() if p.get("cases") and p.get("status") in ("proven", "redundant", "no_help")
                  and p.get("retested", "") < today), key=lambda p: p.get("retested", ""))
    lines: list[str] = []
    for p in due[:limit]:
        with blackbox.quiet():
            ev = distill.evaluate(local, p, p["cases"])
        status = distill.verdict(ev)
        ev.update(model=getattr(local, "label", ""), checked=datetime.now().isoformat(timespec="seconds"))
        on = status == "proven"
        playbooks.update(p["id"], status=status, enabled=on, eval=ev, retested=today)
        if p.get("enabled") and not on:
            why = {"redundant": "the local AI now does this job right without it",
                   "no_help": "it no longer helps the local AI", "unproven": "it could not be tested"}[status]
            lines.append(f"Switched off playbook #{p['id']} '{p['title']}': {why}.")
        elif on and not p.get("enabled"):
            lines.append(f"Switched playbook #{p['id']} '{p['title']}' back on: the local AI does the job better with it "
                         f"({ev['with']} of {ev['steps']} steps, alone {ev['alone']}).")
    return lines


def run(ctx, brain=None) -> str:
    """Judge the real use, then re-test. '' when there is nothing learned yet."""
    pbs, rules = playbooks.load(), [l for l in lessons.load(ctx) if l.get("source") == "evolve"]
    if not pbs and not rules:
        return ""
    lines = judge(ctx) + retest(brain)
    on = sum(1 for p in playbooks.load() if p.get("enabled"))
    head = f"Check of what was learned: {on} of {len(pbs)} playbook(s) switched on."
    return head + ("\n" + "\n".join("  " + x for x in lines) if lines else " Nothing needed changing.")


@T.tool("verify_learning", "Check what Jarvis learned: grade how the playbooks and rules did in real use, and re-test every "
        "playbook on the local AI. Switches off anything that stopped helping. Say 'check what you learned'.")
def verify_learning(ctx):
    def go():
        ctx.speak(run(ctx, getattr(ctx, "brain", None)).splitlines()[0] or "Nothing learned yet.")
    threading.Thread(target=go, daemon=True, name="verify").start()
    return "Started the check of what I learned. I'll tell you when it's done."
