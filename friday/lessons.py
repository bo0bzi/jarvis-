"""Ways of thinking the user teaches Jarvis. Saved in data/lessons.json and injected into every prompt, so a
lesson taught once is followed every time.

A lesson = a title, HOW to think / what to do, and an optional WHEN (keywords; empty = always apply).
Teach by saying:  "teach: when I ask about a part number, always check the manuals first"
or from the dashboard (Thinking tab). Jarvis can also save one itself with the teach_way_to_think tool."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

from . import tools as T

_LOCK = threading.Lock()
MAX_PROMPT_CHARS = 3500
DATA_DIR: Path | None = None  # set by the app at startup; tests/CLI fall back to ./data


def _path(ctx=None) -> Path:
    base = DATA_DIR
    if base is None and ctx is not None and getattr(ctx, "settings", None) is not None:
        base = Path(ctx.settings.data_dir)
    if base is None:
        from .env import data_dir

        base = data_dir()
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    return base / "lessons.json"


def load(ctx=None) -> list[dict]:
    p = _path(ctx)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict) and x.get("how")]


def _save(items: list[dict], ctx=None) -> None:
    p = _path(ctx)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def prune_auto(keep: int, ctx=None) -> int:
    """Keep only the newest `keep` auto-learned (night-time) lessons; the ones you taught yourself are never touched."""
    with _LOCK:
        items = load(ctx)
        autos = [i for i in items if i.get("source") == "evolve"]
        drop = {i["id"] for i in autos[:-max(0, keep)]} if len(autos) > keep else set()
        if drop:
            _save([i for i in items if i["id"] not in drop], ctx)
        return len(drop)


def add(title: str, how: str, when: str = "", ctx=None, source: str = "") -> dict:
    how, title, when = how.strip(), (title or "").strip(), (when or "").strip()
    if not how:
        raise ValueError("A lesson needs some text about how to think.")
    title = title or " ".join(how.split()[:6]).rstrip(",;:- ")
    with _LOCK:
        items = load(ctx)
        for it in items:  # same title = update it, don't duplicate
            if it["title"].lower() == title.lower():
                it.update(how=how, when=when, updated=datetime.now().isoformat(timespec="seconds"))
                _save(items, ctx)
                return it
        nid = max([int(i.get("id", 0)) for i in items] + [0]) + 1
        it = {"id": nid, "title": title[:80], "how": how[:1200], "when": when[:200],
              "created": datetime.now().isoformat(timespec="seconds")}
        if source:
            it["source"] = source  # "evolve" = learned by Jarvis itself at night
        items.append(it)
        _save(items, ctx)
        return it


def remove(key: str, ctx=None) -> int:
    """Delete by id or by (part of) the title. Returns how many were removed."""
    key = str(key).strip().lower()
    with _LOCK:
        items = load(ctx)
        keep = [i for i in items if str(i.get("id")) != key and key not in i["title"].lower()]
        if len(keep) != len(items):
            _save(keep, ctx)
        return len(items) - len(keep)


def applies(lesson: dict, user_text: str) -> bool:
    when = (lesson.get("when") or "").strip()
    if not when:
        return True
    low = (user_text or "").lower()
    words = [w for w in re.split(r"[,;/|]| or ", when.lower()) if w.strip()]
    return any(w.strip() in low for w in words)


def matching(user_text: str, ctx=None) -> list[dict]:
    return [l for l in load(ctx) if not l.get("paused") and applies(l, user_text)]


def set_paused(lid, paused: bool, why: str = "", ctx=None) -> bool:
    """Switch a rule off (or back on) without deleting it. The night-time check pauses night-learned rules that do not help."""
    with _LOCK:
        items = load(ctx)
        for it in items:
            if str(it.get("id")) == str(lid):
                it["paused"] = bool(paused)
                it["paused_why"] = why[:200] if paused else ""
                _save(items, ctx)
                return True
    return False


def prompt_section(user_text: str = "", ctx=None) -> str:
    items = matching(user_text, ctx)
    try:  # a rule that only applies to some requests: remember when it was shown, so the night-time check can grade it
        from . import blackbox

        blackbox.mark_used("lesson", [l["id"] for l in items if (l.get("when") or "").strip()])
    except Exception:
        pass
    head = ("\nHOW THE USER TAUGHT YOU TO THINK (they will keep teaching you new ways; follow these exactly, they "
            "override your habits. When the user teaches you a new way of thinking, call teach_way_to_think to save "
            "it, then confirm in one short sentence which rule you saved):")
    if not items:
        return head + "\n(no lessons yet)"
    out, used = [], len(head)
    for l in items:
        line = f"\n- {l['title']}: {l['how']}" + (f" (applies when: {l['when']})" if l.get("when") else "")
        if used + len(line) > MAX_PROMPT_CHARS:
            break
        out.append(line)
        used += len(line)
    return head + "".join(out)


# ------------------------------------------------------------------ weekly review
def _words(l: dict) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (l["title"] + " " + l["how"]).lower())}


def review(ctx=None, now: datetime | None = None) -> str:
    """Plain-language check of everything Jarvis was taught: too many to fit, near-duplicates, stale auto-lessons."""
    now = now or datetime.now()
    items = load(ctx)
    if not items:
        return "No lessons yet. Teach me with: teach: when I ask about X, always do Y."
    lines = [f"Lessons review: {len(items)} lesson(s) taught."]
    always = [l for l in items if not (l.get("when") or "").strip()]
    used, dropped = len(prompt_section("", ctx)), []
    shown = prompt_section("", ctx)
    for l in always:  # an 'always' lesson that does not fit in the prompt budget is silently ignored
        if f"- {l['title']}:" not in shown:
            dropped.append(l["title"])
    if dropped:
        lines.append(f"- {len(dropped)} always-on lesson(s) DON'T FIT and are being ignored: " + "; ".join(dropped[:5]) +
                     ". Shorten them, give them a 'when' keyword, or delete some.")
    dup = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            wa, wb = _words(a), _words(b)
            if wa and wb and len(wa & wb) / len(wa | wb) >= 0.6:
                dup.append(f"'{a['title']}' and '{b['title']}'")
    if dup:
        lines.append("- Look like duplicates: " + "; ".join(dup[:4]) + ".")
    stale = []
    for l in items:
        if l.get("source") == "evolve":
            try:
                if (now - datetime.fromisoformat(l.get("updated") or l["created"])).days > 30:
                    stale.append(l["title"])
            except (ValueError, KeyError):
                pass
    if stale:
        lines.append(f"- {len(stale)} night-learned lesson(s) older than 30 days (check they still help): " + "; ".join(stale[:4]) + ".")
    lines.append(f"- Prompt space used by lessons: {used} of {MAX_PROMPT_CHARS} characters.")
    if len(lines) == 2:
        lines.append("- All good: nothing is ignored, duplicated or stale.")
    return "\n".join(lines)


# ------------------------------------------------------------------ tools
@T.tool("review_lessons", "Review everything the user taught me: lessons that don't fit, duplicates, stale ones.")
def review_lessons(ctx):
    return review(ctx)


@T.tool("teach_way_to_think", "Save a way of thinking or working that the user just taught you, so you follow it "
        "from now on. title = short name; how = the rule in your own clear words; when = optional keywords "
        "(comma separated) that make it apply - leave empty to apply it always.",
        {"type": "object", "properties": {"title": {"type": "string"}, "how": {"type": "string"},
                                          "when": {"type": "string"}}, "required": ["how"]})
def teach_way_to_think(ctx, how: str, title: str = "", when: str = ""):
    it = add(title, how, when, ctx)
    return f"Saved lesson #{it['id']} '{it['title']}'. I will follow it from now on."


@T.tool("list_ways_of_thinking", "List every way of thinking the user has taught you.")
def list_ways_of_thinking(ctx):
    items = load(ctx)
    if not items:
        return "No lessons yet. The user can teach one by saying 'teach: <how to think>'."
    return "\n".join(f"#{l['id']} {l['title']}: {l['how']}" + (f" [when: {l['when']}]" if l.get("when") else "")
                     for l in items)


@T.tool("forget_way_of_thinking", "Delete a taught lesson by its number or part of its title.",
        {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})
def forget_way_of_thinking(ctx, which: str):
    n = remove(which, ctx)
    return f"Forgot {n} lesson(s)." if n else f"No lesson matched '{which}'."
