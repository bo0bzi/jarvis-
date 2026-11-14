"""Projects: jobs Jarvis works on in the background while you keep talking to him, several at once.

"Jarvis, in the background, research the best price for fork seals" -> start_project. Each project gets its own worker: its own
Abacus conversation (so it never blocks the chat or the other projects), every tool, and its own credit task. When it finishes he
tells you (update: phone + spoken at a good moment); you can ask what is running, read the result, or cancel it.
Rules that keep it safe and sane:
  * at most PROJECTS_MAX_PARALLEL (default 3) run at the same time; more wait in line,
  * permissions work as always, but ONE question at a time ("Project <title>: ..."), whoever asks,
  * a project may NOT use the keyboard, mouse, screen or browser unless it was started with needs_screen=true; then it holds the
    screen alone until it ends, and everyone else's key presses / clicks are refused with a clear message meanwhile,
  * projects cannot start projects, change settings or code, or switch the AI / power off the PC,
  * every step is recorded in the black box like any request, so the local AI can learn from them at night.
State: data/projects.json (the last 40; one that was running when Friday restarted shows as 'interrupted')."""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import activity, blackbox
from . import tools as T
from .env import data_dir, env_int
from .guards import STUCK_REPLY
from .memory import Memory
from .tools import tool

CONTROL_TOOLS = {"press_keys", "keyboard_type", "mouse_click", "take_screenshot", "look_at_screen", "read_screen_text",
                 "allow_computer_control", "stop_computer_control", "open_app", "close_app", "clipboard"}
NEVER = {"start_project", "change_myself", "undo_last_change", "change_setting", "undo_setting_change", "set_brain_mode",
         "switch_local_model", "power_action", "restore_backup", "install_nightly_fixes"}
MAX_ROUNDS = 6        # each round = up to 12 tool steps; a project that is still going after that stops and reports
MAX_QUEUED = 12
KEEP = 40
_TL = threading.local()


def is_control(name: str) -> bool:
    return name in CONTROL_TOOLS or name.startswith("browser_")


def allowed_tools(needs_screen: bool) -> list[str]:
    return [t.name for t in T.active_tools() if t.name not in NEVER and (needs_screen or not is_control(t.name))]


class ProjectCancelled(Exception):
    pass


class ProjectMemory(Memory):
    """A project's own conversation, kept in memory only. It sees the owner's long-term facts, and what it remembers
    (remember_fact) goes to the owner's real memory."""

    def __init__(self, main):
        self._main = main
        self._lock = threading.Lock()
        self.messages: list[dict] = []
        self.max_messages = 60
        self.path = getattr(main, "path", Path(data_dir()) / "memory.json")

    @property
    def facts(self) -> list[str]:
        return self._main.facts if self._main is not None else []

    def add(self, role: str, content: str) -> None:
        with self._lock:
            self.messages.append({"role": role, "content": content, "time": datetime.now().isoformat(timespec="seconds")})
            self.messages = self.messages[-self.max_messages:]
            while self.messages and self.messages[0]["role"] != "user":
                self.messages.pop(0)

    def save(self) -> None:
        pass

    def remember(self, fact: str) -> None:
        if self._main is not None:
            self._main.remember(fact)

    def forget(self, text: str) -> int:
        return self._main.forget(text) if self._main is not None else 0

    def clear(self) -> None:
        self.messages = []


class Project:
    def __init__(self, pid: int, title: str, request: str, needs_screen: bool = False):
        self.id, self.title, self.request, self.needs_screen = pid, title, request, needs_screen
        self.status = "queued"          # queued / waiting for the screen / running / done / failed / cancelled / interrupted
        self.progress = "waiting for a free slot"
        self.steps: list[str] = []
        self.result = self.error = ""
        self.created, self.started, self.finished = time.time(), 0.0, 0.0
        self.cancel = threading.Event()
        self.brain_label = ""

    @property
    def active(self) -> bool:
        return self.status in ("queued", "waiting for the screen", "running")

    def add_step(self, text: str) -> None:
        self.steps = (self.steps + [f"{datetime.now():%H:%M:%S} {text}"])[-40:]
        self.progress = text

    def to_dict(self) -> dict:
        end = self.finished or time.time()
        return {"id": self.id, "title": self.title, "request": self.request, "needs_screen": self.needs_screen,
                "status": self.status, "progress": self.progress, "steps": self.steps[-12:], "result": self.result,
                "error": self.error, "created": self.created, "started": self.started, "finished": self.finished,
                "seconds": int(end - self.started) if self.started else 0, "brain": self.brain_label}

    def line(self) -> str:
        secs = int((self.finished or time.time()) - (self.started or self.created))
        if self.active:
            return f"Project #{self.id} '{self.title}': {self.status.upper()} for {secs}s - {self.progress}"
        return f"Project #{self.id} '{self.title}': {self.status.upper()} after {secs}s - {(self.result or self.error)[:240]}"


def _brain_class():
    from .llm import Brain  # imported when needed: llm imports tools, and tools imports this module

    class ProjectBrain(Brain):
        allowed: list[str] | None = None
        blocked: frozenset = frozenset()
        no_prefetch = True  # the web is read with the tools it decides to use, not by searching the whole brief up front

        def _tool_names(self, user_text: str):
            return self.allowed

        def _run_tool(self, name: str, args):
            if name in self.blocked:
                return (f"Error: the {name} tool is not available in this background project"
                        + (" (it needs the screen: the owner can start a project with needs_screen)." if is_control(name) else "."))
            return super()._run_tool(name, args)

    return ProjectBrain


def make_brain(ctx, proj: Project, on_tool, on_trace):
    """The worker's own Abacus brain. Replaced in tests."""
    main = getattr(ctx, "brain", None)
    base = getattr(main, "backup", None)
    if base is not None:
        settings = base.settings
    elif main is not None and getattr(main, "cloud", False) and not getattr(main, "is_anthropic", False):
        settings = main.settings
    else:
        raise RuntimeError("there is no online AI (Abacus) set up to work on projects")

    def ask(prompt: str) -> bool:
        if proj.cancel.is_set():
            return False
        before, proj.progress = proj.progress, "waiting for your permission"
        try:
            return bool(ctx.confirm(f"Project {proj.title}: {prompt}"))
        finally:
            proj.progress = before

    ctx2 = dataclasses.replace(ctx, confirm=ask)  # a copy: the new brain must not take over the owner's shared context
    brain = _brain_class()(settings, ProjectMemory(getattr(ctx, "memory", None)), ctx2, on_tool=on_tool,
                           usage=getattr(main, "usage", None), on_trace=on_trace)
    brain.sub = True
    brain.allowed = allowed_tools(proj.needs_screen)
    brain.blocked = frozenset(t.name for t in T.REGISTRY.values()) - frozenset(brain.allowed)
    return brain


def _short(args) -> str:
    if not isinstance(args, dict):
        return ""
    return ", ".join(f"{k}={str(v)[:50]!r}" for k, v in list(args.items())[:3])


class Manager:
    def __init__(self):
        self._cv = threading.Condition()
        self._active = 0
        self._screen = threading.Lock()
        self.screen_owner: Project | None = None
        self.projects: list[Project] = []
        self._loaded = False
        self._io = threading.Lock()

    # ---------------------------------------------------------------- storage
    def _path(self) -> Path:
        return data_dir() / "projects.json"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            rows = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        for r in rows[-KEEP:]:
            p = Project(int(r["id"]), r["title"], r.get("request", ""), bool(r.get("needs_screen")))
            p.status, p.progress, p.result, p.error = r.get("status", "done"), r.get("progress", ""), r.get("result", ""), r.get("error", "")
            p.created, p.started, p.finished, p.brain_label = r.get("created", 0), r.get("started", 0), r.get("finished", 0), r.get("brain", "")
            p.steps = list(r.get("steps", []))
            if p.active:
                p.status, p.error, p.finished = "interrupted", "Friday was restarted while this was running. Ask me to run it again.", time.time()
            self.projects.append(p)

    def _save(self) -> None:
        """Written all at once (temp file, then replace) so a reader never sees half a file; one writer at a time."""
        try:
            data = json.dumps([p.to_dict() for p in list(self.projects[-KEEP:])], indent=1)
            with self._io:
                tmp = self._path().with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(self._path())
        except OSError:
            pass

    # ---------------------------------------------------------------- API
    def start(self, ctx, title: str, request: str, needs_screen: bool = False) -> Project:
        title, request = " ".join((title or "").split())[:80], (request or "").strip()
        if len(title) < 3 or len(request) < 10:
            raise ValueError("Give the project a short title and say what it should do (a sentence or two).")
        with self._cv:
            self._ensure_loaded()
            if sum(1 for p in self.projects if p.active) >= MAX_QUEUED:
                raise RuntimeError(f"there are already {MAX_QUEUED} projects waiting or running; cancel one first")
            p = Project(max([x.id for x in self.projects] + [0]) + 1, title, request, needs_screen)
            self.projects.append(p)
            self._save()
        activity.log("project", f"Started project #{p.id}: {title}")
        threading.Thread(target=self._worker, args=(p, ctx), daemon=True, name=f"project-{p.id}").start()
        return p

    def find(self, which) -> Project | None:
        self._ensure_loaded()
        key = str(which or "").strip().lower().lstrip("#")
        if not key:
            active = [p for p in self.projects if p.active]
            return active[-1] if len(active) == 1 else None
        for p in reversed(self.projects):
            if str(p.id) == key or key in p.title.lower():
                return p
        return None

    def cancel(self, which) -> str:
        p = self.find(which)
        if p is None:
            return f"No project matches '{which}'."
        if not p.active:
            return f"Project #{p.id} '{p.title}' is already {p.status}."
        with self._cv:
            was_queued = p.status == "queued"  # decided before the worker can react: a running one may stop at once
            p.cancel.set()
            if was_queued:
                p.status, p.progress, p.finished = "cancelled", "cancelled before it started", time.time()
                self._save()
            self._cv.notify_all()
        activity.log("project", f"Cancelled project #{p.id}: {p.title}")
        return f"Cancelling project #{p.id} '{p.title}'." + ("" if was_queued else " It stops at its next step.")

    def snapshot(self, n: int = 15) -> list[dict]:
        self._ensure_loaded()
        return [p.to_dict() for p in self.projects[-n:]][::-1]

    def prompt_section(self) -> str:
        self._ensure_loaded()
        live = [p for p in self.projects if p.active]
        if not live:
            return ""
        return "\nBackground projects (the TRUE status - report from this, never guess):\n" + "\n".join(p.line() for p in live[:6])

    # ---------------------------------------------------------------- the worker
    def _worker(self, p: Project, ctx) -> None:
        _TL.project = p
        held_screen = False
        try:
            with self._cv:
                while not p.cancel.is_set() and self._active >= max(1, env_int("PROJECTS_MAX_PARALLEL", 3)):
                    self._cv.wait(1.0)
                if p.cancel.is_set():
                    return self._finish(p, "cancelled", error="cancelled while waiting")
                self._active += 1
            try:
                if p.needs_screen:
                    p.status, p.progress = "waiting for the screen", "another project is using the screen"
                    while not self._screen.acquire(timeout=1.0):
                        if p.cancel.is_set():
                            return self._finish(p, "cancelled", error="cancelled while waiting for the screen")
                    held_screen, self.screen_owner = True, p
                p.status, p.progress, p.started = "running", "starting", time.time()
                self._save()
                self._run(p, ctx)
            finally:
                if held_screen:
                    self.screen_owner = None
                    self._screen.release()
                with self._cv:
                    self._active -= 1
                    self._cv.notify_all()
        except ProjectCancelled:
            self._finish(p, "cancelled", error="cancelled")
        except Exception as e:  # a project must never take Friday down
            self._finish(p, "failed", error=f"{type(e).__name__}: {str(e)[:300]}")

    def _run(self, p: Project, ctx) -> None:
        from .credits import get_gate

        def on_tool(name: str, args: dict) -> None:
            if p.cancel.is_set():
                raise ProjectCancelled()
            p.add_step(f"{name}({_short(args)})")

        brain = make_brain(ctx, p, on_tool, lambda kind, text, detail="": None)
        p.brain_label = getattr(brain, "label", "")
        blackbox.begin_turn(f"[project #{p.id} {p.title}] {p.request}", p.brain_label)  # the steps are recorded like any request
        screen = ("You may use the keyboard, mouse, screen and browser: you have the screen to yourself for this project."
                  if p.needs_screen else "You cannot use the keyboard, mouse, screen or browser in this project.")
        first = (f"You are working on a BACKGROUND PROJECT for the owner, who is busy with other things. Project: {p.title}\n"
                 f"Request: {p.request}\n\nWork through it step by step with your tools and check every result. Do not ask the "
                 f"owner questions unless you truly cannot continue (a tool that needs permission asks by itself). {screen} "
                 "When you are finished, reply with a short report in plain sentences, no markdown: what you did, what you found "
                 "or made, and where anything was saved. If you could not finish, say exactly why.")
        gate = get_gate()
        lo, hi = gate.estimate("research", p.request, 4000)
        reply = ""
        with gate.task(f"project #{p.id}: {p.title}", "a background project", lo, hi, p.brain_label) as ok:
            if not ok:
                raise RuntimeError("the credit gate did not allow this project")
            reply = brain._chat_inner(first)  # not chat(): the quick commands must not intercept a project
            rounds = 1
            while reply == STUCK_REPLY and rounds < MAX_ROUNDS and not p.cancel.is_set():
                p.add_step("still working (next round)")
                reply = brain._chat_inner("Continue where you left off. If the project is finished, give the final report.")
                rounds += 1
        if p.cancel.is_set():
            raise ProjectCancelled()
        if reply == STUCK_REPLY or not reply.strip():
            return self._finish(p, "failed", error="I used up all my steps without finishing.", result=reply if reply != STUCK_REPLY else "")
        self._finish(p, "failed" if getattr(brain, "last_error", "") and reply.lower().startswith("i can't reach") else "done",
                     result=reply.strip())

    def _finish(self, p: Project, status: str, result: str = "", error: str = "") -> None:
        p.status, p.finished, p.result, p.error = status, time.time(), result[:6000], error
        p.progress = {"done": "finished", "cancelled": "cancelled", "failed": "failed"}.get(status, status)
        self._save()
        try:
            (data_dir() / "projects").mkdir(parents=True, exist_ok=True)
            (data_dir() / "projects" / f"{p.id}.txt").write_text(f"{p.title}\n{p.request}\n\n{p.result or p.error}\n", encoding="utf-8")
        except OSError:
            pass
        activity.log("project", p.line())
        if status == "cancelled":
            return
        try:
            from . import updates

            if status == "done":
                updates.send("project", f"Project {p.title} is finished. {p.result[:260]}")
            else:
                updates.send("project", f"Project {p.title} could not finish: {p.error[:200]}", important=True)
        except Exception:
            pass


MANAGER = Manager()


def screen_conflict(tool_name: str) -> str:
    """'' normally. While a project holds the screen, every other thread's keyboard / mouse / screen / browser tool is refused."""
    owner = MANAGER.screen_owner
    if owner is None or not is_control(tool_name) or getattr(_TL, "project", None) is owner:
        return ""
    return f"Not now: the project '{owner.title}' is using the screen. Try again when it finishes, or cancel it."


def prompt_section() -> str:
    return MANAGER.prompt_section()


# ------------------------------------------------------------------ tools
@tool("start_project", "Start a long job as a BACKGROUND PROJECT and carry on talking: research, writing, a multi-step task, "
      "anything that takes more than a minute or two, or when the owner says 'in the background' or gives several jobs at once. "
      "Give a short title and the full request with every detail. Several projects run at the same time (3 by default). Set "
      "needs_screen=true ONLY if the job must use the keyboard, mouse, screen or browser (then it has the screen alone).",
      {"type": "object", "properties": {"title": {"type": "string"}, "request": {"type": "string"},
                                        "needs_screen": {"type": "boolean"}}, "required": ["title", "request"]})
def start_project(ctx, title: str, request: str, needs_screen: bool = False):
    try:
        p = MANAGER.start(ctx, title, request, bool(needs_screen))
    except (ValueError, RuntimeError) as e:
        return f"Error: {e}"
    ahead = sum(1 for x in MANAGER.projects if x.active and x.id != p.id)
    limit = max(1, env_int("PROJECTS_MAX_PARALLEL", 3))
    return (f"Started project #{p.id} '{p.title}'. " + (f"It waits in line behind {ahead - limit + 1} other(s) until a slot is free. "
            if ahead >= limit else "") + "I'll tell you when it's done; ask 'what are you working on' any time.")


@tool("list_projects", "List the background projects: running, waiting, and recently finished, with what each is doing now. "
      "Use for 'what are you working on', 'what projects are running'.")
def list_projects(ctx):
    MANAGER._ensure_loaded()
    rows = [p.line() for p in MANAGER.projects[-8:][::-1]]
    return "\n".join(rows) if rows else "No projects yet. Give me a job and say 'in the background'."


@tool("project_status", "The details and the result of one background project (by number or part of its title): its last steps "
      "and, when finished, its report.", {"type": "object", "properties": {"which": {"type": "string"}}})
def project_status(ctx, which: str = ""):
    p = MANAGER.find(which)
    if p is None:
        return f"No project matches '{which}'." if which else "Which project? Say its number or name (see list_projects)."
    out = [p.line()]
    if p.active:
        out += ["Last steps:"] + [f"  {s}" for s in p.steps[-5:]]
    else:
        out.append("Report: " + (p.result or p.error or "(nothing)"))
    return "\n".join(out)


@tool("cancel_project", "Stop a background project (by number or part of its title).",
      {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})
def cancel_project(ctx, which: str):
    return MANAGER.cancel(which)
