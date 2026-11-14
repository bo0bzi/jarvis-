"""Playbooks: step-by-step recipes the local AI learned by watching the online AI do a job.

A playbook = what you say (keywords + real example requests), the steps the online AI took (tool names with the
arguments that worked, e.g. press_keys keys='win+d'), and what to watch out for. They are written by the nightly full
review (distill.py) and shown to the LOCAL brain, as text in its prompt, whenever a new request looks like one of them.
They are advice only: every action still goes through the normal permission checks, and a playbook can never turn a
permission off. A playbook is only shown to the local AI after it was tested (distill.evaluate): the local AI must do
the job better with it than without it.

File: data/playbooks.json. Newest wins on the same title."""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime

from .autonomy import ALWAYS_ASK
from .env import data_dir

_LOCK = threading.RLock()
_TL = threading.local()  # evaluation (distill.py) shows chosen playbooks to ONE thread only: live chat is never affected
MAX_PROMPT_CHARS = 1800
STATUSES = ("unchecked", "proven", "redundant", "no_help", "unproven", "failing")
_DESTRUCTIVE = re.compile(r"(?i)\b(del|erase|rd|rmdir|rm|format|diskpart|shutdown|restart-computer|stop-computer|"
                          r"remove-item|reg\s+(delete|add)|net\s+user|takeown|icacls|cipher|bcdedit|taskkill|sc\s+(delete|stop)|"
                          r"clear-disk|set-executionpolicy|invoke-expression|iex|downloadstring|start-bitstransfer)\b")
_INJECT = re.compile(r"(?i)ignore (all|any|previous|the above)|disregard|override|jailbreak|without (asking|permission|"
                     r"approval|confirm)|no (permission|approval) (needed|required)|skip (the )?(permission|approval|"
                     r"confirmation)|auto-?approve|turn off (the )?(permission|safety)|never ask")


def _path():
    return data_dir() / "playbooks.json"


def load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8")) if _path().exists() else []
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("title") and p.get("steps")]


def save(items: list[dict]) -> None:
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


def clean(raw: dict, tools: dict) -> tuple[dict | None, str]:
    """Validate one playbook proposed by the review AI. (playbook, '') or (None, why not).
    `tools` = the registry (name -> Tool). Risky tools keep their name but lose their arguments, so a playbook can
    never carry a ready-made delete / send / power command."""
    from .blackbox import redact_args

    title = " ".join(str(raw.get("title", "")).split())[:80]
    if len(title) < 4:
        return None, "no title"
    when = [" ".join(str(w).lower().split())[:40] for w in (raw.get("when") or []) if str(w).strip()][:10]
    examples = [" ".join(str(x).split())[:200] for x in (raw.get("examples") or []) if str(x).strip()][:3]
    if not when and not examples:
        return None, "nothing says when it applies"
    steps = []
    for s in (raw.get("steps") or [])[:10]:
        if not isinstance(s, dict):
            continue
        name = str(s.get("tool", "")).strip()
        t = tools.get(name)
        if t is None:
            return None, f"unknown tool '{name}'"
        args = s.get("args") if isinstance(s.get("args"), dict) else {}
        args = redact_args(name, args)
        if t.risk in ALWAYS_ASK or (t.dangerous and not t.risk):  # send / delete / spend: never a ready-made target or message
            args = {}
        elif name == "run_shell_command" and _DESTRUCTIVE.search(str(args.get("command", ""))):
            args = {}  # a destructive command is never stored as a recipe
        why = " ".join(str(s.get("why", "")).split())[:140]
        steps.append({"tool": name, "args": {k: v for k, v in args.items() if k in t.params.get("properties", {})}, "why": why})
    if not steps:
        return None, "no steps"
    watch = " ".join(str(raw.get("watch_out", "")).split())[:300]
    blob = " ".join([title, watch, *when, *examples, *(s["why"] for s in steps)])
    if _INJECT.search(blob):
        return None, "wording that tries to get around permissions"
    return {"title": title, "when": when, "examples": examples, "steps": steps, "watch_out": watch,
            "from_turns": [str(x) for x in (raw.get("from_turns") or [])][:6]}, ""


def add_or_update(pb: dict, source: str = "") -> dict:
    """Same title = update (the newer recipe wins and must be tested again)."""
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK:
        items = load()
        for it in items:
            if it["title"].lower() == pb["title"].lower():
                it.update(pb, updated=now, source=source or it.get("source", ""), status="unchecked", enabled=False, eval={},
                          live={}, cases=[], retested="")
                save(items)
                try:  # a rewritten playbook starts its record of real use from zero
                    from . import verify

                    verify.reset("playbooks", it["id"])
                except Exception:
                    pass
                return it
        nid = max([int(i.get("id", 0)) for i in items] + [0]) + 1
        it = {"id": nid, **pb, "source": source, "created": now, "updated": now, "status": "unchecked", "enabled": False,
              "eval": {}}
        items.append(it)
        save(items)
        return it


def update(pid: int, **fields) -> dict | None:
    with _LOCK:
        items = load()
        for it in items:
            if int(it["id"]) == int(pid):
                it.update(fields)
                save(items)
                return it
    return None


def remove(key) -> int:
    key = str(key).strip().lower()
    with _LOCK:
        items = load()
        keep = [i for i in items if str(i["id"]) != key and key not in i["title"].lower()]
        if len(keep) != len(items):
            save(keep)
        return len(items) - len(keep)


def prune(keep: int) -> int:
    """Keep at most `keep` playbooks: proven ones first, then the newest."""
    with _LOCK:
        items = load()
        if len(items) <= keep:
            return 0
        ranked = sorted(items, key=lambda p: (p.get("status") == "proven", p.get("updated", "")), reverse=True)
        gone = {p["id"] for p in ranked[keep:]}
        save([p for p in items if p["id"] not in gone])
        return len(gone)


# ------------------------------------------------------------------ choosing what the local AI sees
@contextmanager
def testing(only: list[dict] | None = None):
    """For the evaluation: in THIS thread, show exactly `only` (a list of playbooks, maybe empty) and nothing else."""
    before = getattr(_TL, "only", None)
    _TL.only = list(only or [])
    try:
        yield
    finally:
        _TL.only = before


def _candidates() -> list[dict]:
    only = getattr(_TL, "only", None)
    return only if only is not None else [p for p in load() if p.get("enabled")]


def _stems(text: str) -> set[str]:
    from .toolpick import _stems

    return _stems(text)


def _score(pb: dict, text: str) -> int:
    low = (text or "").lower()
    stems = _stems(text)
    score = 0
    for w in pb.get("when", []):
        if w and w in low:
            score += 3
        elif _stems(w) and _stems(w) <= stems:
            score += 2
    for ex in pb.get("examples", []):
        a = _stems(ex)
        if a and stems and len(a & stems) / len(a | stems) >= 0.5:
            score += 4
            break
    return score


def match(text: str, limit: int = 2) -> list[dict]:
    scored = sorted(((_score(p, text), p) for p in _candidates()), key=lambda x: -x[0])
    return [p for s, p in scored[:limit] if s >= 3]


def tools_for(text: str) -> list[str]:
    """Tools the matching playbooks use: they are shown to the local AI (it can't use a tool it was never shown)."""
    names: list[str] = []
    for p in match(text):
        for s in p["steps"]:
            if s["tool"] not in names:
                names.append(s["tool"])
    return names


def render(pb: dict) -> str:
    steps = []
    for i, s in enumerate(pb["steps"], 1):
        args = ", ".join(f"{k}={v!r}" for k, v in s["args"].items())
        steps.append(f"{i}) {s['tool']}({args})" + (f" - {s['why']}" if s.get("why") else ""))
    return f"- {pb['title']}: " + "; ".join(steps) + (f". Watch out: {pb['watch_out']}" if pb.get("watch_out") else "")


def prompt_section(user_text: str = "") -> str:
    hits = match(user_text)
    if not hits:
        return ""
    head = ("\nPLAYBOOKS (a stronger AI already did a request like this one; do the same, with the details of this "
            "request. Check each step's result before the next, and report only what the results say):")
    out, used, shown = [], len(head), []
    for p in hits:
        line = "\n" + render(p)
        if used + len(line) > MAX_PROMPT_CHARS:
            break
        out.append(line)
        shown.append(p["id"])
        used += len(line)
    if shown and getattr(_TL, "only", None) is None:  # real use (not a night-time test): remember it, so it can be graded
        try:
            from . import blackbox

            blackbox.mark_used("playbook", shown)
        except Exception:
            pass
    return head + "".join(out) if out else ""
