"""Which brain does the talking: online (Claude) or local (any model on this PC).

  auto   (default) - the local AI answers; when it can't, Jarvis climbs: free cloud -> Claude -> Abacus, each approved first
  local            - ONLY local models. Nothing leaves the PC, no credits are ever used.
  online           - Claude answers directly (Haiku for everyday, Sonnet for hard, Opus only if you ask for it by name).
                     Every call is still approved through the Credit Gate (pre-approve a daily amount to skip the asks).
  teach            - Abacus does the work (the teacher) and everything it does is recorded. The local AI is the apprentice: it
                     takes a request over only when a playbook for that kind of job was TESTED on it (see distill.py), and
                     asks Abacus when it fails. Only the Credit Gate limits it (not the 25-a-day backup limit).
Switch any time: "go online", "go local", "back to auto" - or the set_brain_mode tool. Saved in data/brain_mode.json, so it
survives restarts. Small local models: LOCAL_FAST_MODEL answers simple chat on the CPU, so the big model stays on the GPU."""
from __future__ import annotations

import json
from pathlib import Path

from .env import data_dir
from .tools import tool

MODES = ("auto", "local", "online", "teach")


def _file() -> Path:
    return Path(data_dir()) / "brain_mode.json"


def get() -> str:
    try:
        m = json.loads(_file().read_text(encoding="utf-8")).get("mode", "auto")
    except (OSError, ValueError):
        return "auto"
    return m if m in MODES else "auto"


def set_mode(mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    _file().write_text(json.dumps({"mode": mode}), encoding="utf-8")
    return mode


DESCRIPTION = {
    "auto": "Auto: the local AI answers, and I climb to the free cloud, Claude, then Abacus only when it can't (each one approved first).",
    "local": "Local only: only models on this PC answer. Nothing goes online and no credits are used.",
    "online": "Online: Claude answers directly (Haiku for everyday, Sonnet for hard questions, Opus only when you ask for it).",
    "teach": "Teach: Abacus does the work and everything it does is recorded. The local AI learns from it at night and takes over a "
             "kind of job only after it passed its test.",
}


@tool("set_brain_mode", "Switch which AI answers: 'online' (Claude), 'local' (only models on this PC, nothing leaves the "
      "PC, no credits) or 'auto' (local first, climbs to the cloud only when needed). Use when the owner says 'go online', "
      "'go local', 'use Claude', 'offline mode', 'back to normal'. 'teach' = Abacus does the work and the local AI learns from "
      "it ('teach mode', 'you are the apprentice').",
      {"type": "object", "properties": {"mode": {"type": "string", "enum": list(MODES)}}, "required": ["mode"]})
def set_brain_mode(ctx, mode: str):
    from . import claude_tiers

    try:
        mode = set_mode(mode)
    except ValueError as e:
        return f"Error: {e}"
    note = ""
    if mode == "online" and not claude_tiers.enabled():
        note = " (No Claude API key is set yet, so I'll keep answering locally until you add ANTHROPIC_API_KEY in Settings.)"
    return DESCRIPTION[mode] + note


def installed_models() -> list[dict]:
    """Models downloaded in Ollama: [{'name', 'gb'}]. [] if Ollama isn't running."""
    import urllib.request

    from .env import ollama_url

    try:
        with urllib.request.urlopen(ollama_url() + "/api/tags", timeout=5) as r:
            rows = json.loads(r.read()).get("models", [])
    except Exception:
        return []
    return [{"name": m.get("name", ""), "gb": round(m.get("size", 0) / 1e9, 1)} for m in rows if m.get("name")]


def match_model(want: str, models: list[dict]) -> tuple[str, str]:
    """(exact name, '') or ('', why not). 'nemotron' finds 'nemotron-3.5-lightning:30b' if only one model matches."""
    w = (want or "").strip().lower()
    names = [m["name"] for m in models]
    if not w:
        return "", "Tell me which model."
    exact = [n for n in names if n.lower() == w or n.lower() == w + ":latest"]
    if exact:
        return exact[0], ""
    part = [n for n in names if w in n.lower() and "embed" not in n.lower()]
    if len(part) == 1:
        return part[0], ""
    if not part:
        return "", f"I don't have a model matching '{want}'. Installed: " + ", ".join(names or ["(Ollama isn't answering)"])
    return "", "More than one matches: " + ", ".join(part) + ". Which one?"


@tool("list_local_models", "List the AI models downloaded on this PC (Ollama) and which one is the main brain. Read-only.",
      {"type": "object", "properties": {}})
def list_local_models(ctx):
    from .env import env

    cur = env("CUSTOM_MODEL", "")
    rows = installed_models()
    if not rows:
        return "Ollama isn't answering, so I can't list the local models."
    return "Local models:\n" + "\n".join(f"- {m['name']} ({m['gb']} GB){'  <- main brain' if m['name'] == cur else ''}" for m in rows)


@tool("switch_local_model", "Change which downloaded local model is the main brain (for example 'qwen2.5:14b' for speed, "
      "or a bigger one for quality). Asks first; reconnects the brain right away.",
      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, dangerous=True, risk="system")
def switch_local_model(ctx, name: str):
    import threading
    from dataclasses import fields

    from .config import load_settings
    from .settings_store import EnvStore

    model, why = match_model(name, installed_models())
    if not model:
        return why
    try:
        EnvStore().write({"CUSTOM_MODEL": model})
        new = load_settings(override=True)
        s = ctx.settings
        for f in fields(s):
            if f.name != "data_dir":
                setattr(s, f.name, getattr(new, f.name))
    except Exception as e:
        return f"Error: couldn't switch the model ({type(e).__name__}: {str(e)[:150]})."
    note = ""
    if getattr(s, "provider", "") != "custom":
        note = " (Your main brain is a cloud AI right now, so this applies when you go back to local.)"
    elif getattr(ctx, "control", None):
        ctx.control("restart_brain")
        from .warmup import warm_up

        threading.Thread(target=warm_up, args=(s,), daemon=True).start()  # load it now so the first answer isn't slow
    return f"Switched my main local brain to {model}.{note}"


@tool("brain_status", "Say which AI mode is active, which local models exist and how much Claude was used today. Read-only.",
      {"type": "object", "properties": {}})
def brain_status(ctx):
    from . import claude_tiers
    from .env import env

    s = getattr(ctx, "settings", None)
    lines = [DESCRIPTION[get()], f"Main local model: {getattr(s, 'model', '?') if getattr(s, 'provider', '') == 'custom' else env('CUSTOM_MODEL', '?')}"]
    if env("LOCAL_FAST_MODEL"):
        lines.append(f"Small fast model (simple chat, on the CPU): {env('LOCAL_FAST_MODEL')}")
    if env("LOCAL_CODER_MODEL"):
        lines.append(f"Coder model: {env('LOCAL_CODER_MODEL')}")
    lines.append(claude_tiers.summary(s))
    return "\n".join(lines)
