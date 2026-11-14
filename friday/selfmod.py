"""Jarvis changes its own code - safely.

"Jarvis, change yourself so that ..." runs this pipeline (change_myself):
  1. the AI drafts the change as exact search/replace edits on a COPY of the program (never the live files),
  2. the draft is checked: only allowed files, no safety-critical files, no risky code patterns, it must compile,
  3. the whole test suite runs on the copy,
  4. you get a plain-English summary + the test result and must APPROVE (voice, dashboard or phone),
  5. only then are the files backed up and replaced, and Jarvis restarts,
  6. if the new version doesn't come up healthy, a separate watcher (scripts/selfmod_guard.py) puts the old files back.
undo_last_change puts the previous version back on request.
Files that guard safety (approvals, credits, logins, sending messages, this file) can never be changed this way."""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import activity, jobs
from .env import data_dir, env, env_int
from .tools import tool

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_PREFIXES = ("friday/", "tests/")
PROTECTED = {  # these decide what Jarvis may do or send, so Jarvis may not rewrite them (use Claude Code for these)
    "friday/selfmod.py", "friday/autonomy.py", "friday/credits.py", "friday/skills.py", "friday/dashboard.py",
    "friday/local_api.py", "friday/settings_store.py", "friday/env.py", "friday/main.py", "friday/sms.py",
    "friday/email_tools.py", "friday/sandbox.py", "friday/admin.py", "friday/notify.py", "friday/__init__.py",
    "friday/__main__.py", "friday/config.py", "friday/tools.py",
    # added later: the permission prompt itself, the honesty guards, the secret-blanking record, the review that spends
    # credits without a Friday-side cap, the phone remote, and the tool that edits settings
    "friday/approvals.py", "friday/guards.py", "friday/blackbox.py", "friday/distill.py", "friday/phone.py",
    "friday/selfsettings.py",
}
RISKY = re.compile(r"\beval\(|\bexec\(|os\.system|shell\s*=\s*True|\bctypes\b|\bwinreg\b|__import__|\bimportlib\b|"
                   r"['\"]\.env['\"]|\.env\b.{0,20}(?:read|open)|AUTONOMY_LEVEL|NEVER_TOOLS|dangerous\s*=\s*False|"
                   r"\bconfirm\s*=|rmtree|os\.remove|\.unlink\(|shutil\.move", re.I)
COPY_DIRS = ("friday", "tests", "scripts")
COPY_FILES = (".env.example", "requirements.txt", "requirements-dev.txt", "README.md", "CLAUDE.md")

SYSTEM = (
    "You are the lead engineer of Jarvis, a Windows voice assistant written in Python. You change its OWN source code. "
    "Reply with ONE JSON object and nothing else:\n"
    '{"explanation": "plain English, 1-3 sentences: what changes and why", '
    '"edits": [{"path": "friday/xxx.py", "old": "EXACT text that exists once in the file", "new": "replacement text"}], '
    '"new_files": [{"path": "friday/yyy.py", "content": "full file"}], '
    '"new_tests": [{"path": "tests/test_xxx.py", "content": "full pytest file"}]}\n'
    "Rules: keep changes small and focused; 'old' must match the file EXACTLY (copy it) and appear only once; "
    "only touch friday/ and tests/; never touch approvals, credits, logins, sending messages or .env; add a test for "
    "new behaviour. friday/tools.py, main.py, config.py and settings_store.py are protected, so change behaviour inside "
    "modules like llm.py, quick.py, toolpick.py, lessons.py, voice.py, listening.py, webcontext.py, agenda.py, routines.py or "
    "the static pages. If a plain number or switch would fix it (a threshold, a time, a speed), do NOT edit code: reply with an "
    "explanation that says to use the change_setting tool with that setting name. If you need to read other files before you can "
    "write exact edits, reply with ONLY {\"need_files\": [\"friday/voice.py\"]} (at most 4 paths) and you will be given them. "
    'If the request can\'t be done within these rules reply {"explanation": "why not", "edits": [], "new_files": [], '
    '"new_tests": []}.')


def _dir() -> Path:
    d = Path(data_dir()) / "selfmod"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _dir() / "state.json"


def state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    _state_path().write_text(json.dumps(s, indent=1), encoding="utf-8")


def _log(entry: dict) -> None:
    with open(_dir() / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), **entry}) + "\n")


# ------------------------------------------------------------------ reading the model's answer
def parse_proposal(text: str) -> dict:
    """The JSON object in the AI's reply (it may be wrapped in a code fence or chatter). Raises ValueError if unusable."""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise ValueError("the AI didn't answer with a change in the expected format")
        t = t[a:b + 1]
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError("the AI's answer isn't a change description")
    for k in ("edits", "new_files", "new_tests"):
        data[k] = [x for x in (data.get(k) or []) if isinstance(x, dict)]
    data["explanation"] = str(data.get("explanation") or "").strip()[:600]
    data["need_files"] = [str(x) for x in (data.get("need_files") or []) if isinstance(x, str)][:4]
    return data


def check_path(path: str) -> str:
    """'' if Jarvis may edit this file, else the reason."""
    p = str(path or "").replace("\\", "/").strip("/")
    if not p or ".." in p.split("/") or ":" in p:
        return f"{path!r} isn't a valid project path"
    if p in PROTECTED:
        return f"{p} guards Jarvis's safety (approvals, credits, logins, sending) so it can't be changed by Jarvis itself"
    if not p.startswith(ALLOWED_PREFIXES):
        return f"{p} is outside the folders Jarvis may change (friday/ and tests/)"
    if not p.endswith((".py", ".html", ".css", ".js", ".md")):
        return f"{p}: only code and page files can be changed"
    return ""


def apply_to(root: Path, proposal: dict) -> tuple[list[str], list[str], str]:
    """Apply the proposed edits under root. Returns (modified, created, problem). Nothing is changed if anything fails."""
    plan: dict[str, str] = {}
    created: list[str] = []
    for e in proposal["edits"]:
        why = check_path(e.get("path", ""))
        if why:
            return [], [], why
        rel = str(e["path"]).replace("\\", "/").strip("/")
        f = root / rel
        if not f.exists():
            return [], [], f"{rel} doesn't exist (use new_files to add a file)"
        text = plan.get(rel, f.read_text(encoding="utf-8"))
        old, new = str(e.get("old", "")), str(e.get("new", ""))
        if not old:
            return [], [], f"an edit in {rel} has no 'old' text"
        if text.count(old) != 1:
            first = old.strip().splitlines()[0] if old.strip() else ""
            close = difflib.get_close_matches(first.strip(), [l.strip() for l in text.splitlines()], n=2, cutoff=0.5)
            hint = (" The closest real lines in that file are: " + " | ".join(close)) if close else (
                f" Nothing like that is in {rel}; the text may live in a different file.")
            return [], [], f"the text to replace appears {text.count(old)} times in {rel} (must be exactly once).{hint}"
        plan[rel] = text.replace(old, new, 1)
    for n in [*proposal["new_files"], *proposal["new_tests"]]:
        why = check_path(n.get("path", ""))
        if why:
            return [], [], why
        rel = str(n["path"]).replace("\\", "/").strip("/")
        if (root / rel).exists():
            return [], [], f"{rel} already exists (use an edit instead)"
        plan[rel] = str(n.get("content", ""))
        created.append(rel)
    if not plan:
        return [], [], "the AI proposed no changes"
    if len(plan) > env_int("SELFMOD_MAX_FILES", 4):
        return [], [], f"the change touches {len(plan)} files, more than the {env_int('SELFMOD_MAX_FILES', 4)} allowed"
    for rel, text in plan.items():
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as ex:
                return [], [], f"{rel} would not compile: {ex.msg} (line {ex.lineno})"
    for rel, text in plan.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return [r for r in plan if r not in created], created, ""


def risky_lines(old_root: Path, new_root: Path, modified: list[str], created: list[str]) -> list[str]:
    """Added lines that match risky patterns (compared with the live code)."""
    bad = []
    for rel in [*modified, *created]:
        new = (new_root / rel).read_text(encoding="utf-8").splitlines()
        old = set((old_root / rel).read_text(encoding="utf-8").splitlines()) if (old_root / rel).exists() else set()
        for line in new:
            if line not in old and RISKY.search(line):
                bad.append(f"{rel}: {line.strip()[:100]}")
    return bad


def make_diff(old_root: Path, new_root: Path, modified: list[str], created: list[str]) -> str:
    out = []
    for rel in [*modified, *created]:
        a = (old_root / rel).read_text(encoding="utf-8").splitlines() if (old_root / rel).exists() else []
        b = (new_root / rel).read_text(encoding="utf-8").splitlines()
        out += list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", lineterm="", n=2))
    return "\n".join(out)


# ------------------------------------------------------------------ workspace + tests
def remove_tree(path: Path) -> None:
    """Delete a work folder even if Windows marked files read-only or a test left a file open for a moment."""
    def fix(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    for _ in range(3):
        shutil.rmtree(path, onerror=fix)
        if not Path(path).exists():
            return
        time.sleep(0.5)


def make_workspace(src: Path | None = None) -> Path:
    src = src or ROOT
    work = Path(tempfile.mkdtemp(prefix="jarvis-selfmod-", dir=str(_dir())))
    for d in COPY_DIRS:
        if (src / d).exists():
            shutil.copytree(src / d, work / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in src.iterdir():  # launchers, requirements, notes (never .env, data, keys or the virtual environment)
        if f.is_file() and (f.suffix.lower() in (".bat", ".ps1", ".md", ".txt") or f.name == ".env.example"):
            shutil.copy2(f, work / f.name)
    return work


def run_tests(work: Path, timeout: int = 420) -> tuple[bool, str]:
    # A CLEAN environment: only what Windows and Python need. Jarvis's own settings (from .env) must not leak into the
    # tests - they changed how some tests behave and made good changes look broken.
    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE",
            "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS", "OS", "USERNAME")
    envv = {k: v for k, v in os.environ.items() if k.upper() in keep}
    envv.update(PYTHONPATH=str(work), PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"], cwd=str(work),
                           capture_output=True, text=True, timeout=timeout, env=envv, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "the tests took too long (over 7 minutes)"
    tail = " ".join((p.stdout or "").strip().splitlines()[-3:])[:300]
    return p.returncode == 0, tail


def _context_files(request: str, root: Path | None = None) -> str:
    root = root or ROOT
    words = {w for w in re.findall(r"[a-z]{4,}", request.lower())} - {"that", "this", "with", "from", "make", "have", "when",
                                                                      "your", "jarvis", "change", "yourself", "should"}
    # Text the owner quotes ('Yes, sir?') is the best clue: files that literally contain it come first.
    quoted = [q.strip() for q in re.findall(r"['\"â€œâ€˜]([^'\"â€â€™]{4,80})['\"â€â€™]", request)]
    scored = []
    for f in sorted((root / "friday").glob("*.py")):
        rel = f"friday/{f.name}"
        if rel in PROTECTED:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        literal = sum(text.count(q) for q in quoted)
        score = sum(text.lower().count(w) for w in words)
        if literal or score:
            scored.append((literal * 1000 + score, rel, text, quoted if literal else []))
    scored.sort(key=lambda s: s[0], reverse=True)
    parts = []
    for _, rel, text, exact in scored[:3]:
        if len(text) > 9000 or exact:
            lines = text.splitlines()
            keys = exact or list(words)
            hits = [i for i, l in enumerate(lines) if any(w.lower() in l.lower() for w in keys)][:6]
            keep = sorted({j for i in hits for j in range(max(0, i - 15), min(len(lines), i + 15))})
            text = "\n".join(lines[j] for j in keep)
        parts.append(f"=== {rel} (copy 'old' text from here EXACTLY) ===\n{text}")
    layout = ""
    try:
        layout = (root / "CLAUDE.md").read_text(encoding="utf-8")[:2500]
    except OSError:
        pass
    return f"Project notes:\n{layout}\n\nRelevant files:\n" + "\n\n".join(parts)


def module_index(root: Path | None = None) -> str:
    """One line per source file (first line of its docstring), so the AI can tell where something lives and ask for it."""
    root = root or ROOT
    rows = []
    for f in sorted((root / "friday").glob("*.py")):
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:600].lstrip()
        except OSError:
            continue
        body = head[3:].strip() if head.startswith(('"' * 3, "'" * 3)) else ""
        doc = body.splitlines()[0] if body else ""
        mark = " (protected: can't be edited by me)" if f"friday/{f.name}" in PROTECTED else ""
        rows.append(f"friday/{f.name}: {doc[:90]}{mark}")
    return "\n".join(rows)


def read_requested(paths: list[str], root: Path | None = None, limit: int = 12000) -> str:
    """The files the AI asked to read (code and pages under friday/, tests/, scripts/ only; never .env or data)."""
    root = root or ROOT
    out = []
    for rel in paths[:4]:
        p = str(rel).replace("\\", "/").strip("/")
        if (not p.startswith(("friday/", "tests/", "scripts/")) or ".." in p.split("/") or ":" in p
                or not p.endswith((".py", ".html", ".css", ".js", ".md"))):
            out.append(f"=== {rel}: not a file I can show ===")
            continue
        f = root / p
        if not f.is_file():
            out.append(f"=== {p}: doesn't exist ===")
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        note = "" if len(text) <= limit else f" - first {limit} characters"
        out.append(f"=== {p} (copy 'old' text from here EXACTLY){note} ===\n" + text[:limit])
    return "\n\n".join(out)


def _recent_problems() -> str:
    """What Jarvis logged as errors lately: often the quickest clue to what the owner means by 'fix it'."""
    try:
        rows = activity.recent(300, kind="error")[-6:]
    except Exception:
        rows = []
    return "\n".join(f"- {str(r.get('time', ''))[5:16]} {str(r.get('message', ''))[:160]}" for r in rows)


def draft(ctx, request: str, strong: bool = False, feedback: str = "") -> dict:
    ask = getattr(ctx, "ask_ai", None)
    if ask is None:
        raise RuntimeError("the AI brain isn't available in this mode")
    user = (f"Change request from the owner: {request}\n\nRecent errors Jarvis logged (may show what is broken):\n"
            f"{_recent_problems() or '(none)'}\n\nAll source files:\n{module_index()}\n\n{_context_files(request)}\n")
    if feedback:
        user += f"\nYour previous draft failed:\n{feedback}\nFix it and answer again with the full JSON.\n"

    def go(text):
        return ask(SYSTEM, text, prefer="cloud", task="change my own code") if strong else ask(SYSTEM, text)

    reply, who = go(user)
    prop = parse_proposal(reply)
    if prop["need_files"] and not (prop["edits"] or prop["new_files"] or prop["new_tests"]):
        # it wants to read more code first (the files picked by keywords missed): show them, once, and let it answer again
        reply, who = go(user + "\n\nFiles you asked to read:\n" + read_requested(prop["need_files"]) +
                        "\n\nNow answer with the full JSON change (no more need_files).")
        prop = parse_proposal(reply)
    prop["who"] = who
    return prop


# ------------------------------------------------------------------ the whole flow
def propose(ctx, request: str, strong: bool, progress=lambda t: None) -> tuple[dict | None, Path | None, str]:
    """(proposal, workspace, message). Tries twice: the second time the AI sees why the first draft failed."""
    feedback = ""
    for attempt in (1, 2):
        progress(f"drafting the change (try {attempt})")
        try:  # the second try uses the strongest AI (the Credit Gate asks you first), since the free one missed
            prop = draft(ctx, request, strong or attempt == 2, feedback)
        except Exception as e:
            return None, None, f"Error: I couldn't get a draft from the AI ({type(e).__name__}: {str(e)[:150]})."
        if not prop["edits"] and not prop["new_files"] and not prop["new_tests"]:
            return None, None, "I can't do that one safely: " + (prop["explanation"] or "no change was proposed") + "."
        work = make_workspace()
        modified, created, problem = apply_to(work, prop)
        if not problem:
            bad = risky_lines(ROOT, work, modified, created)
            if bad:
                problem = "the draft uses code I don't allow myself to write: " + "; ".join(bad[:3])
        if not problem:
            progress("running the full test suite on the copy")
            ok, tail = run_tests(work)
            if ok:
                prop.update(modified=modified, created=created, tests=tail)
                return prop, work, ""
            problem = "the tests failed on the copy: " + tail
        remove_tree(work)
        feedback = problem
        _log({"event": "draft_rejected", "request": request, "why": problem[:300]})
    return None, None, f"Error: after two tries the change still didn't pass my checks ({feedback[:200]}). Nothing was changed."


def apply_live(prop: dict, work: Path, request: str) -> str:
    """Back up what is about to change, copy the checked files over the live ones, arm the safety watcher."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(data_dir()) / "backups" / f"code-{stamp}"
    for rel in prop["modified"]:
        (backup / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, backup / rel)
    for rel in [*prop["modified"], *prop["created"]]:
        (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / rel, ROOT / rel)
    _save_state({"status": "applied", "verified": False, "time": time.time(), "request": request[:300],
                 "backup": str(backup), "modified": prop["modified"], "created": prop["created"],
                 "explanation": prop["explanation"]})
    _log({"event": "applied", "request": request, "files": [*prop["modified"], *prop["created"]], "backup": str(backup)})
    activity.log("selfmod", f"Changed my own code: {prop['explanation'][:160]} ({', '.join(prop['modified'] + prop['created'])})")
    return str(backup)


def restore(st: dict | None = None) -> str:
    """Put the previous version back (files backed up by apply_live; files it created are removed)."""
    st = st or state()
    if not st or st.get("status") not in ("applied", "ok"):
        return "There's no recent self-change to undo."
    backup = Path(st["backup"])
    for rel in st.get("modified", []):
        if (backup / rel).exists():
            shutil.copy2(backup / rel, ROOT / rel)
    for rel in st.get("created", []):
        try:
            (ROOT / rel).unlink()
        except OSError:
            pass
    st["status"] = "undone"
    _save_state(st)
    _log({"event": "undone", "files": st.get("modified", []) + st.get("created", [])})
    return "Put my previous version back: " + ", ".join(st.get("modified", []) + st.get("created", []))


def arm_watcher(minutes: float = 3) -> None:
    """A separate process that waits, and if Jarvis didn't come up healthy by then, restores the old version and relaunches."""
    guard = ROOT / "scripts" / "selfmod_guard.py"
    if not guard.exists():
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, str(guard), "--watch", str(int(minutes * 60))], cwd=str(ROOT), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mark_verified() -> None:
    """Called a while after startup: this version came up fine, so the watcher stands down."""
    st = state()
    if st.get("status") == "applied" and not st.get("verified"):
        st.update(verified=True, status="ok")
        _save_state(st)
        activity.log("selfmod", "The changed version is running fine; the safety watcher stood down.")


def summary_for_owner(prop: dict) -> str:
    files = ", ".join(prop["modified"] + prop["created"])
    return (f"{prop['explanation'] or 'A change to my own code.'} Files: {files}. All tests passed on a copy "
            f"({prop.get('tests', '')[:80]}). Approve to apply it and restart; I keep a backup and undo it if I don't start properly.")


@tool("change_myself", "Fix or change Jarvis's OWN code when the owner asks ('fix that', 'change yourself so that ...', "
      "'reprogram yourself to ...', or when a setting can't solve it). The strongest AI (Abacus) drafts the change on a copy, "
      "reads whatever files it needs, runs every test, and only then asks the owner to approve before anything is applied; a "
      "backup is kept and the old version comes back by itself if the new one doesn't start. Safety files can't be changed. "
      "Say what should be fixed or changed in plain words. If a number or switch would fix it, use change_setting instead.",
      {"type": "object", "properties": {"request": {"type": "string"}, "strong": {"type": "boolean"}},
       "required": ["request"]})
def change_myself(ctx, request: str, strong: bool = False):
    if env("SELFMOD_ENABLED", "true").lower() in ("0", "false", "no", "off"):
        return "Self-changes are switched off (SELFMOD_ENABLED)."
    request = (request or "").strip()
    if len(request) < 12:
        return "Tell me what the change should do, in a sentence or two."

    def work(progress):
        prop, workdir, msg = propose(ctx, request, True, progress)  # always the strongest AI: Abacus has no limit here
        if prop is None:
            return msg
        try:
            diff = make_diff(ROOT, workdir, prop["modified"], prop["created"])
            (_dir() / "last_proposal.diff").write_text(diff, encoding="utf-8")
            progress("waiting for your approval")
            if not ctx.confirm("Approve changing my own code? " + summary_for_owner(prop)):
                _log({"event": "declined", "request": request})
                return "Okay, I did not change anything. (The draft diff is saved in data/selfmod/last_proposal.diff.)"
            arm = apply_live(prop, workdir, request)
            arm_watcher()
            if getattr(ctx, "control", None):
                ctx.control("restart_friday")
            return f"Done: {prop['explanation']} Backup: {arm}. Restarting now; if I don't come back healthy I restore the old version."
        finally:
            remove_tree(workdir)

    job = jobs.start("change my own code: " + request[:50], work, notify=getattr(ctx, "speak", None))
    return (f"On it. I'll draft '{request[:80]}' on a copy, test everything, and ask you before applying "
            f"anything (job {job.id}).")


@tool("undo_last_change", "Put back the version of Jarvis from before the last self-change. Asks first and restarts.",
      {"type": "object", "properties": {}}, dangerous=True, risk="system")
def undo_last_change(ctx):
    st = state()
    if st.get("status") not in ("applied", "ok"):
        return "There's no recent self-change to undo."
    out = restore(st)
    if getattr(ctx, "control", None):
        ctx.control("restart_friday")
    return out + " Restarting."


@tool("list_self_changes", "Show the changes Jarvis made to its own code (read-only).", {"type": "object", "properties": {}})
def list_self_changes(ctx):
    try:
        rows = [json.loads(x) for x in (_dir() / "history.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return "I haven't changed my own code yet."
    rows = [r for r in rows if r.get("event") in ("applied", "undone", "rolled_back")][-8:]
    return "\n".join(f"{r['time'][:16].replace('T', ' ')} {r['event']}: " + (r.get("request") or ", ".join(r.get("files", [])))[:100]
                     for r in rows) or "I haven't changed my own code yet."
