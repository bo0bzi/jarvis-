"""Daily routines, run by Friday's background loop (routines.tick every few seconds):

* Morning briefing (BRIEFING_TIME, default 07:30): date, weather, open shop jobs, parts reminders, credits used,
  and what Friday did overnight. Spoken out loud, and texted to you if SMS is set up (BRIEFING_SMS).
* Nightly self-improvement (NIGHTLY_TIME, default 02:00): backup, review of the day's errors, and repair of broken
  learned skills using ONLY the free local AI (cloud credits are never used at night - nobody is awake to approve).
  Repaired skills are installed automatically only at autonomy level 2; otherwise they wait for your OK
  ("install the nightly fixes"). The report goes into the morning briefing.
Each routine runs at most once a day; state is kept in data/routines.json."""
from __future__ import annotations

import json
import shutil
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from . import activity
from .env import data_dir, env, env_bool, env_int
from .tools import REGISTRY, tool

WINDOW_HOURS = 4  # if Friday was off at the set time, still run within this many hours (not at 3 pm)
_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def _state_path() -> Path:
    return data_dir() / "routines.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mark(name: str, now: datetime) -> None:
    st = _load()
    st[name] = now.strftime("%Y-%m-%d")
    _state_path().write_text(json.dumps(st, indent=1), encoding="utf-8")


def _parse_hhmm(s: str, default: str) -> tuple[int, int]:
    for v in (s, default):
        try:
            h, m = (v or "").strip().replace(".", ":").split(":")[:2]
            return max(0, min(23, int(h))), max(0, min(59, int(m)))
        except ValueError:
            continue
    return 0, 0


def is_due(name: str, hhmm: str, default: str, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if _load().get(name) == now.strftime("%Y-%m-%d"):
        return False
    h, m = _parse_hhmm(hhmm, default)
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return start <= now < start + timedelta(hours=WINDOW_HOURS)


def tick(assistant, now: datetime | None = None) -> list[str]:
    """Called every few seconds. Starts any routine that is due (in its own thread). Returns what started."""
    now = now or datetime.now()
    started = []
    jobs = []
    if env_bool("BRIEFING_ENABLED", True) and is_due("briefing", env("BRIEFING_TIME"), "07:30", now):
        jobs.append(("briefing", lambda: deliver_briefing(assistant)))
    if env_bool("NIGHTLY_ENABLED", True) and is_due("nightly", env("NIGHTLY_TIME"), "02:00", now):
        jobs.append(("nightly", lambda: run_nightly(assistant.ctx, getattr(assistant, "brain", None))))
    if env_bool("DIGEST_ENABLED", True) and is_due("digest", env("DIGEST_TIME"), "20:00", now):
        jobs.append(("digest", lambda: deliver_digest(assistant)))
    if env_bool("LESSON_REVIEW_ENABLED", True) and now.weekday() == 6 and is_due("lessons", "10:00", "10:00", now):
        jobs.append(("lessons", lambda: deliver_lesson_review(assistant)))  # Sundays, 10:00
    for name, fn in jobs:
        with _LOCK:
            if name in _RUNNING:
                continue
            _RUNNING.add(name)
        _mark(name, now)  # mark first: a crash must not make it repeat every 5 seconds
        started.append(name)

        def run(name=name, fn=fn):
            try:
                fn()
            except Exception as e:
                activity.log("error", f"routine {name} failed: {type(e).__name__}: {e}")
            finally:
                with _LOCK:
                    _RUNNING.discard(name)
        threading.Thread(target=run, daemon=True, name=f"routine-{name}").start()
    return started


# ------------------------------------------------------------------ morning briefing
def _report_path() -> Path:
    return data_dir() / "nightly_report.txt"


def build_briefing(ctx) -> str:
    from . import credits, shop

    now = datetime.now()
    parts = [f"Good morning. It's {now:%A, %B} {now.day}."]
    w = REGISTRY.get("get_weather")
    if w is not None:
        try:
            weather = str(w.func(ctx))
            if not weather.startswith("Error"):
                parts.append(weather)
        except Exception:
            pass
    try:
        from . import agenda

        today = agenda.summary("today", getattr(ctx.settings, "data_dir", None), now)
        if not today.startswith("Nothing"):
            parts.append(today.replace("\n- ", "; ").replace(":\n", ": ") + ".")
    except Exception as e:
        activity.log("error", f"briefing calendar part failed: {e}")
    try:
        jobs = shop.open_jobs_summary(ctx)
        parts.append(f"Shop jobs open: {jobs}." if jobs else "No open jobs on the board.")
        due = shop.due_parts_reminders(ctx, mark_done=True)
        if due:
            parts.append("Parts reminders: " + "; ".join(due) + ".")
    except Exception as e:
        activity.log("error", f"briefing shop part failed: {e}")
    try:
        g = credits.get_gate()
        spent = g.spent_today() if hasattr(g, "spent_today") else 0
        st = g.status()
        yday = sum(e.get("actual", 0) or 0 for e in st.get("ledger", [])
                   if str(e.get("time", "")).startswith((now - timedelta(days=1)).strftime("%Y-%m-%d")))
        if yday or spent:
            parts.append(f"Cloud credits used yesterday: {yday:g}. This month: {g.spent_month():g}.")
    except Exception:
        pass
    rp = _report_path()
    if rp.exists() and datetime.fromtimestamp(rp.stat().st_mtime) > now - timedelta(hours=20):
        first = rp.read_text(encoding="utf-8").strip().splitlines()
        summary = next((ln for ln in first if ln.startswith("Summary:")), "")
        if summary:
            parts.append("Overnight: " + summary.removeprefix("Summary:").strip())
    return " ".join(parts)


def deliver_briefing(assistant) -> str:
    text = build_briefing(assistant.ctx)
    activity.log("routine", "Morning briefing delivered")
    try:
        assistant._announce(text)
    except Exception:
        print(text)
    if env_bool("BRIEFING_SMS", True):
        try:
            from .sms import get_link

            link = get_link()
            if link.configured():
                link.text_owner(text[:1500])
        except Exception as e:
            activity.log("error", f"briefing text failed: {e}")
    return text


@tool("morning_briefing", "Give the morning briefing now: weather, open shop jobs, parts reminders, credits used "
      "and what Friday did overnight.")
def morning_briefing(ctx):
    return build_briefing(ctx)


# ------------------------------------------------------------------ evening digest
def build_digest(ctx, hours: float = 24) -> str:
    """What Jarvis did in the last day, in a few plain lines: activity by kind, cloud use, failures, self-changes."""
    from collections import Counter

    from . import claude_tiers, credits

    rows = activity.recent(2000, hours=hours)
    kinds = Counter(r.get("kind", "?") for r in rows)
    lines = [f"Jarvis digest, last {int(hours)} hours:"]
    done = [r for r in rows if r.get("kind") in ("tool", "api", "sms", "skill", "selfmod", "routine", "nightly")]
    lines.append(f"- {len(rows)} things logged; {len(done)} actions (" +
                 (", ".join(f"{n} {k}" for k, n in kinds.most_common(4)) or "none") + ").")
    try:
        s = credits.get_gate().status()
        lines.append(f"- Cloud credits: {s['spent_today']:g} today of {s['daily_cap']}; {s['spent_month']:g} this month of {s['monthly_cap']}.")
        if s["monthly_cap"] and s["spent_month"] >= 0.8 * s["monthly_cap"]:
            lines.append("- WARNING: over 80% of this month's credit cap is used.")
    except Exception:
        pass
    st = getattr(ctx, "settings", None)
    if st is not None and claude_tiers.enabled():
        used, cap = claude_tiers.used_month(st), claude_tiers.month_cap()
        lines.append(f"- Claude: {claude_tiers.summary(st)}" + (" WARNING: over 80% of the monthly Claude limit." if cap and used >= 0.8 * cap else ""))
    errs = [r for r in rows if r.get("kind") == "error"]
    if errs:
        lines.append(f"- {len(errs)} problem(s), latest: {errs[-1].get('message', '')[:140]}")
    changes = [r for r in rows if r.get("kind") == "selfmod"]
    if changes:
        lines.append("- Changes I made to myself: " + "; ".join(r.get("message", "")[:80] for r in changes[-3:]) +
                     ". Say 'undo your last change' to reverse the latest.")
    denied = [r for r in rows if r.get("kind") == "credits" and "Declined" in r.get("message", "")]
    if denied:
        lines.append(f"- {len(denied)} cloud request(s) you declined.")
    return "\n".join(lines)


def deliver_digest(assistant) -> str:
    text = build_digest(assistant.ctx)
    activity.log("routine", "Evening digest delivered")
    try:
        from . import notify

        notify.push("Jarvis digest", text[:300], text_too=False)
    except Exception:
        pass
    if env_bool("DIGEST_SMS", False):
        try:
            from .sms import get_link

            link = get_link()
            if link.configured():
                link.text_owner(text[:1500])
        except Exception as e:
            activity.log("error", f"digest text failed: {e}")
    return text


def deliver_lesson_review(assistant) -> str:
    from . import lessons, notify

    text = lessons.review(assistant.ctx)
    activity.log("routine", "Weekly lessons review delivered")
    try:
        notify.push("Jarvis lessons review", text[:300], text_too=False)
    except Exception:
        pass
    return text


@tool("daily_digest", "Summarise what Jarvis did today: actions, errors, credits and Claude use, self-changes.")
def daily_digest(ctx):
    return build_digest(ctx)


# ------------------------------------------------------------------ nightly self-improvement
def fixes_dir() -> Path:
    from .skills import skills_dir

    d = skills_dir() / "_fixes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _local_ask(brain):
    """An ask_ai that can ONLY use the local (free) model - never the cloud, never the Credit Gate."""
    def ask(system, user, **_kw):
        local = brain._local_brain(env("LOCAL_CODER_MODEL")) if brain is not None else None
        if local is None:
            raise RuntimeError("no local AI available (cloud is never used at night)")
        return local._raw_complete(system, user), getattr(local, "label", "local AI") + " (local, free)"
    return ask


def run_nightly(ctx, brain=None, now: datetime | None = None) -> str:
    from . import autonomy, backup, skills
    from .tools import ToolContext

    now = now or datetime.now()
    lines, summary = [f"Nightly report - {now:%Y-%m-%d %H:%M}"], []
    activity.log("nightly", "Nightly self-improvement started")
    # 1. backup
    try:
        p = backup.make_backup(label="nightly")
        lines.append(f"Backup: {p.name}")
        summary.append("backup made")
    except Exception as e:
        lines.append(f"Backup FAILED: {e}")
        summary.append("backup failed")
    # 2. errors of the last 24 hours
    errs = activity.recent(5000, kind="error", hours=24)
    by_tool = Counter(e.get("tool") or e.get("message", "")[:40] for e in errs)
    lines.append(f"Errors in the last 24 h: {len(errs)}")
    lines += [f"  {n}x {name}" for name, n in by_tool.most_common(8)]
    # 3. broken learned skills: failed to load, or their tools keep failing
    broken = {name: err for name, err in skills.ERRORS.items()}
    for e in errs:
        src = str(e.get("source", ""))
        if src.startswith("skill:") and by_tool.get(e.get("tool"), 0) >= env_int("NIGHTLY_ERROR_THRESHOLD", 3):
            broken.setdefault(src.split(":", 1)[1], e.get("message", ""))
    fixed, failed = [], []
    ask_ctx = ToolContext(settings=getattr(ctx, "settings", None), ask_ai=_local_ask(brain))
    for name, err in list(broken.items())[:env_int("NIGHTLY_MAX_REPAIRS", 3)]:
        path = skills.skills_dir() / f"{name}.py"
        if not path.exists():
            continue
        request = next((ln.split(":", 1)[1].strip() for ln in path.read_text(encoding="utf-8").splitlines()[:3]
                        if ln.startswith("# Request:")), f"the {name} skill")
        pending, msg, _info = skills.write_skill(ask_ctx, request, name, previous_code=path.read_text(encoding="utf-8"),
                                                 prefer="local", extra_hint=f"It is broken. The error was: {err[:600]}")
        skills.remember_outcome(f"nightly repair: {request}", name, pending is not None, msg)
        if pending is None:
            failed.append(name)
            lines.append(f"Repair of '{name}' failed: {msg[:200]}")
            continue
        if autonomy.level() >= 2:
            skills.install(pending)
            fixed.append(name)
            lines.append(f"Repaired and installed '{name}' (autonomy level 2).")
        else:
            shutil.move(str(pending), fixes_dir() / pending.name)
            fixed.append(name + " (waiting for your OK)")
            lines.append(f"Repaired '{name}' - waiting for your OK (say 'install the nightly fixes').")
    if broken:
        summary.append(f"{len(fixed)} skill(s) repaired, {len(failed)} could not be fixed" if fixed or failed
                       else "broken skills found")
    elif errs:
        summary.append(f"{len(errs)} error(s) yesterday, no broken skills")
    else:
        summary.append("no errors yesterday")
    # 4. learning: what failed to be learned recently
    fails = [e for e in skills.learning_log()[-50:] if not e.get("ok")
             and e.get("time", "") >= (now - timedelta(days=1)).isoformat()]
    if fails:
        lines.append("Skills I failed to learn today: " + ", ".join(sorted({e["skill"] for e in fails})))
    # 4b. evolve: learn new behaviour rules from yesterday's mistakes (free OmniRoute / local AI, never credits)
    try:
        from . import evolve

        lines.append(evolve.run(ctx, brain))
        summary.append("learning session done")
    except Exception as e:
        lines.append(f"Learning session failed: {type(e).__name__}: {e}")
    # 4c. full review: the online AI (Abacus, no Friday-side credit cap) studies everything done and writes playbooks the
    #     local AI is then tested on. Skipped when there is too little to study or DISTILL_ENABLED=false.
    try:
        from . import distill

        rep = distill.run(ctx, brain)
        lines.append(rep)
        if "Studied" in rep:
            summary.append("full review done")
    except Exception as e:
        lines.append(f"Full review failed: {type(e).__name__}: {e}")
    # 5. knowledge base: read any manuals added during the day (local embeddings)
    try:
        from . import knowledge

        lines.append(knowledge.update_index())
    except Exception as e:
        lines.append(f"Knowledge base update failed: {e}")
    # 6. housekeeping: keep the activity log from growing forever
    try:
        _trim_activity(env_int("ACTIVITY_KEEP_LINES", 20000))
    except OSError:
        pass
    lines.insert(1, "Summary: " + "; ".join(summary) + ".")
    try:
        from . import updates

        updates.send("nightly", "Overnight: " + "; ".join(summary) + ".", speak=False)  # the morning briefing says it out loud
    except Exception:
        pass
    report = "\n".join(lines)
    _report_path().write_text(report, encoding="utf-8")
    activity.log("nightly", "Nightly done: " + "; ".join(summary))
    return report


def _trim_activity(keep: int) -> None:
    p = data_dir() / "activity.jsonl"
    if p.exists():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > keep:
            p.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")


@tool("run_nightly_now", "Run the nightly self-improvement now (backup, error review, repair broken learned skills "
      "with the free local AI). Takes a few minutes; the result is announced.")
def run_nightly_now(ctx):
    def go():
        report = run_nightly(ctx, getattr(ctx, "brain", None))
        ctx.speak("Self-improvement finished. " + report.splitlines()[1].removeprefix("Summary: "))
    threading.Thread(target=go, daemon=True).start()
    return "Started the self-improvement run. I'll tell you when it's done."


@tool("nightly_report", "Read the last nightly self-improvement report.")
def nightly_report(ctx):
    p = _report_path()
    return p.read_text(encoding="utf-8") if p.exists() else "No nightly report yet."


@tool("install_nightly_fixes", "Install the skill repairs the nightly run prepared (they were tested in the safety "
      "cage). Asks first.", dangerous=True)
def install_nightly_fixes(ctx):
    from . import skills

    done = []
    for p in sorted(fixes_dir().glob("*.py")):
        try:
            skills.install(p)
            done.append(p.stem)
        except Exception as e:
            activity.log("error", f"installing fix {p.stem} failed: {e}")
    return f"Installed: {', '.join(done)}." if done else "There are no nightly fixes waiting."
