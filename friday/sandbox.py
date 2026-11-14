"""Safety cage for learned and downloaded skills.

Every tool from a learned / downloaded skill runs in its OWN separate Python process ("sandbox") that:
  * gets a cleaned environment - none of your API keys, passwords or tokens (only SKILL_<NAME>_* keys you add),
  * runs in its own folder (data/skill_data/<skill>) and cannot open Friday's .env, data, secrets or backups,
  * can only use the internet / write files / run programs if its permission card says so,
  * is killed after SKILL_TIMEOUT seconds (default 60).
The permission card is worked out by reading the code (not by trusting what the skill says) and is shown to you
before installing. Python 'audit hooks' enforce it inside the sandbox process.
Set SKILL_SANDBOX=false only if a skill truly needs to run inside Friday (not recommended).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PERMS = {
    "internet": "use the internet",
    "files": "create, change or delete files on your PC",
    "email": "read or send email",
    "run": "start other programs or commands",
}
_NET = {"urllib", "http", "requests", "socket", "ssl", "ftplib", "httpx", "aiohttp", "websocket", "urlopen"}
_MAIL = {"smtplib", "imaplib", "poplib"}
_RUN = {"subprocess", "os.system", "os.popen", "os.startfile", "os.spawnl", "os.execv", "webbrowser"}
_FILES = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.replace", "os.makedirs", "os.mkdir",
          "shutil", "send2trash", "write_text", "write_bytes", "unlink", "rmdir", "mkdir", "rename", "touch"}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def detect_permissions(code: str) -> list[str]:
    """Read the code and list what it is able to do (internet / files / email / run)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    found: set[str] = set()
    for n in ast.walk(tree):
        names: list[str] = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names = [n.module or ""] + [f"{n.module}.{a.name}" for a in n.names]
        elif isinstance(n, ast.Call):
            names = [_dotted(n.func)]
            if names[0].split(".")[-1] == "open":
                mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
                if mode is not None and (not isinstance(mode, ast.Constant) or set(str(mode.value)) & set("wax+")):
                    found.add("files")
        for name in names:
            root, last = name.split(".")[0], name.split(".")[-1]
            if root in _NET or last in _NET:
                found.add("internet")
            if root in _MAIL:
                found.update({"email", "internet"})
            if name in _RUN or root in _RUN:
                found.add("run")
            if name in _FILES or root in _FILES or last in _FILES:
                found.add("files")
    return sorted(found)


def permission_card(name: str, perms: list[str], tools: list[dict] | None = None, source: str = "") -> str:
    """Plain-English card shown before installing."""
    can = [PERMS[p] for p in perms if p in PERMS]
    card = f"Permission card for '{name}'" + (f" (from {source})" if source else "") + ": "
    card += ("it can " + ", ".join(can) + ". ") if can else "it can't use the internet, change files, email or run programs. "
    card += "It runs in the safety cage: no access to your passwords, keys or Friday's files."
    if tools:
        card += " Adds: " + "; ".join(f"{t['name']} - {t.get('description', '')[:90]}" for t in tools) + "."
    return card


def enabled() -> bool:
    return os.environ.get("SKILL_SANDBOX", "true").strip().lower() not in ("0", "false", "no", "off")


def skill_data_dir(skill: str) -> Path:
    from .env import data_dir

    d = data_dir() / "skill_data" / skill
    d.mkdir(parents=True, exist_ok=True)
    return d


def clean_env(skill: str) -> dict:
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA",
            "APPDATA", "COMSPEC", "PATHEXT", "LANG", "TZ", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
    out = {k: os.environ[k] for k in keep if k in os.environ}
    prefix = f"SKILL_{skill.upper()}_"
    out.update({k: v for k, v in os.environ.items() if k.startswith(prefix)})
    out["PYTHONPATH"] = str(PROJECT_ROOT)
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out


def run_tool(skill_path: Path, tool_name: str, args: dict, perms: list[str], ctx=None, timeout: int | None = None) -> str:
    """Run one skill tool inside the sandbox process and return its text result."""
    skill = Path(skill_path).stem
    work = skill_data_dir(skill)
    settings = getattr(ctx, "settings", None)
    job = {"skill": str(Path(skill_path).resolve()), "tool": tool_name, "args": args, "perms": perms,
           "work": str(work), "project": str(PROJECT_ROOT),
           "settings": {"default_city": getattr(settings, "default_city", ""),
                        "user_name": getattr(settings, "user_name", ""),
                        "assistant_name": getattr(settings, "assistant_name", "Friday")}}
    timeout = timeout or int(os.environ.get("SKILL_TIMEOUT", "60") or 60)
    try:
        r = subprocess.run([sys.executable, "-m", "friday.sandbox"], input=json.dumps(job), capture_output=True,
                           text=True, timeout=timeout, cwd=str(work), env=clean_env(skill), encoding="utf-8")
    except subprocess.TimeoutExpired:
        return f"Error: the {tool_name} skill took longer than {timeout} seconds and was stopped."
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("SANDBOX_RESULT "):
            return json.loads(line[15:])
    err = (r.stderr or r.stdout).strip().splitlines()
    return "Error in sandboxed skill: " + (" | ".join(err[-3:])[:500] or "no output")


def _param_names(func) -> list[str]:
    """The tool function's own parameters (after ctx), so the main process can check them against the JSON schema
    without importing the skill."""
    import inspect

    try:
        params = list(inspect.signature(func).parameters.values())[1:]
    except (TypeError, ValueError):
        return []
    return [p.name for p in params if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]


def probe_tools(skill_path: Path, perms: list[str], timeout: int | None = None) -> list[dict]:
    """Import a skill INSIDE the sandbox and report the tools it registers, without running any of them.

    Import-time code (decorators, class bodies, module-level statements) is attacker-controlled, so it must never run
    in Friday's own process. Returns [{name, description, params, dangerous, risk}]."""
    skill = Path(skill_path).stem
    work = skill_data_dir(skill)
    job = {"skill": str(Path(skill_path).resolve()), "probe": True, "perms": perms, "work": str(work),
           "project": str(PROJECT_ROOT)}
    timeout = timeout or int(os.environ.get("SKILL_TIMEOUT", "60") or 60)
    try:
        r = subprocess.run([sys.executable, "-m", "friday.sandbox"], input=json.dumps(job), capture_output=True,
                           text=True, timeout=timeout, cwd=str(work), env=clean_env(skill), encoding="utf-8")
    except subprocess.TimeoutExpired:
        raise ValueError(f"the skill took longer than {timeout} seconds to load and was stopped")
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("SANDBOX_PROBE "):
            data = json.loads(line[14:])
            if data.get("error"):
                raise ValueError(data["error"])
            return data.get("tools") or []
    err = (r.stderr or r.stdout).strip().splitlines()
    raise ValueError("could not load the skill in the safety cage: " + (" | ".join(err[-3:])[:400] or "no output"))


# ------------------------------------------------------------------ worker side (runs in the sandbox process)
def _install_guard(perms: set[str], project: Path, work: Path, skill_file: Path) -> None:
    project, work = project.resolve(), work.resolve()
    allowed_inside = [work, (project / "friday").resolve(), (project / ".venv").resolve(), skill_file.resolve()]
    secret_words = (".env", "auth_secrets", "dashboard_token", "credentials", "api_key", "id_rsa")

    def inside(p: Path, base: Path) -> bool:
        return p == base or base in p.parents

    def hook(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            try:
                p = path.resolve()
            except OSError:
                return
            name = p.name.lower()
            if any(w in name for w in secret_words):
                raise PermissionError(f"Safety cage: skills may not open {p.name}")
            if inside(p, project) and not any(inside(p, a) for a in allowed_inside):
                raise PermissionError("Safety cage: skills may not read or change Friday's own files")
            mode, flags = (args[1] if len(args) > 1 else None), (args[2] if len(args) > 2 else 0)
            writing = (isinstance(mode, str) and bool(set(mode) & set("wax+"))) or \
                      (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))
            if writing and "files" not in perms and not inside(p, work):
                raise PermissionError("Safety cage: this skill has no permission to write files outside its folder")
        elif event in ("socket.connect", "socket.getaddrinfo") and "internet" not in perms:
            raise PermissionError("Safety cage: this skill has no internet permission")
        elif event in ("subprocess.Popen", "os.system", "os.startfile", "os.exec", "os.spawn", "os.posix_spawn") \
                and "run" not in perms:
            raise PermissionError("Safety cage: this skill has no permission to run programs")
        elif event in ("os.remove", "os.rename", "os.rmdir", "shutil.rmtree", "os.mkdir") and "files" not in perms:
            target = Path(os.fsdecode(args[0])) if args and isinstance(args[0], (str, bytes, os.PathLike)) else None
            if target is None or not inside(target.resolve(), work):
                raise PermissionError("Safety cage: this skill has no permission to change files")

    sys.addaudithook(hook)


def _worker() -> None:
    import importlib.util
    from types import SimpleNamespace

    job = json.loads(sys.stdin.read())
    from friday import tools as T  # imported before the guard (reads Friday's package files)

    skill_file, work = Path(job["skill"]), Path(job["work"])
    code = skill_file.read_text(encoding="utf-8")
    _install_guard(set(job["perms"]), Path(job["project"]), work, skill_file)
    if job.get("probe"):  # import only: report the tools the skill registers, run none of them
        before = set(T.REGISTRY)
        try:
            spec = importlib.util.spec_from_loader(f"friday_skill_{skill_file.stem}", loader=None)
            module = importlib.util.module_from_spec(spec)
            module.__file__ = str(skill_file)
            exec(compile(code, str(skill_file), "exec"), module.__dict__)  # noqa: S102 - the code passed check_code
            found = [{"name": n, "description": T.REGISTRY[n].description, "params": T.REGISTRY[n].params,
                      "dangerous": T.REGISTRY[n].dangerous, "risk": T.REGISTRY[n].risk,
                      "sig": _param_names(T.REGISTRY[n].func)}
                     for n in sorted(set(T.REGISTRY) - before)]
            print("SANDBOX_PROBE " + json.dumps({"tools": found}))
        except PermissionError as e:
            print("SANDBOX_PROBE " + json.dumps({"error": f"the safety cage blocked it while loading: {e}"}))
        except Exception as e:
            print("SANDBOX_PROBE " + json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return
    out = ""
    try:
        spec = importlib.util.spec_from_loader(f"friday_skill_{skill_file.stem}", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(skill_file)
        exec(compile(code, str(skill_file), "exec"), module.__dict__)  # noqa: S102 - the code passed check_code
        t = T.REGISTRY[job["tool"]]
        st = job["settings"]
        ctx = SimpleNamespace(settings=SimpleNamespace(data_dir=work, **st), memory=None, timers=[],
                              speak=lambda text: print(f"[skill] {text}", file=sys.stderr),
                              confirm=lambda prompt: False, ask_ai=None, dashboard_url="", control=None)
        out = str(t.func(ctx, **job["args"]))
    except PermissionError as e:
        out = f"Error: {e}"
    except Exception as e:
        out = f"Error while running {job['tool']}: {type(e).__name__}: {e}"
    print("SANDBOX_RESULT " + json.dumps(out[:20000]))


if __name__ == "__main__":
    _worker()
