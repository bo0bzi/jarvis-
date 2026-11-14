"""Learned skills: Friday can write, test and install new tools for itself.

How it works
1. You ask: "Friday, learn how to <do something>".
2. The learn_skill tool asks the strongest AI available (the backup brain if set, e.g. Abacus) to write a small
   Python file using the same @tool system as the built-in tools.
3. The code is checked (safety rules below) and test-loaded in a separate process. If that fails, the AI gets one
   more try with the error message.
4. Friday tells you what the new skill does and asks permission. Only then is it saved in the `skills` folder and
   switched on - immediately, no restart.

Safety rules for learned skills
- Nothing may run when the file is loaded (only imports, constants, functions/classes).
- Never allowed: eval/exec/compile/__import__/importlib, ctypes, winreg, reading Friday's .env or secrets files,
  editing Friday's own code.
- Anything that changes things (delete/move/write files, run programs, send email or data) is only allowed if every
  tool in the file is marked dangerous=True, so Friday asks you before each use.
- Skills can be listed, switched off (moved to skills/_disabled) and restored. Old versions go to skills/_backup.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from . import tools as T
from .tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

LOADED: dict[str, list[str]] = {}   # skill name -> tool names it registered
ERRORS: dict[str, str] = {}         # skill name -> load error

FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "importlib", "ctypes", "winreg", "globals", "setattr",
                   "delattr", "__builtins__", "__subclasses__"}
FORBIDDEN_TEXT = (".env", "abacusai_auth", "auth_secrets", "dashboard_token")
RISKY_CALLS = {"subprocess", "os.system", "os.popen", "os.startfile", "os.remove", "os.unlink", "os.rmdir",
               "os.removedirs", "os.rename", "os.replace", "shutil.rmtree", "shutil.move", "shutil.copy",
               "shutil.copyfile", "shutil.copytree", "send2trash", "smtplib", "write_text", "write_bytes", "unlink",
               "rmdir", "rename", "replace_file", "urlopen_post", "requests.post", "requests.put", "requests.delete"}


def skills_dir() -> Path:
    d = Path(os.environ.get("SKILLS_DIR") or PROJECT_ROOT / "skills")
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- static safety check
def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_tool_decorator(dec: ast.AST) -> ast.Call | None:
    if isinstance(dec, ast.Call) and _dotted(dec.func).split(".")[-1] == "tool":
        return dec
    return None


def check_code(code: str) -> tuple[list[str], dict]:
    """Return (problems, info). info = {"tools": [(name, dangerous)], "risky": [...]}."""
    problems: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Python syntax error on line {e.lineno}: {e.msg}"], {}

    tools: list[tuple[str, bool]] = []
    risky: set[str] = set()
    imports_tool = False

    for node in tree.body:  # top level: nothing may *run* on import
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module in ("friday.tools", "tools") \
                    and any(a.name == "tool" for a in node.names):
                imports_tool = True
            continue
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                call = _is_tool_decorator(dec)
                if call is None:
                    continue
                name = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else None
                dangerous = any(k.arg == "dangerous" and isinstance(k.value, ast.Constant) and k.value.value is True
                                for k in call.keywords)
                if not isinstance(name, str):
                    problems.append(f"The @tool on {node.name} needs a literal tool name as its first argument.")
                else:
                    tools.append((name, dangerous))
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if any(isinstance(n, (ast.Call, ast.Lambda, ast.Await)) for n in ast.walk(node)):
                problems.append(f"Line {node.lineno}: top-level values must be plain constants (no function calls).")
            continue
        problems.append(f"Line {node.lineno}: only imports, constants and functions are allowed at the top level.")

    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES:
            problems.append(f"Not allowed: {n.id}")
        elif isinstance(n, ast.Attribute) and (n.attr in FORBIDDEN_NAMES or n.attr.startswith("__")):
            problems.append(f"Not allowed: .{n.attr}")
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for m in mods:
                root = m.split(".")[0]
                if root in FORBIDDEN_NAMES:
                    problems.append(f"Not allowed to import {m}")
                if root in ("subprocess", "smtplib", "send2trash"):
                    risky.add(root)
                if m.startswith("friday") and m != "friday.tools":
                    problems.append(f"Skills may only import friday.tools, not {m}")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and any(t in n.value for t in FORBIDDEN_TEXT):
            problems.append("Skills may not touch Friday's settings/secret files.")
        elif isinstance(n, ast.Call):
            d = _dotted(n.func)
            if d in RISKY_CALLS or d.split(".")[-1] in RISKY_CALLS or d.split(".")[0] in RISKY_CALLS:
                risky.add(d)
            if d.split(".")[-1] == "open":
                mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
                    risky.add("open(write)")
                elif mode is not None and not isinstance(mode, ast.Constant):
                    risky.add("open(dynamic mode)")
            if any(k.arg == "data" for k in n.keywords) and "url" in d.lower():
                risky.add(d + "(data=...)")

    for name in undefined_names(tree):
        problems.append(f"'{name}' is used but never defined or imported (missing an import?).")
    if not imports_tool:
        problems.append("The skill must start with: from friday.tools import tool")
    if not tools:
        problems.append("The skill must define at least one function decorated with @tool(...).")
    for name, _ in tools:
        if not NAME_RE.match(name):
            problems.append(f"Tool name '{name}' must be lowercase letters, numbers and underscores.")
    if risky and not all(d for _, d in tools):
        problems.append("This skill changes things or sends data (" + ", ".join(sorted(risky)) +
                        "), so every @tool in it must have dangerous=True.")
    return sorted(set(problems), key=problems.index), {"tools": tools, "risky": sorted(risky)}


def _bound_names(node: ast.AST) -> set[str]:
    """Names a function/module defines: assignments, loop/with/except targets, imports, nested defs, arguments."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


def undefined_names(tree: ast.Module) -> list[str]:
    """Simple check for names that are used but never defined/imported (e.g. a forgotten 'import random')."""
    import builtins

    known = set(dir(builtins)) | _bound_names(tree) | {"__file__", "__name__", "__doc__"}
    missing = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known and n.id not in missing:
            missing.append(n.id)
    return missing


# --------------------------------------------------------------------------- loading
def _skill_files() -> list[Path]:
    return sorted(p for p in skills_dir().glob("*.py") if not p.name.startswith("_") and NAME_RE.match(p.stem))


def _unload(name: str) -> None:
    for t in LOADED.pop(name, []):
        T.REGISTRY.pop(t, None)
    ERRORS.pop(name, None)
    sys.modules.pop(f"friday_skill_{name}", None)


def load_skill(path: Path) -> list[str]:
    """(Re)load one skill file. Returns its tool names; raises on error (nothing half-registered).

    The skill is imported inside the safety cage, never in Friday's own process: import-time code (decorators, class
    bodies, module-level statements) is attacker-controlled, so a check_code pass alone is not enough."""
    name = path.stem
    code = path.read_text(encoding="utf-8")
    problems, info = check_code(code)
    if problems:
        raise ValueError("; ".join(problems))
    builtin = {t for t, tl in T.REGISTRY.items() if getattr(tl, "source", "built-in") != f"skill:{name}"}
    clash = [t for t, _ in info["tools"] if t in builtin]
    if clash:
        raise ValueError(f"tool name(s) already used by Friday: {', '.join(clash)}")
    from . import sandbox

    perms = sandbox.detect_permissions(code)
    if sandbox.enabled():
        found = sandbox.probe_tools(path, perms)  # import happens in the cage
        if not found:
            raise ValueError("the skill didn't register any tools")
        clash = [t["name"] for t in found if t["name"] in builtin]
        if clash:
            raise ValueError(f"tool name(s) already used by Friday: {', '.join(clash)}")
    else:  # cage switched off: import here, as before
        before = set(T.REGISTRY)
        spec = importlib.util.spec_from_file_location(f"friday_skill_{name}", path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            for t in set(T.REGISTRY) - before:
                T.REGISTRY.pop(t, None)
            raise
        found = [{"name": n, "description": T.REGISTRY[n].description, "params": T.REGISTRY[n].params,
                  "dangerous": T.REGISTRY[n].dangerous, "risk": T.REGISTRY[n].risk}
                 for n in sorted(set(T.REGISTRY) - before)]
        sys.modules[spec.name] = module
    _unload(name)
    for t in found:
        T.REGISTRY[t["name"]] = T.Tool(t["name"], t["description"], t["params"],
                                       _caged(path, t["name"], perms, sig=t.get("sig")), bool(t["dangerous"]),
                                       risk=t.get("risk", ""))
        T.REGISTRY[t["name"]].source = f"skill:{name}"
        T.REGISTRY[t["name"]].perms = perms
    new = sorted(t["name"] for t in found)
    LOADED[name] = new
    return new


def _caged(path: Path, tool_name: str, perms: list[str], original=None, sig: list[str] | None = None):
    """Replace a skill tool's function with one that runs it inside the safety cage (separate process)."""
    from . import sandbox

    def run(ctx, **kwargs):
        return sandbox.run_tool(path, tool_name, kwargs, perms, ctx)
    run.__name__ = tool_name
    run.caged = True
    run.original = original
    if sig:  # the real signature, so schema checks still see the skill's own parameters
        import inspect

        run.__signature__ = inspect.Signature(
            [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            + [inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD) for n in sig])
    return run


# --------------------------------------------------------------------------- learning memory (remembers failures)
def _learning_path() -> Path:
    from .env import data_dir

    return data_dir() / "learning.json"


def learning_log() -> list[dict]:
    try:
        return json.loads(_learning_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def remember_outcome(request: str, name: str, ok: bool, detail: str) -> None:
    log = learning_log()[-200:]
    log.append({"time": datetime.now().isoformat(timespec="seconds"), "request": request[:300], "skill": name,
                "ok": ok, "detail": detail[:500]})
    try:
        _learning_path().write_text(json.dumps(log, indent=1), encoding="utf-8")
    except OSError:
        pass


def past_failures(request: str, limit: int = 3) -> list[dict]:
    words = set(re.findall(r"[a-z]{4,}", request.lower()))
    scored = []
    for e in learning_log():
        if e.get("ok"):
            continue
        overlap = len(words & set(re.findall(r"[a-z]{4,}", e.get("request", "").lower())))
        if overlap >= max(1, len(words) // 3):
            scored.append((overlap, e))
    return [e for _, e in sorted(scored, key=lambda x: -x[0])[:limit]]


def load_all() -> dict:
    """Load every skill in the skills folder. Broken skills are reported, never crash Friday."""
    for name in list(LOADED):
        _unload(name)
    for p in _skill_files():
        try:
            load_skill(p)
        except Exception as e:
            ERRORS[p.stem] = f"{type(e).__name__}: {str(e)[:300]}"
            print(f"[skills] '{p.stem}' not loaded: {ERRORS[p.stem]}")
    if LOADED:
        print(f"[skills] Learned skills loaded: {', '.join(LOADED)}")
    return {"loaded": dict(LOADED), "errors": dict(ERRORS)}


def test_in_subprocess(path: Path, timeout: int = 40) -> tuple[bool, str]:
    """Import the skill in a fresh Python process to make sure it loads and its tool schemas are valid."""
    script = textwrap.dedent(f"""
        import inspect, json, sys
        sys.path.insert(0, {str(PROJECT_ROOT)!r})
        from friday import tools as T, skills as S
        from pathlib import Path
        names = S.load_skill(Path({str(path)!r}))
        out = []
        for n in names:
            t = T.REGISTRY[n]
            assert isinstance(t.description, str) and t.description, f"{{n}}: description missing"
            assert t.params.get("type") == "object", f"{{n}}: params must be a JSON schema object"
            assert "strict" not in t.params
            props = t.params.get("properties", {{}})
            for r in t.params.get("required", []):
                assert r in props, f"{{n}}: required '{{r}}' not in properties"
            sig = list(inspect.signature(getattr(t.func, "original", None) or t.func).parameters.values())[1:]
            for p in sig:
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                assert p.name in props, (f"{{n}}: function parameter '{{p.name}}' is missing from the JSON schema "
                                         "'properties' (the assistant could never pass it)")
            out.append({{"name": n, "dangerous": t.dangerous, "description": t.description}})
        print("SKILL_OK " + json.dumps(out))
    """)
    try:
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=timeout,
                           cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return False, "Loading the skill took too long (does it do work at import time?)."
    for line in r.stdout.splitlines():
        if line.startswith("SKILL_OK "):
            return True, line[9:]
    err = (r.stderr or r.stdout).strip().splitlines()
    return False, "\n".join(err[-6:])[:800] or "unknown error"


# --------------------------------------------------------------------------- writing skills with the AI
WRITER_SYSTEM = textwrap.dedent('''
    You write "skills" (plugins) for Friday, a Python voice assistant on Windows 11. Reply with ONE complete Python
    file in a single ```python code block, then one short line describing it. No other text.

    Exact format:
    ```python
    """<one-line description of the skill>"""
    from friday.tools import tool
    import json, urllib.request   # standard library only (plus: psutil, pyperclip, numpy, Pillow)

    API_URL = "https://example.com"   # top level: only imports, plain constants, functions - nothing runs on import

    @tool("verb_noun", "Clear description so the assistant knows when to use it.",
          {"type": "object", "properties": {"city": {"type": "string", "description": "..."}}, "required": ["city"]})
    def verb_noun(ctx, city: str):
        ...
        return "short plain-text result that will be spoken"   # always return a str
    ```
    Rules:
    - Every tool function takes ctx first, then the parameters from its JSON schema; returns a short string.
    - Catch errors inside the function and return a helpful message instead of raising.
    - Use only the standard library (urllib.request for web, timeout=15) or psutil/pyperclip/numpy/Pillow.
      Prefer free APIs that need no key. If a key is truly needed, read os.environ.get("SKILL_<NAME>_KEY") and,
      if missing, return a message telling the user to add that line to Friday's .env file.
    - ctx.settings.default_city, ctx.settings.data_dir (a pathlib.Path for files the skill may keep),
      ctx.memory (facts) are available.
    - Forbidden: eval, exec, compile, __import__, importlib, ctypes, winreg, reading .env or Friday's own files,
      code that runs at import time, imports from friday other than friday.tools.
    - Anything that deletes/moves/writes files, runs programs (subprocess), sends emails or posts data MUST be
      marked dangerous=True on EVERY @tool in the file (the user is then asked before each use).
    - Tool names: lowercase_with_underscores, specific (not generic like "run" or "do_task").
    - Keep it small and reliable: 1-3 tools, under 150 lines.
''').strip()

CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def extract_code(reply: str) -> str:
    m = CODE_RE.search(reply or "")
    return (m.group(1) if m else reply or "").strip() + "\n"


def _existing_tool_list() -> str:
    return ", ".join(sorted(T.REGISTRY))


def write_skill(ctx, request: str, skill_name: str, previous_code: str = "", prefer: str = "local",
                extra_hint: str = "") -> tuple[Path | None, str, list]:
    """Ask the AI for code, check + test it (2 attempts). Returns (pending_path, message, tool_info).
    prefer="local" uses the free local coder model; "cloud" goes through the Credit Gate (asks you first)."""
    ask = getattr(ctx, "ask_ai", None)
    if ask is None:
        return None, "Error: learning needs the AI brain, which isn't available in this mode.", []
    pending = skills_dir() / "_pending"
    pending.mkdir(exist_ok=True)
    user = (f"Skill file name: {skill_name}.py\nWhat the user wants: {request}\n"
            f"Names already used by other tools (don't reuse): {_existing_tool_list()}\n")
    if extra_hint:
        user += "\n" + extra_hint + "\n"
    if previous_code:
        user += f"\nThis is an UPDATE of the existing skill. Current code:\n```python\n{previous_code}\n```\n" \
                "Keep its tool names unless the change requires otherwise."
    error = ""
    for attempt in range(2):
        prompt = user if not error else user + f"\nYour previous attempt failed these checks, fix them:\n{error}\n"
        try:
            if prefer == "cloud":
                reply, who = ask(WRITER_SYSTEM, prompt, prefer="cloud", task=f"write the skill '{skill_name}'")
            else:
                reply, who = ask(WRITER_SYSTEM, prompt)
        except TypeError as e:
            return None, f"Error: this AI can't be asked that way ({e}).", []
        except Exception as e:
            return None, f"Error: the AI couldn't write the skill ({type(e).__name__}: {str(e)[:200]}).", []
        code = extract_code(reply)
        header = f"# Learned skill written by {who} on {datetime.now():%Y-%m-%d %H:%M}\n# Request: {request[:200]}\n"
        code = header + code
        path = pending / f"{skill_name}.py"
        path.write_text(code, encoding="utf-8")
        problems, info = check_code(code)
        if problems:
            error = "\n".join(problems)
            continue
        ok, detail = test_in_subprocess(path)
        if ok:
            return path, detail, json.loads(detail)
        error = f"Loading failed:\n{detail}"
    return None, f"Error: I couldn't build a working skill after 2 tries. Last problem: {error[:400]}", []


def install(pending_path: Path) -> list[str]:
    target = skills_dir() / pending_path.name
    if target.exists():
        backup = skills_dir() / "_backup"
        backup.mkdir(exist_ok=True)
        shutil.copy2(target, backup / f"{target.stem}-{datetime.now():%Y%m%d-%H%M%S}.py")
    shutil.move(str(pending_path), target)
    return load_skill(target)


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "to", "how", "my", "me", "for", "and", "of", "learn", "can", "you", "please", "i", "want"}
    words = [w for w in words if w not in stop][:4] or ["skill"]
    name = "_".join(words)
    if not name[0].isalpha():
        name = "skill_" + name
    return name[:40]


# --------------------------------------------------------------------------- tools
@tool("learn_skill",
      "Teach yourself a NEW ability by writing a new tool (Python plugin), when the user asks you to learn something "
      "or asks for something none of your tools can do. Also used to improve an existing learned skill. "
      "The user is asked to approve before it is installed.",
      {"type": "object", "properties": {
          "request": {"type": "string", "description": "What the new skill should do, in detail"},
          "skill_name": {"type": "string", "description": "Short snake_case name, e.g. stock_prices"},
          "update_existing": {"type": "boolean", "description": "true to improve an existing learned skill with this name"}},
       "required": ["request"]})
def learn_skill(ctx, request: str, skill_name: str = "", update_existing: bool = False):
    name = re.sub(r"[^a-z0-9_]", "_", (skill_name or _slug(request)).lower()).strip("_")[:40]
    if not NAME_RE.match(name):
        name = _slug(request)
    existing = skills_dir() / f"{name}.py"
    previous = existing.read_text(encoding="utf-8") if existing.exists() and update_existing else ""
    if existing.exists() and not update_existing:
        name = f"{name}_2"
    hint = ""
    fails = past_failures(request)
    if fails:
        hint = "Earlier attempts at similar skills failed like this - avoid it:\n" + \
               "\n".join(f"- {f['detail'][:200]}" for f in fails)
    path, detail, info = write_skill(ctx, request, name, previous, extra_hint=hint)  # free local AI first
    if path is None and "No AI available" not in detail:
        local_detail = detail
        cloud_path, cloud_detail, cloud_info = write_skill(ctx, request, name, previous, prefer="cloud", extra_hint=hint)
        if cloud_path is not None:
            path, detail, info = cloud_path, cloud_detail, cloud_info
        elif "declined" in cloud_detail or "Refused" in cloud_detail or "No AI" in cloud_detail \
                or "approval" in cloud_detail.lower() or "can't be asked" in cloud_detail:
            detail = local_detail + " (The cloud AI wasn't used.)"
        else:
            detail = cloud_detail
    if path is None:
        remember_outcome(request, name, False, detail)
        return detail
    from . import autonomy, sandbox

    code = path.read_text(encoding="utf-8")
    perms = sandbox.detect_permissions(code)
    card = sandbox.permission_card(name, perms, info)
    lines = [f"{t['name']} ({'asks you first' if t['dangerous'] else 'safe'}): {t['description']}" for t in info]
    question = (f"I wrote a new skill called {name}. It adds: " + "; ".join(lines) + ". " + card +
                " You can read the code in the skills/_pending folder. Should I install it?")
    auto_ok = autonomy.level() == 2 and not perms and not any(t["dangerous"] for t in info)
    if not auto_ok and not ctx.confirm(question):
        remember_outcome(request, name, False, "user declined install")
        return (f"Not installed. The code is saved in skills/_pending/{name}.py if the user wants to look at it. "
                "Do not retry unless asked.")
    try:
        tools_added = install(path)
    except Exception as e:
        remember_outcome(request, name, False, f"install failed: {e}")
        return f"Error: the skill failed to install: {e}"
    remember_outcome(request, name, True, "installed: " + ", ".join(tools_added))
    from . import activity

    activity.log("skill", f"Learned skill '{name}' installed ({', '.join(tools_added)}). {card}")
    return (f"Skill '{name}' installed and active now (runs in the safety cage). New tools: {', '.join(tools_added)}. "
            "You can use them right away in this conversation.")


@tool("list_skills", "List the skills (abilities) Friday has learned, plus any that failed to load or are switched off.")
def list_skills(ctx):
    d = skills_dir()
    parts = []
    if LOADED:
        parts.append("Learned skills: " + "; ".join(f"{n} ({', '.join(t)})" for n, t in LOADED.items()) + ".")
    else:
        parts.append("No learned skills yet. Ask me to learn something, e.g. 'learn how to get stock prices'.")
    if ERRORS:
        parts.append("Broken: " + "; ".join(f"{n}: {e[:80]}" for n, e in ERRORS.items()) + ".")
    disabled = sorted(p.stem for p in (d / "_disabled").glob("*.py")) if (d / "_disabled").exists() else []
    if disabled:
        parts.append("Switched off: " + ", ".join(disabled) + ".")
    return " ".join(parts) + f" Built-in tools: {sum(1 for t in T.REGISTRY.values() if getattr(t, 'source', 'built-in') == 'built-in')}."


@tool("disable_skill", "Switch off (remove) a learned skill. It is moved to skills/_disabled and can be restored.",
      {"type": "object", "properties": {"skill_name": {"type": "string"}}, "required": ["skill_name"]}, dangerous=True,
      risk="delete")
def disable_skill(ctx, skill_name: str):
    p = skills_dir() / f"{skill_name}.py"
    if not p.exists():
        return f"There's no learned skill called {skill_name}. {list_skills(ctx)}"
    off = skills_dir() / "_disabled"
    off.mkdir(exist_ok=True)
    _unload(skill_name)
    shutil.move(str(p), off / p.name)
    return f"Skill {skill_name} switched off. Say 'restore skill {skill_name}' to bring it back."


@tool("restore_skill", "Switch a disabled learned skill back on.",
      {"type": "object", "properties": {"skill_name": {"type": "string"}}, "required": ["skill_name"]})
def restore_skill(ctx, skill_name: str):
    src = skills_dir() / "_disabled" / f"{skill_name}.py"
    if not src.exists():
        return f"There's no switched-off skill called {skill_name}."
    dst = skills_dir() / src.name
    shutil.move(str(src), dst)
    try:
        return f"Skill {skill_name} is back on: {', '.join(load_skill(dst))}."
    except Exception as e:
        ERRORS[skill_name] = str(e)
        return f"Error: restored the file but it failed to load: {e}"


@tool("reload_skills", "Reload all learned skills from the skills folder (after editing them by hand or with Claude Code).")
def reload_skills(ctx):
    r = load_all()
    msg = f"Reloaded. Active learned skills: {', '.join(r['loaded']) or 'none'}."
    if r["errors"]:
        msg += " Failed: " + "; ".join(f"{n}: {e[:100]}" for n, e in r["errors"].items())
    return msg
