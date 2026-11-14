"""PC-control tools the LLM can call.

Add a new tool by writing a function and decorating it with @tool(...).
The decorator's JSON-schema `params` is sent to the LLM so it knows how to call it.
Mark risky tools with dangerous=True: the user must confirm before they run.
"""
from __future__ import annotations

import ctypes
import fnmatch
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = sys.platform.startswith("win")


# --------------------------------------------------------------------------- registry
@dataclass
class Tool:
    name: str
    description: str
    params: dict
    func: Callable[..., str]
    dangerous: bool = False
    source: str = "built-in"  # or "skill:<name>" for learned skills (see skills.py)
    risk: str = ""  # "send", "delete", "spend" (ALWAYS ask, at every autonomy level) or "system"
    perms: list = field(default_factory=list)  # what a skill can do (internet/files/email/run), see sandbox.py


@dataclass
class ToolContext:
    """Things tools may need: settings, memory, and callbacks into the assistant."""
    settings: Any = None
    memory: Any = None
    speak: Callable[[str], None] = lambda text: print(f"[reminder] {text}")
    confirm: Callable[[str], bool] = lambda prompt: False
    timers: list = field(default_factory=list)
    dashboard_url: str = ""
    control: Any = None  # callable(action) -> str, set by the assistant
    ask_ai: Any = None  # callable(system, user) -> (text, "provider/model"), set by Brain; used by learn_skill
    brain: Any = None  # the main Brain (routines use its local model for nightly repairs)
    apply_settings: Any = None  # callable(list of apply kinds) -> dict, set by the assistant: makes a changed setting take effect


REGISTRY: dict[str, Tool] = {}
ALIASES: dict[str, str] = {}  # invented tool names -> real ones (e.g. web_browse -> browse_web)
DISABLED: set[str] = set()  # tool names switched off in the dashboard (DISABLED_TOOLS in .env)
STATS: dict[str, dict] = {}  # per-tool call counts for the dashboard


def active_tools() -> list[Tool]:
    return [t for t in REGISTRY.values() if t.name not in DISABLED]


def tool(name: str, description: str, params: dict | None = None, dangerous: bool = False, risk: str = ""):
    def deco(func):
        REGISTRY[name] = Tool(name, description, params or {"type": "object", "properties": {}}, func,
                              dangerous or bool(risk), risk=risk)
        return func
    return deco


def _chosen(names: list[str] | None) -> list[Tool]:
    if names is None:
        return active_tools()
    return [REGISTRY[n] for n in names if n in REGISTRY and n not in DISABLED]


def openai_tool_specs(names: list[str] | None = None) -> list[dict]:
    """Schemas in OpenAI 'tools' format (no 'strict' field: RouteLLM rejects it). names: only these tools."""
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.params}}
        for t in _chosen(names)
    ]


def anthropic_tool_specs() -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.params} for t in active_tools()]


def tools_as_text(names: list[str] | None = None) -> str:
    """Compact tool list for the JSON fallback protocol."""
    lines = []
    for t in _chosen(names):
        props = t.params.get("properties", {})
        args = ", ".join(f"{k}: {v.get('type', 'string')}" for k, v in props.items())
        lines.append(f"- {t.name}({args}): {t.description}")
    return "\n".join(lines)


def dispatch(name: str, args: dict | str | None, ctx: ToolContext) -> str:
    """Run a tool by name safely. Always returns a string for the LLM."""
    name = ALIASES.get(name, name)
    t = REGISTRY.get(name)
    if t is None:
        return f"Error: unknown tool '{name}'. Available: {', '.join(t.name for t in active_tools())}"
    if name in DISABLED:
        return f"The {name} tool is switched off in the dashboard. Tell the user it is disabled."
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return f"Error: arguments for {name} were not valid JSON."
    args = args or {}
    if not isinstance(args, dict):
        return f"Error: arguments for {name} must be an object."
    from . import activity, autonomy, projects as _projects

    busy = _projects.screen_conflict(name)
    if busy:
        return busy

    blocked = autonomy.blocked(t)
    if blocked:
        activity.log("blocked", f"{name}: {blocked}")
        return f"Not allowed: {blocked} Tell the user."
    if autonomy.needs_approval(t):
        summary = ", ".join(f"{k}={str(v)[:80]}" for k, v in args.items())
        if not ctx.confirm(f"This action needs your permission: {name} ({summary}). Should I go ahead?"):
            activity.log("denied", f"{name} ({summary[:150]})")
            return "Cancelled: the user did not confirm this action. Do not retry it unless asked again."
    allowed = set(t.params.get("properties", {}))
    clean = {k: v for k, v in args.items() if k in allowed}
    st = STATS.setdefault(name, {"calls": 0, "errors": 0, "last_used": "", "last_result": ""})
    st["calls"] += 1
    st["last_used"] = datetime.now().isoformat(timespec="seconds")
    try:
        result = str(t.func(ctx, **clean))
    except TypeError as e:
        result = f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # tools must never crash the assistant
        result = f"Error while running {name}: {type(e).__name__}: {e}"
    if result.startswith("Error"):
        st["errors"] += 1
        activity.log("error", f"{name}: {result[:300]}", tool=name, source=t.source, args=clean)
    else:
        activity.log("tool", f"{name}: {result[:160]}", tool=name)
    st["last_result"] = result[:200]
    return result


# --------------------------------------------------------------------------- helpers
def _http_get(url: str, timeout: float = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Friday assistant)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _notes_path(ctx: ToolContext) -> Path:
    if ctx.settings:
        base = Path(ctx.settings.data_dir)
    else:  # no settings (tests, CLI): honour DATA_DIR like every other store, so tests never write into the real notes
        from .env import data_dir

        base = Path(data_dir())
    base.mkdir(parents=True, exist_ok=True)
    return base / "notes.txt"


# Friendly names -> Windows commands / URIs. Extend freely.
APP_ALIASES = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "command prompt": "cmd", "cmd": "cmd", "terminal": "wt", "powershell": "powershell",
    "task manager": "taskmgr", "control panel": "control", "settings": "ms-settings:",
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox", "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "outlook": "outlook", "spotify": "spotify:", "discord": "discord:", "steam": "steam:",
    "vs code": "code", "vscode": "code", "visual studio code": "code", "teams": "msteams:",
    "camera": "microsoft.windows.camera:", "store": "ms-windows-store:", "snipping tool": "snippingtool",
    "clock": "ms-clock:", "mail": "outlookmail:", "photos": "ms-photos:", "whatsapp": "whatsapp:",
}

# Process names used when closing apps.
PROCESS_ALIASES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "notepad": "notepad.exe", "calculator": "CalculatorApp.exe", "calc": "CalculatorApp.exe",
    "spotify": "Spotify.exe", "discord": "Discord.exe", "word": "WINWORD.EXE", "excel": "EXCEL.EXE",
    "powerpoint": "POWERPNT.EXE", "outlook": "OUTLOOK.EXE", "vs code": "Code.exe", "vscode": "Code.exe",
    "steam": "steam.exe", "teams": "ms-teams.exe", "paint": "mspaint.exe", "task manager": "Taskmgr.exe",
}

PROTECTED_PROCESSES = {"explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "services.exe",
                       "svchost.exe", "smss.exe", "wininit.exe", "system", "dwm.exe", "python.exe", "pythonw.exe"}


# --------------------------------------------------------------------------- tools
@tool("get_time_date", "Get the current local date and time.")
def get_time_date(ctx):
    now = datetime.now()
    return now.strftime("It is %I:%M %p on %A, %B %d, %Y.").replace(" 0", " ")


@tool("get_weather", "Get the current weather and today's forecast for a city (no key needed).",
      {"type": "object", "properties": {"city": {"type": "string", "description": "City name; omit for the default city"}}})
def get_weather(ctx, city: str = ""):
    city = city or (ctx.settings.default_city if ctx.settings else "Toronto")
    geo = json.loads(_http_get("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(city)))
    if not geo.get("results"):
        return f"I couldn't find a place called {city}."
    p = geo["results"][0]
    url = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto&forecast_days=1"
           ).format(lat=p["latitude"], lon=p["longitude"])
    w = json.loads(_http_get(url))
    c, d = w["current"], w["daily"]
    return (f"Weather in {p['name']}, {p.get('country', '')}: {WEATHER_CODES.get(c['weather_code'], 'unknown conditions')}, "
            f"{c['temperature_2m']}°C (feels like {c['apparent_temperature']}°C), humidity {c['relative_humidity_2m']}%, "
            f"wind {c['wind_speed_10m']} km/h. Today: high {d['temperature_2m_max'][0]}°C, low {d['temperature_2m_min'][0]}°C, "
            f"{d['precipitation_probability_max'][0]}% chance of precipitation.")


WEATHER_CODES = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
                 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
                 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
                 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
                 81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
                 95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


_LAUNCH_OK = re.compile(r"^[A-Za-z0-9 _.:\\/\-]+$")


def _launch_target_ok(target: str) -> bool:
    """Only plain app names, URIs (spotify:) and paths. Anything with shell metacharacters is refused: the name comes
    from the model, so 'x" & calc & "' must never reach a command line."""
    return bool(target) and len(target) <= 260 and bool(_LAUNCH_OK.match(target)) and ".." not in target


@tool("open_app", "Open an application on the PC, e.g. notepad, chrome, spotify, calculator, settings.",
      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def open_app(ctx, name: str):
    key = name.lower().strip()
    target = APP_ALIASES.get(key, key)
    if not IS_WINDOWS:
        return f"(simulated) Would open {name} -> {target}. App launching only works on Windows."
    if not _launch_target_ok(target):
        return f"I won't open {name!r}: it contains characters I don't launch."
    try:
        os.startfile(target)  # ShellExecute: searches App Paths (chrome, winword...) and handles spotify:/ms-settings:
        return f"Opening {name}."
    except OSError as e:
        try:  # 'start' as a fallback, passed as a list so no shell parses it
            subprocess.Popen(["cmd", "/c", "start", "", target])
            return f"Opening {name}."
        except OSError:
            return f"I couldn't open {name}: {e}"


@tool("close_app", "Close a running application by name, e.g. chrome, notepad, spotify.",
      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def close_app(ctx, name: str):
    import psutil

    key = name.lower().strip()
    proc_name = PROCESS_ALIASES.get(key, key if key.endswith(".exe") else key + ".exe").lower()
    if proc_name in PROTECTED_PROCESSES:
        return f"I won't close {proc_name}; it's a protected system process."
    killed = 0
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == proc_name:
            try:
                p.terminate()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return f"Closed {killed} {name} window(s)." if killed else f"{name} doesn't seem to be running."


@tool("open_website", "Open a website or URL in the default browser, e.g. youtube.com, gmail.",
      {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
def open_website(ctx, url: str):
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + (url if "." in url else url.replace(" ", "") + ".com")
    webbrowser.open(url)
    return f"Opened {url}."


@tool("play_youtube", "Play a song or video on YouTube (opens the first search result).",
      {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
def play_youtube(ctx, query: str):
    search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    try:
        html = _http_get(search_url)
        m = re.search(r'"videoId":"([\w-]{11})"', html)
    except Exception:
        m = None
    if m:
        webbrowser.open(f"https://www.youtube.com/watch?v={m.group(1)}")
        return f"Playing {query} on YouTube."
    webbrowser.open(search_url)
    return f"Opened YouTube search results for {query}."


def _press_vk(vk: int, times: int = 1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


@tool("set_volume", "Change the system volume. action: up, down, mute, unmute, or set (with level 0-100).",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["up", "down", "mute", "unmute", "set"]},
          "level": {"type": "integer", "description": "0-100, only for action=set"}}, "required": ["action"]})
def set_volume(ctx, action: str, level: int | None = None):
    if not IS_WINDOWS:
        return f"(simulated) volume {action} {level or ''}".strip()
    action = action.lower()
    try:  # Precise control through pycaw when available.
        from pycaw.pycaw import AudioUtilities

        vol = AudioUtilities.GetSpeakers().EndpointVolume
        if action == "set" and level is not None:
            vol.SetMasterVolumeLevelScalar(max(0, min(100, int(level))) / 100, None)
            vol.SetMute(0, None)
            return f"Volume set to {int(level)}%."
        if action in ("mute", "unmute"):
            vol.SetMute(1 if action == "mute" else 0, None)
            return "Muted." if action == "mute" else "Unmuted."
        cur = vol.GetMasterVolumeLevelScalar()
        new = min(1.0, cur + 0.1) if action == "up" else max(0.0, cur - 0.1)
        vol.SetMasterVolumeLevelScalar(new, None)
        return f"Volume is now {round(new * 100)}%."
    except Exception:
        pass
    # Fallback: simulate media keys (each press = 2%).
    VK_MUTE, VK_DOWN, VK_UP = 0xAD, 0xAE, 0xAF
    if action in ("mute", "unmute"):
        _press_vk(VK_MUTE)
        return "Toggled mute."
    if action == "set" and level is not None:
        _press_vk(VK_DOWN, 50)
        _press_vk(VK_UP, max(0, min(100, int(level))) // 2)
        return f"Volume set to about {int(level)}%."
    _press_vk(VK_UP if action == "up" else VK_DOWN, 5)
    return f"Volume {action}."


@tool("system_info", "Report CPU, memory, disk, battery and OS information.")
def system_info(ctx):
    import psutil

    parts = [f"{platform.system()} {platform.release()} ({platform.machine()})",
             f"CPU {psutil.cpu_percent(interval=0.5)}% used on {psutil.cpu_count()} threads"]
    mem = psutil.virtual_memory()
    parts.append(f"RAM {mem.percent}% used of {mem.total / 1e9:.1f} GB")
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    parts.append(f"main disk {disk.percent}% full, {disk.free / 1e9:.0f} GB free")
    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if bat:
        parts.append(f"battery {bat.percent:.0f}%{' charging' if bat.power_plugged else ''}")
    up = timedelta(seconds=int(time.time() - psutil.boot_time()))
    parts.append(f"up for {up}")
    return "; ".join(parts) + "."


@tool("take_screenshot", "Take a screenshot and save it to Pictures/Friday Screenshots.")
def take_screenshot(ctx):
    from PIL import ImageGrab

    folder = Path.home() / "Pictures" / "Friday Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return f"Screenshot saved to {path}."


def _timers_file(ctx: ToolContext) -> Path:
    base = getattr(ctx.settings, "data_dir", None) if ctx.settings else None
    return Path(base or ".") / "reminders.json"


def _save_timers(ctx: ToolContext) -> None:
    """Timers survive a restart: the pending ones are written to data/reminders.json."""
    if not ctx.settings:
        return
    try:
        rows = [{"id": t["id"], "due": t["due"].isoformat(timespec="seconds"), "message": t["message"]}
                for t in ctx.timers if not t.get("transient")]  # calendar alerts are rebuilt from the calendar itself
        _timers_file(ctx).write_text(json.dumps(rows), encoding="utf-8")
    except OSError:
        pass


def _beep() -> None:
    try:
        import winsound

        for _ in range(3):
            winsound.Beep(1000, 250)
            winsound.Beep(1400, 250)
    except Exception:
        pass


def _fire(ctx: ToolContext, message: str) -> None:
    """A reminder/timer is due: beep, say it, and tell the phone (so it works when you're away from the PC)."""
    try:
        from . import notify

        notify.push("Jarvis reminder", message)  # phone first: it works even if the PC speakers fail
    except Exception:
        pass
    threading.Thread(target=_beep, daemon=True).start()
    ctx.speak(message)


def _schedule(ctx: ToolContext, seconds: float, message: str, timer_id: str = "", transient: bool = False) -> None:
    def fire():
        ctx.timers[:] = [t for t in ctx.timers if t["timer"] is not timer]
        _save_timers(ctx)
        _fire(ctx, message)
    timer = threading.Timer(max(0.0, seconds), fire)
    timer.daemon = True
    timer.start()
    ctx.timers.append({"id": timer_id or f"t{int(time.time() * 1000) % 10**9}{len(ctx.timers)}", "timer": timer,
                       "due": datetime.now() + timedelta(seconds=seconds), "message": message,
                       "transient": transient})
    _save_timers(ctx)


def restore_timers(ctx: ToolContext, grace_hours: float = 6) -> int:
    """At startup: put back reminders saved before the last restart. One that came due while Jarvis was off (within
    grace_hours) is announced now, marked as missed; older ones are dropped."""
    try:
        rows = json.loads(_timers_file(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    for r in rows:
        try:
            due = datetime.fromisoformat(r["due"])
        except (KeyError, ValueError):
            continue
        left = (due - datetime.now()).total_seconds()
        if left >= 0:
            _schedule(ctx, left, r["message"], r.get("id", ""))
            n += 1
        elif -left <= grace_hours * 3600:
            _schedule(ctx, 2, f"{r['message']} (missed while I was off, due {due:%I:%M %p})", r.get("id", ""))
            n += 1
    _save_timers(ctx)
    return n


def cancel_timer(ctx: ToolContext, timer_id: str) -> bool:
    for t in list(ctx.timers):
        if t.get("id") == timer_id:
            t["timer"].cancel()
            ctx.timers.remove(t)
            _save_timers(ctx)
            return True
    return False


@tool("set_timer", "Start a countdown timer. Give the duration in seconds (e.g. 5 minutes = 300).",
      {"type": "object", "properties": {"seconds": {"type": "integer"}, "label": {"type": "string"}},
       "required": ["seconds"]})
def set_timer(ctx, seconds: int, label: str = "timer"):
    seconds = int(seconds)
    if seconds <= 0:
        return "The timer duration must be positive."
    _schedule(ctx, seconds, f"Your {label} is done.")
    mins, secs = divmod(seconds, 60)
    return f"Timer set for {f'{mins} min ' if mins else ''}{f'{secs} s' if secs else ''}".strip() + "."


@tool("set_reminder", "Set a reminder either in N minutes or at a clock time today/tomorrow (HH:MM 24h).",
      {"type": "object", "properties": {
          "message": {"type": "string"},
          "in_minutes": {"type": "number"},
          "at_time": {"type": "string", "description": "24h time like 17:30"}}, "required": ["message"]})
def set_reminder(ctx, message: str, in_minutes: float | None = None, at_time: str | None = None):
    if in_minutes is not None:
        seconds = float(in_minutes) * 60
        if seconds <= 0:
            return "Tell me when: in how many minutes, or at what time."
    elif at_time:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", at_time)
        if not m:
            return "Please give the time as HH:MM, for example 17:30."
        now = datetime.now()
        due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        seconds = (due - now).total_seconds()
    else:
        return "Tell me when: in how many minutes, or at what time."
    _schedule(ctx, seconds, f"Reminder: {message}")
    due = datetime.now() + timedelta(seconds=seconds)
    return f"Okay, I'll remind you to {message} at {due:%I:%M %p}."


@tool("list_reminders", "List active timers and reminders.")
def list_reminders(ctx):
    if not ctx.timers:
        return "You have no active timers or reminders."
    return "; ".join(f"{t['message']} at {t['due']:%I:%M:%S %p}" for t in ctx.timers)


@tool("add_note", "Save a note to the notes file.",
      {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def add_note(ctx, text: str):
    with open(_notes_path(ctx), "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {text.strip()}\n")
    try:
        from .memory_db import index

        index("note", f"[{datetime.now():%Y-%m-%d %H:%M}] {text.strip()}",
              base=Path(ctx.settings.data_dir) if ctx.settings else None)
    except Exception:
        pass
    return "Note saved."


@tool("read_notes", "Read back saved notes (most recent last).",
      {"type": "object", "properties": {"last_n": {"type": "integer"}}})
def read_notes(ctx, last_n: int = 10):
    p = _notes_path(ctx)
    if not p.exists() or not p.read_text(encoding="utf-8").strip():
        return "You have no notes yet."
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    return "\n".join(lines[-max(1, int(last_n)):])


@tool("clear_notes", "Delete all saved notes.", dangerous=True, risk="delete")
def clear_notes(ctx):
    _notes_path(ctx).write_text("", encoding="utf-8")
    return "All notes cleared."


SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata", "$recycle.bin",
             "node_modules", ".git", ".venv", "venv", "__pycache__", "system volume information"}


@tool("search_files", "Find files by name (wildcards allowed, e.g. *.pdf or *budget*) inside the user's folders. Read-only.",
      {"type": "object", "properties": {
          "pattern": {"type": "string"},
          "folder": {"type": "string", "description": "Optional: Desktop, Documents, Downloads, Pictures, Music, Videos or a path in your home folder"},
          "max_results": {"type": "integer"}}, "required": ["pattern"]})
def search_files(ctx, pattern: str, folder: str = "", max_results: int = 15):
    home = Path.home().resolve()
    root = home
    if folder:
        cand = (home / folder) if not os.path.isabs(folder) else Path(folder)
        cand = cand.resolve()
        if home not in cand.parents and cand != home:
            return "For safety I only search inside your user folder."
        root = cand
    if not root.exists():
        return f"The folder {root} doesn't exist."
    if not any(ch in pattern for ch in "*?"):
        pattern = f"*{pattern}*"
    results, scanned, deadline = [], 0, time.time() + 8
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            scanned += 1
            if fnmatch.fnmatch(fn.lower(), pattern.lower()):
                results.append(os.path.join(dirpath, fn))
                if len(results) >= max_results:
                    break
        if len(results) >= max_results or time.time() > deadline:
            break
    if not results:
        return f"No files matching {pattern} found under {root}."
    return f"Found {len(results)} file(s):\n" + "\n".join(results)


@tool("list_folder", "List what is in one of the user's folders (Downloads, Desktop, Documents, Pictures...): newest first "
      "with sizes. Read-only. Use for 'what's in my Downloads folder'.",
      {"type": "object", "properties": {"folder": {"type": "string", "description": "Downloads, Desktop, Documents, Pictures, Music, "
                                                                                     "Videos or a path inside the home folder"},
                                        "max_items": {"type": "integer"}}, "required": []})
def list_folder(ctx, folder: str = "Downloads", max_items: int = 20):
    home = Path.home().resolve()
    cand = ((home / (folder or "Downloads")) if not os.path.isabs(folder or "") else Path(folder)).resolve()
    if home not in cand.parents and cand != home:
        return "For safety I only list folders inside your user folder."
    if not cand.is_dir():
        return f"{cand} isn't a folder I can see."
    try:
        entries = sorted(cand.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        return f"I couldn't read {cand.name}: {e}"
    if not entries:
        return f"{cand.name} is empty."
    lines = []
    for p in entries[:max(1, min(int(max_items or 20), 60))]:
        try:
            st = p.stat()
        except OSError:
            continue
        size = "folder" if p.is_dir() else (f"{st.st_size / 1e6:.1f} MB" if st.st_size >= 1e6 else f"{max(1, st.st_size // 1024)} KB")
        lines.append(f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}  {p.name}  ({size})")
    more = f"\n(+{len(entries) - len(lines)} more)" if len(entries) > len(lines) else ""
    return f"{cand.name}: {len(entries)} item(s), newest first:\n" + "\n".join(lines) + more


@tool("open_file", "Open a file or folder with its default program.",
      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def open_file(ctx, path: str):
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.exists():
        return f"{p} doesn't exist."
    if IS_WINDOWS:
        os.startfile(str(p))
        return f"Opened {p.name}."
    return f"(simulated) would open {p}"


@tool("delete_file", "Move a file or folder to the Recycle Bin. Requires confirmation.",
      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, dangerous=True, risk="delete")
def delete_file(ctx, path: str):
    p = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    home = Path.home().resolve()
    if home not in p.parents:
        return "For safety I can only delete things inside your user folder."
    if not p.exists():
        return f"{p} doesn't exist."
    try:
        from send2trash import send2trash

        send2trash(str(p))
        return f"Moved {p.name} to the Recycle Bin."
    except ImportError:
        return "The send2trash package is missing, so I won't delete permanently. Run install.bat again."


@tool("run_shell_command", "Run a Windows command (cmd) and return its output. Requires confirmation.",
      {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, dangerous=True,
      risk="system")
def run_shell_command(ctx, command: str):
    if ctx.settings is not None and not ctx.settings.allow_shell:
        return "Shell commands are disabled (ALLOW_SHELL=false in .env)."
    proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    out = (proc.stdout + proc.stderr).strip()
    return f"Exit code {proc.returncode}.\n{out[:3000] or '(no output)'}"


@tool("power_action", "Lock, sleep, restart or shut down the PC. Requires confirmation.",
      {"type": "object", "properties": {"action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown"]}},
       "required": ["action"]}, dangerous=True, risk="system")
def power_action(ctx, action: str):
    if not IS_WINDOWS:
        return f"(simulated) {action}"
    cmds = {"lock": "rundll32.exe user32.dll,LockWorkStation",
            "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
            "restart": "shutdown /r /t 10", "shutdown": "shutdown /s /t 10"}
    if action not in cmds:
        return "Unknown power action."
    subprocess.Popen(cmds[action], shell=True)
    return f"Okay, {action} in progress." + (" You have 10 seconds; run 'shutdown /a' to abort." if action in ("restart", "shutdown") else "")


@tool("remember_fact", "Store a long-term fact about the user (preferences, names, etc.).",
      {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]})
def remember_fact(ctx, fact: str):
    if ctx.memory is None:
        return "Memory is not available."
    ctx.memory.remember(fact)
    return "Got it, I'll remember that."


@tool("list_facts", "List every long-term fact stored about the user (read-only). Use for 'what do you remember about me'.")
def list_facts(ctx):
    facts = list(getattr(getattr(ctx, "memory", None), "facts", None) or [])
    if not facts:
        return "I have no stored facts about you yet."
    return "Stored facts:\n" + "\n".join(f"- {f}" for f in facts[-40:])


@tool("forget_fact", "Forget stored long-term facts containing some text.",
      {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def forget_fact(ctx, text: str):
    if ctx.memory is None:
        return "Memory is not available."
    n = ctx.memory.forget(text)
    return f"Forgot {n} fact(s)."


@tool("clipboard", "Read or write the clipboard. action: read or write (with text).",
      {"type": "object", "properties": {"action": {"type": "string", "enum": ["read", "write"]}, "text": {"type": "string"}},
       "required": ["action"]})
def clipboard(ctx, action: str, text: str = ""):
    import pyperclip

    if action == "write":
        pyperclip.copy(text)
        return "Copied to clipboard."
    content = pyperclip.paste()
    return content[:2000] if content else "The clipboard is empty."


@tool("open_dashboard", "Open Friday's control dashboard (settings, status checks, agents) in the browser.")
def open_dashboard(ctx):
    url = getattr(ctx, "dashboard_url", "") or ""
    if not url:
        return "The dashboard is not running (DASHBOARD=false in .env)."
    webbrowser.open(url)
    return "Opening your dashboard."


@tool("set_listening", "Control Friday's listening: mute or unmute the microphone, or turn conversation mode on/off "
      "(conversation mode = answer everything without the wake word).",
      {"type": "object", "properties": {"action": {"type": "string",
       "enum": ["mute_mic", "unmute_mic", "conversation_on", "conversation_off"]}}, "required": ["action"]})
def set_listening(ctx, action: str):
    fn = getattr(ctx, "control", None)
    if fn is None:
        return "Listening control isn't available in this mode."
    return fn(action)


# Extra built-in skill packs (they register more tools with @tool when imported).
from . import email_tools  # noqa: E402,F401
from . import skills  # noqa: E402,F401
from . import activity, autonomy, credits, sms, memory_db, shop, backup, routines, skill_hub, knowledge, jobs, toolpick, web, computer, lessons, software, evolve, livefacts, agenda, selfmod, brainmode, projects  # noqa: E402,F401
