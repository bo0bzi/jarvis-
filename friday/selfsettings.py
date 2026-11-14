"""Jarvis fixes his own settings: how he listens, speaks and behaves.

"Let me finish talking", "less sensitive to noise", "speak faster": most of what the owner complains about is a number
or a switch, not code. These tools let Jarvis (running on Abacus) change such a setting himself, tell the owner what he
changed, apply it at once, and put it back on request.
What he may change: the Listening, Microphone & speech and Voice groups, the assistant's name, the briefing / digest times,
the shop numbers, the local model names and a few more (see ALLOWED_GROUPS). What he may NOT change, ever: anything about
permissions (autonomy, never-tools, shell), money (credit caps, allowance, approval wait), which brain / provider / web
address is used, sending (email, texts), the local API, and every secret. Those stay with the owner: Dashboard > Settings.
Every change is validated by the same code the dashboard uses, backed up first (data/backups), and logged in
data/setting_changes.jsonl; undo_setting_change puts the last one back."""
from __future__ import annotations

import json
from datetime import datetime

from . import activity
from .env import data_dir
from .settings_store import SCHEMA, SCHEMA_BY_KEY, EnvStore
from .tools import tool

ALLOWED_GROUPS = {"Listening", "Microphone & speech", "Voice", "Assistant", "Shop", "Local AI", "Backup", "General",
                  "Dashboard", "Routines"}
NEVER = {"ALLOW_SHELL", "DASHBOARD_PORT", "TRAY_ICON", "MEMORY_MAX_MESSAGES", "DISTILL_ENABLED", "DISTILL_MAX_PLAYBOOKS",
         "BLACKBOX_ENABLED", "BLACKBOX_KEEP_DAYS", "BACKUP_INCLUDE_ENV", "SHOP_READY_TEXT", "NIGHTLY_ENABLED",
         "EVOLVE_ENABLED", "BRIEFING_SMS", "DIGEST_SMS", "CALENDAR_ICS_URL"}


def why_not(key: str) -> str:
    """'' if Jarvis may change this setting by himself, else the reason in plain words."""
    f = SCHEMA_BY_KEY.get(key)
    if f is None:
        return "not a setting I know"
    if f.get("type") == "secret":
        return "a key or password, which only the owner may change"
    if key in NEVER or f.get("group") not in ALLOWED_GROUPS:
        return "about permissions, spending, which AI is used or sending, so only the owner may change it (Dashboard > Settings)"
    return ""


def _log_path():
    return data_dir() / "setting_changes.jsonl"


def _history() -> list[dict]:
    try:
        return [json.loads(x) for x in _log_path().read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _append(entry: dict) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), **entry}) + "\n")
    except OSError:
        pass


def _current(key: str) -> str:
    f = SCHEMA_BY_KEY[key]
    return EnvStore().read().get(key, str(f.get("default", "")))


def _apply(ctx, applies) -> str:
    """Make the change take effect now when it can. Returns a short sentence for the owner."""
    hook = getattr(ctx, "apply_settings", None)
    if "restart" in applies:
        return "It needs a restart to take effect."
    if hook is None:
        return "It takes effect the next time I start."
    try:
        notes = hook(list(applies)).get("notes") or []
    except Exception as e:
        return f"Saved, but applying it failed ({type(e).__name__}); it will apply at the next start."
    return "It is in effect now." + ("" if not notes else " " + " ".join(str(n) for n in notes))


@tool("list_settings", "Look up my own settings by a word (like 'silence', 'noise', 'voice', 'interrupt'): the name, what it "
      "does, its current value, and whether I may change it myself. Read-only. Use before change_setting.",
      {"type": "object", "properties": {"search": {"type": "string"}}})
def list_settings(ctx, search: str = ""):
    words = [w for w in (search or "").lower().split() if len(w) > 2]
    vals = EnvStore().read()
    rows = []
    for f in SCHEMA:
        text = f"{f['key']} {f['label']} {f.get('help', '')} {f['group']}".lower()
        if words and not any(w in text for w in words):
            continue
        shown = "(secret)" if f.get("type") == "secret" else vals.get(f["key"], str(f.get("default", "")))
        can = "I can change it" if not why_not(f["key"]) else "owner only"
        rows.append(f"{f['key']} = {shown or '(empty)'} - {f['label']} [{can}]" + (f" Help: {f['help']}" if f.get("help") else ""))
    return "\n".join(rows[:14]) + (f"\n(+{len(rows) - 14} more: use a more specific word)" if len(rows) > 14 else "") if rows \
        else "No setting matches that."


@tool("change_setting", "Change one of MY OWN settings when something about how I listen, speak or behave is wrong and a number "
      "or switch would fix it (for example SILENCE_SECONDS so I don't cut the owner off, ENERGY_THRESHOLD or BARGE_IN_LEVEL "
      "for noise and interrupting, EDGE_RATE for voice speed). Look it up with list_settings first. It is validated, backed up "
      "and applied at once. Tell the owner exactly what you changed, the old value and the new one, and that they can say "
      "'undo that setting'. Permissions, credits, which AI is used and keys can't be changed this way.",
      {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]})
def change_setting(ctx, key: str, value: str):
    key = (key or "").strip().upper()
    reason = why_not(key)
    if reason:
        return f"I can't change {key}: it is {reason}. Tell the owner."
    old = _current(key)
    try:
        res = EnvStore().write({key: str(value)})
    except ValueError as e:
        bad = e.args[0] if e.args and isinstance(e.args[0], dict) else {key: str(e)}
        return "Not changed: " + "; ".join(f"{k}: {v}" for k, v in bad.items())
    if not res["changed"]:
        return f"{key} is already {old}. Nothing to change."
    new = _current(key)
    _append({"key": key, "old": old, "new": new, "backup": res.get("backup")})
    activity.log("settings", f"Jarvis changed {key}: {old} -> {new}")
    label = SCHEMA_BY_KEY[key]["label"]
    return f"Changed {label} ({key}) from {old} to {new}. " + _apply(ctx, res["apply"]) + " Say 'undo that setting' to put it back."


@tool("undo_setting_change", "Put back the last setting I changed myself (the old value). Use when the owner says 'undo that "
      "setting' or the change made things worse.", {"type": "object", "properties": {}})
def undo_setting_change(ctx):
    done = {h["key"] + h["time"] for h in _history() if h.get("undone_of")}
    todo = [h for h in _history() if not h.get("undone_of") and h["key"] + h["time"] not in done]
    if not todo:
        return "I haven't changed any setting myself, so there is nothing to undo."
    last = todo[-1]
    try:
        res = EnvStore().write({last["key"]: last["old"]})
    except ValueError as e:
        return f"I couldn't put {last['key']} back: {str(e)[:120]}"
    _append({"key": last["key"], "old": last["new"], "new": last["old"], "undone_of": last["time"]})
    activity.log("settings", f"Jarvis put {last['key']} back to {last['old']}")
    return f"Put {last['key']} back to {last['old']} (it was {last['new']}). " + _apply(ctx, res["apply"])
