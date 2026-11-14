"""Full review: a stronger online AI studies everything Jarvis did, and teaches the local AI how to do the same jobs.

Every night (after the normal learning session) and whenever you say "review everything":
  1. Reads the black box (blackbox.py): every request, every tool the AI ran with its exact arguments (each key press,
     click, typed word, shell command, app it opened), the results, the window in front, and the answers. Passwords and
     keys were blanked when it was written.
  2. Sends it to the online AI (Abacus). The owner turned this on and sets the limit on Abacus itself, so Friday puts
     no credit cap on it (credits.CreditGate uncapped task: still listed in the ledger, never counted against the caps).
  3. The online AI writes PLAYBOOKS (what you say -> the steps that worked -> what to watch out for) and a few RULES.
  4. Every new playbook is TESTED on the local AI without running anything: for each recorded step, the local AI is
     shown the conversation so far (with the recorded results) and asked what it would do next, once without the playbook
     and once with it. Only a playbook that makes it do better is switched on ("proven"). The rest are kept, switched off.
Playbooks reach the local AI as text in its prompt (playbooks.py). They never change a permission.
Settings: DISTILL_ENABLED (true), DISTILL_MAX_PLAYBOOKS (60). Report: data/distill_report.txt."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, blackbox, evolve, lessons, playbooks
from . import tools as T
from .env import data_dir, env_bool, env_int

BATCH_CHARS = 14000
MAX_BATCHES = 8
MAX_EVAL_STEPS = 6
MAX_EVAL_PLAYBOOKS = 10
MAX_EVAL_EPISODES = 2
_RUN = {"running": False}

SYSTEM = (
    "You review the work log of Jarvis, a voice assistant that runs a motorcycle / powersports shop's Windows PC. "
    "Each episode is one request: what the user said, the brain that answered (an online AI such as abacus or claude, "
    "a free cloud AI, or the weak local AI), every tool it ran with its arguments and results, and any PROBLEM. "
    "Your job is to teach the small LOCAL AI to do the same jobs the way the strong online AI did them.\n"
    "Write PLAYBOOKS, one per kind of job, only from episodes where the steps really worked (ok, and no PROBLEM after "
    "them). Prefer jobs the online AI solved after the local AI failed or was corrected. A playbook has: title (3-8 "
    "words); when = 3-8 short phrases the user says for this job (lower case); examples = 1-3 REAL requests copied "
    "from the episodes; steps = the tools in order, each with the arguments that worked (replace details that change "
    "from request to request, like a customer name, with a <placeholder>) and a short why; watch_out = the mistake that "
    "happened or the trap to avoid (one sentence, may be empty); from_episodes = the episode labels it came from, like "
    "[\"E3\"]. Use ONLY tool names from the list you are given. For tools that send, delete, spend or change the system, "
    "give no arguments. Never copy passwords, keys, tokens, card numbers or customers' private details. A job done in one "
    "plain step still gets a playbook when the local AI got it wrong. Skip chit-chat and one-off questions.\n"
    "Also write up to 3 RULES (how to think or check, one imperative sentence under 220 characters, general, no names, "
    "numbers, URLs, commands or keys) that would have prevented the PROBLEMs. And list in retire the ids of existing "
    "playbooks that this log shows are wrong.\n"
    "Also GRADE: for every episode that says USED (a playbook or rule was shown to the local AI) and every episode where the "
    "local AI ran tools, say in grades whether the job was done CORRECTLY, judging by the tool results and the user's "
    "reaction, not by how the answer sounds. A failed step that was fixed later still counts as correct if the final result "
    "is right. A reply that claims something the results do not show is incorrect. At most 40 grades.\n"
    "Reply with ONLY this JSON: {\"playbooks\":[{\"title\":\"\",\"when\":[],\"examples\":[],\"steps\":[{\"tool\":\"\","
    "\"args\":{},\"why\":\"\"}],\"watch_out\":\"\",\"from_episodes\":[]}],\"rules\":[{\"title\":\"3-6 words\","
    "\"how\":\"\",\"when\":\"optional comma separated keywords\"}],\"retire\":[],\"grades\":[{\"episode\":\"E1\",\"correct\":true,\"why\":\"\"}],\"notes\":\"one sentence on what went "
    "well or badly overall\"}. At most {n} playbooks. If nothing is worth learning, return empty lists."
)


def _report_path() -> Path:
    return data_dir() / "distill_report.txt"


def _state_path() -> Path:
    return data_dir() / "distill_state.json"


def last_report() -> str:
    p = _report_path()
    return p.read_text(encoding="utf-8") if p.exists() else "No full review has run yet. Say 'review everything'."


def running() -> bool:
    return _RUN["running"]


def _last_review() -> str:
    """When the last review started (ISO text), or ' if there was none: what it already studied is never studied twice."""
    try:
        return str(json.loads(_state_path().read_text(encoding="utf-8"))["last"])
    except (OSError, ValueError, KeyError):
        return ""


# ------------------------------------------------------------------ what the reviewer reads
def render_episode(label: str, ep: dict, names: dict | None = None) -> str:
    who = ep["final_brain"] or (ep["brains"][-1] if ep["brains"] else "?")
    lines = [f"[{label}] USER: {ep['text'][:300]}", f"  answered by: {who}" + (" (online AI)" if ep["cloud_final"] else "")]
    for s in ep["steps"][:12]:
        args = ", ".join(f"{k}={str(v)[:90]!r}" for k, v in (s.get("args") or {}).items())
        win = f" [window: {s['window']}]" if s.get("window") else ""
        lines.append(f"  {'ok  ' if s['ok'] else 'FAIL'} {s['tool']}({args}) -> {str(s.get('result', ''))[:170]!r}{win}"
                     + (f" [by {s['brain']}]" if s.get("brain") and s["brain"] != who else ""))
    used = ep.get("used") or {}
    if used.get("playbook") or used.get("lesson"):
        names = names or {}
        parts = [("playbook", i) for i in used.get("playbook", [])] + [("rule", i) for i in used.get("lesson", [])]
        lines.append("  USED: " + "; ".join(f"{kind} #{i} {names.get(('lesson' if kind == 'rule' else kind, i), '')}".strip()
                                            for kind, i in parts))
    if ep["replies"]:
        lines.append("  reply: " + ep["replies"][-1][:200])
    if ep["flags"]:
        lines.append("  PROBLEM: " + "; ".join(dict.fromkeys(ep["flags"])))
    return "\n".join(lines)


def _priority(ep: dict) -> int:
    return (0 if ep["cloud_final"] and ep["steps"] else 1 if ep["flags"] and ep["steps"] else 2 if ep["steps"]
            else 3 if ep["flags"] else 4)


def build_batches(eps: list[dict], names: dict | None = None) -> tuple[list[tuple[str, dict]], int]:
    """([(text, {label: episode})...], how many episodes did not fit). Teaching examples first."""
    ordered = sorted(eps, key=lambda e: (_priority(e), e["t"]))
    batches: list[tuple[list[str], dict]] = []
    cur: list[str] = []
    cur_map: dict = {}
    size, skipped, n = 0, 0, 0
    for ep in ordered:
        n += 1
        label = f"E{n}"
        text = render_episode(label, ep, names)
        if size + len(text) > BATCH_CHARS and cur:
            batches.append((cur, cur_map))
            cur, cur_map, size = [], {}, 0
            if len(batches) >= MAX_BATCHES:
                skipped = len(ordered) - n + 1
                break
        cur.append(text)
        cur_map[label] = ep
        size += len(text)
    else:
        if cur:
            batches.append((cur, cur_map))
    return [("\n\n".join(t), m) for t, m in batches], skipped


def _catalog() -> str:
    return ", ".join(f"{t.name}({', '.join(t.params.get('properties', {}))})" for t in T.active_tools()
                     if not t.source.startswith(("skill:", "mcp:")))


def parse(raw: str) -> dict:
    s = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ choosing who reviews
def _reviewer(brain):
    """(brain object, name, is_cloud). Abacus first; then the main brain if it is a cloud one; then free cloud; then local."""
    if brain is None:
        return None, "", False
    b = getattr(brain, "backup", None)
    if b is not None and getattr(b, "cloud", False) and not getattr(b, "is_anthropic", False):
        return b, "the online AI (" + b.label + ")", True
    if getattr(brain, "cloud", False) and not getattr(brain, "is_anthropic", False):
        return brain, "the online AI (" + brain.label + ")", True
    fr = getattr(brain, "free_router", None)
    if fr is not None and brain._free_allowed():
        brain._count_free_use()
        return fr, "the free cloud AI (weaker than Abacus)", False
    local = brain._local_brain("")
    return (local, "the local AI (much weaker than Abacus)", False) if local is not None else (None, "", False)


# ------------------------------------------------------------------ testing a playbook on the local AI
def _next_tool(local, text: str, prior: list[dict]) -> str:
    """What the local AI would do NEXT, given the recorded conversation so far. Nothing is run."""
    from . import toolpick
    from .prompt import build_system_prompt

    names = toolpick.pick(text, [s["tool"] for s in prior])
    system = build_system_prompt(local.settings, None, False, False, user_text=text)
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": text + f"\n[clock: {datetime.now():%A %d %B %Y %H:%M}]"}]
    for j, s in enumerate(prior):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{j}", "type": "function", "function": {
            "name": s["tool"], "arguments": json.dumps(s.get("args") or {})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{j}", "content": str(s.get("result", ""))[:1500]})
    resp = local._create(local.client.chat.completions.create, model=local.settings.model, messages=msgs,
                         tools=T.openai_tool_specs(names))
    calls = getattr(resp.choices[0].message, "tool_calls", None) or []
    return T.ALIASES.get(calls[0].function.name, calls[0].function.name) if calls else ""


def evaluate(local, pb: dict, source_eps: list[dict]) -> dict:
    """Score the local AI on the recorded steps, without and with this playbook. {alone, with, steps, episodes}.
    Nothing is run, and nothing here is recorded as if it were a real request."""
    alone = withpb = total = used = 0
    with blackbox.quiet():
        for ep in source_eps[:MAX_EVAL_EPISODES]:
            good = [s for s in ep["steps"] if s["ok"]][:MAX_EVAL_STEPS]
            if not good:
                continue
            used += 1
            for k, step in enumerate(good):
                want = T.ALIASES.get(step["tool"], step["tool"])
                total += 1
                for score_with in (False, True):
                    with playbooks.testing([pb] if score_with else []):
                        try:
                            got = _next_tool(local, ep["text"], good[:k])
                        except Exception:
                            got = "?"
                    if got == want:
                        withpb += score_with
                        alone += not score_with
    return {"alone": alone, "with": withpb, "steps": total, "episodes": used}


def cases_of(eps: list[dict]) -> list[dict]:
    """The recorded jobs a playbook came from, kept with it so it can be re-tested long after the black box has rotated."""
    out = []
    for ep in eps[:MAX_EVAL_EPISODES]:
        steps = [{"tool": s["tool"], "args": s.get("args") or {}, "result": str(s.get("result", ""))[:600], "ok": True}
                 for s in ep["steps"] if s["ok"]][:MAX_EVAL_STEPS]
        if steps:
            out.append({"text": ep["text"], "steps": steps})
    return out

def verdict(ev: dict) -> str:
    if not ev["steps"]:
        return "unproven"
    if ev["alone"] >= ev["steps"]:
        return "redundant"  # the local AI already does it right: showing the playbook would only cost prompt space
    return "proven" if ev["with"] > ev["alone"] else "no_help"


# ------------------------------------------------------------------ the review
def run(ctx, brain=None, hours: float | None = None) -> str:
    if _RUN["running"]:
        return "A full review is already running."
    if not env_bool("DISTILL_ENABLED", True):
        return "The full review is switched off (DISTILL_ENABLED=false)."
    _RUN["running"] = True
    try:
        rep = _run(ctx, brain, hours)
        try:  # then check everything that was learned so far: real results, and a re-test on the local AI
            from . import verify

            extra = verify.run(ctx, brain)
        except Exception as e:
            extra = f"The check of what was learned failed: {type(e).__name__}: {str(e)[:120]}"
        if extra:
            rep = rep + "\n" + extra
            _report_path().write_text(rep, encoding="utf-8")
        return rep
    finally:
        _RUN["running"] = False


def _finish(rep: str) -> str:
    _report_path().write_text(rep, encoding="utf-8")
    return rep


def _run(ctx, brain, hours) -> str:
    from .credits import CreditsDenied, get_gate

    now = datetime.now()
    head = f"Full review - {now:%Y-%m-%d %H:%M}"
    last = "" if hours else _last_review()
    eps = [e for e in blackbox.episodes(hours or 72) if e["t"] > last]  # only what happened since the last review
    if len(eps) < 3:
        return _finish(f"{head}\nOnly {len(eps)} request(s) since the last review - not enough to study yet.")
    reviewer, who, cloud = _reviewer(brain)
    if reviewer is None:
        return _finish(f"{head}\nNo AI is available to do the review right now.")
    known = playbooks.load()
    names = {("playbook", p["id"]): f"'{p['title']}'" for p in known}
    names.update({("lesson", l["id"]): f"'{l['title']}'" for l in lessons.load(ctx)})
    batches, skipped = build_batches(eps, names)
    catalog = _catalog()
    max_pb = max(2, min(12, env_int("DISTILL_MAX_PLAYBOOKS", 60) // 5))
    system = SYSTEM.replace("{n}", str(max_pb))
    proposed: list[tuple[dict, dict]] = []  # (raw playbook, label -> episode map of its batch)
    rules: list[dict] = []
    graded: dict = {}  # episode id -> (correct, why), as judged by the online AI
    retire: list = []
    notes: list[str] = []
    errors: list[str] = []
    spent = 0.0
    estimate_chars = sum(len(t) for t, _ in batches) + len(system) * len(batches)

    def ask_all() -> None:
        for text, mapping in batches:
            user = (f"Tools you may use in steps: {catalog}\n\nExisting playbooks (update by reusing the title): "
                    + ("; ".join(f"#{p['id']} {p['title']}" for p in known[-40:]) or "(none)")
                    + f"\nExisting rules (do not repeat): " + ("; ".join(e["how"][:80] for e in lessons.load(ctx)[-20:]) or "(none)")
                    + "\n\nEPISODES:\n\n" + text)
            try:
                data = parse(reviewer._raw_complete(system, user))
            except CreditsDenied as e:
                errors.append(f"stopped: {e}")
                return
            except Exception as e:
                errors.append(f"{type(e).__name__}: {str(e)[:140]}")
                return
            proposed.extend((p, mapping) for p in (data.get("playbooks") or []) if isinstance(p, dict))
            rules.extend(x for x in (data.get("rules") or []) if isinstance(x, dict))
            retire.extend(data.get("retire") or [])
            for g in (data.get("grades") or [])[:60]:
                ep = mapping.get(str(g.get("episode", ""))) if isinstance(g, dict) else None
                if ep is not None and isinstance(g.get("correct"), bool):
                    graded[ep["id"]] = (g["correct"], " ".join(str(g.get("why", "")).split())[:160])
            if data.get("notes"):
                notes.append(str(data["notes"])[:300])

    if cloud:
        gate = get_gate()
        lo, hi = gate.estimate("research", "", estimate_chars)
        with gate.task(f"full review of {len(eps)} request(s)", "the online AI teaches the local AI from yesterday's work",
                       lo, hi, getattr(reviewer, "label", "cloud AI"), uncapped=True) as ok:
            if not ok:
                return _finish(f"{head}\nThe credit gate did not allow the review.")
            ask_all()
            spent = (gate.ticket or {}).get("spent", 0.0)
    else:
        ask_all()
    if errors and not proposed and not rules:
        return _finish(f"{head}\nThe review could not finish: {errors[0]}")
    # ---- save the playbooks
    saved, skipped_pb, source = [], [], getattr(reviewer, "label", "")
    for raw, mapping in proposed:
        raw = dict(raw)
        raw["from_turns"] = [mapping[x]["id"] for x in (raw.get("from_episodes") or []) if x in mapping]
        pb, why = playbooks.clean(raw, T.REGISTRY)
        if pb is None:
            skipped_pb.append(f"'{str(raw.get('title', ''))[:40]}' ({why})")
            continue
        saved.append(playbooks.add_or_update(pb, source))
    # ---- test them on the local AI (nothing is run), then switch on the ones that help
    by_turn = {e["id"]: e for e in eps}
    local = brain._local_brain("") if brain is not None else None
    results = {"proven": 0, "redundant": 0, "no_help": 0, "unproven": 0, "unchecked": 0}
    for pb in saved[:MAX_EVAL_PLAYBOOKS]:
        src = [by_turn[t] for t in pb.get("from_turns", []) if t in by_turn]
        if local is None or not src:
            results["unchecked" if local is None else "unproven"] += 1
            if local is not None:
                playbooks.update(pb["id"], status="unproven", enabled=False)
            continue
        cases = cases_of(src)
        ev = evaluate(local, pb, src)
        status = verdict(ev)
        results[status] += 1
        ev.update(model=getattr(local, "label", ""), checked=datetime.now().isoformat(timespec="seconds"))
        playbooks.update(pb["id"], status=status, enabled=status == "proven", eval=ev, cases=cases,
                         retested=datetime.now().strftime("%Y-%m-%d"))
    results["unchecked"] += max(0, len(saved) - MAX_EVAL_PLAYBOOKS)
    # ---- rules and retirements
    new_rules = []
    for item in rules[:5]:
        if len(new_rules) >= 3:
            break
        if evolve.acceptable(item, lessons.load(ctx)):
            continue
        new_rules.append(lessons.add(str(item.get("title", "")), " ".join(str(item["how"]).split()),
                                     str(item.get("when", ""))[:100], ctx, source="evolve"))
    lessons.prune_auto(env_int("EVOLVE_MAX_AUTO", 20), ctx)
    retired = 0
    for pid in retire[:5]:
        try:
            if playbooks.update(int(pid), enabled=False, status="no_help"):
                retired += 1
        except (TypeError, ValueError):
            pass
    dropped = playbooks.prune(env_int("DISTILL_MAX_PLAYBOOKS", 60))
    from . import verify

    counted = verify.record(eps, graded)  # how did the requests that used a playbook or rule really go?
    try:
        _state_path().write_text(json.dumps({"last": now.isoformat(timespec="seconds")}), encoding="utf-8")
    except OSError:
        pass
    st = blackbox.stats(hours or 24)
    lines = [head, f"Studied {len(eps)} request(s) ({sum(len(e['steps']) for e in eps)} actions) with {who}"
             + (f", using {spent:g} credits (no Friday cap)." if cloud else ".")]
    if skipped:
        lines.append(f"{skipped} more request(s) did not fit in this review.")
    if counted:
        wrong = sum(1 for ep in eps if ep["id"] in graded and not graded[ep["id"]][0] and (ep.get("used") or {}).get("playbook"))
        lines.append(f"Graded {counted} request(s) where a playbook or rule was used" + (f"; {wrong} went wrong." if wrong else "."))
    if notes:
        lines.append("Overall: " + " ".join(notes)[:400])
    if saved:
        lines.append(f"Wrote {len(saved)} playbook(s): {results['proven']} proven and switched on, "
                     f"{results['redundant']} the local AI already does right, {results['no_help']} didn't help, "
                     f"{results['unproven']} couldn't be tested, {results['unchecked']} waiting to be tested.")
        for p in playbooks.load():
            if p["id"] in {s["id"] for s in saved}:
                ev = p.get("eval") or {}
                lines.append(f"  #{p['id']} {p['title']} [{p['status']}]"
                             + (f" local AI alone {ev['alone']}/{ev['steps']} steps, with it {ev['with']}/{ev['steps']}" if ev.get("steps") else ""))
    else:
        lines.append("No new playbooks this time.")
    if new_rules:
        lines.append(f"Learned {len(new_rules)} rule(s): " + " | ".join(f"#{r['id']} {r['how']}" for r in new_rules))
    if retired:
        lines.append(f"Switched off {retired} playbook(s) the log showed were wrong.")
    if dropped:
        lines.append(f"Removed {dropped} old playbook(s) to stay under the limit.")
    if skipped_pb:
        lines.append("Skipped: " + "; ".join(skipped_pb[:5]))
    if errors:
        lines.append("Stopped early: " + errors[0])
    rep = "\n".join(lines)
    activity.log("distill", f"Full review: {len(saved)} playbook(s), {results['proven']} proven, {len(new_rules)} rule(s), "
                            f"{spent:g} credits")
    return _finish(rep)


# ------------------------------------------------------------------ tools
@T.tool("review_everything", "Full review: the online AI studies everything Jarvis did since the last review (every tool, "
        "key press, command and answer) and teaches the local AI playbooks and rules for next time. Uses Abacus credits "
        "(no limit set by Friday). Takes a few minutes. Say 'review everything' or 'learn from the online AI'.")
def review_everything(ctx):
    def go():
        report = run(ctx, getattr(ctx, "brain", None))
        ctx.speak("The full review is finished. " + (report.splitlines()[1] if len(report.splitlines()) > 1 else report))
    if _RUN["running"]:
        return "A full review is already running."
    threading.Thread(target=go, daemon=True, name="distill").start()
    return "Started the full review. I'll tell you when it's done."


@T.tool("list_playbooks", "List the playbooks the local AI learned from the online AI: what each is for, and whether it "
        "was proven to help.")
def list_playbooks(ctx):
    items = playbooks.load()
    if not items:
        return "No playbooks yet. They are written by the full review ('review everything')."
    return "\n".join(f"#{p['id']} {p['title']} [{p['status']}{', on' if p.get('enabled') else ', off'}]: "
                     + " -> ".join(s["tool"] for s in p["steps"]) for p in items)


@T.tool("forget_playbook", "Delete a playbook by its number or part of its title.",
        {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})
def forget_playbook(ctx, which: str):
    n = playbooks.remove(which)
    return f"Forgot {n} playbook(s)." if n else f"No playbook matched '{which}'."
