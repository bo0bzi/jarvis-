"""Autonomy dial - how much Friday may do without asking.

AUTONOMY_LEVEL in .env (or Dashboard > Settings > Autonomy & learning):
  0 = Cautious : asks before every risky action AND before any tool from a learned / downloaded skill.
  1 = Trusted  : asks before risky actions (default).
  2 = Free     : only asks for the actions that ALWAYS ask (below) and PC-level actions (commands, shutdown).
Whatever the level: sending (texts, emails), deleting, and spending money ALWAYS ask - and spending cloud
credits always goes through the Credit Gate.
Clicking/typing on the PC or in Friday's browser ("control" risk) asks every time unless the user switched on
control mode (allow_computer_control) for a few minutes. NEVER_TOOLS lists tools that are blocked completely.
"""
from __future__ import annotations

from .env import env_int, env_list

ALWAYS_ASK = {"send", "delete", "spend"}
NAMES = {0: "Cautious", 1: "Trusted", 2: "Free"}


def level() -> int:
    return max(0, min(2, env_int("AUTONOMY_LEVEL", 1)))


def blocked(t) -> str:
    if t.name in env_list("NEVER_TOOLS"):
        return f"the {t.name} tool is on your never-allowed list (NEVER_TOOLS)."
    return ""


def needs_approval(t) -> bool:
    risk = getattr(t, "risk", "")
    if risk in ALWAYS_ASK:
        return True
    if risk == "control":  # clicking/typing in the browser or on the PC: asks unless control mode is on
        from . import computer

        return not computer.control_active()
    lv = level()
    if lv == 0:
        return t.dangerous or getattr(t, "source", "built-in") != "built-in"
    if lv == 1:
        return t.dangerous
    return risk == "system"


def describe() -> str:
    lv = level()
    return (f"Autonomy level {lv} ({NAMES[lv]}). Sending, deleting and spending always ask; cloud credits always "
            "need your approval.")
