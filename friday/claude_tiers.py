"""Claude as Jarvis's cloud brains - Haiku, Sonnet, Opus - used sparingly.

Order of preference for a hard question: local AI -> free cloud (OmniRoute) -> Claude (this file) -> Abacus.
The cheapest Claude that can do the job is chosen:
  Haiku  - quick, cheap: the default for ordinary "the local AI couldn't" questions
  Sonnet - hard questions: long, multi-step, code, planning, analysis
  Opus   - only when you ask for it by name ("use Opus", "the best model") - never automatically, unless
           CLAUDE_AUTO_OPUS=true
Every call still goes through the Credit Gate (you approve it), and each tier has its own daily cap
(CLAUDE_MAX_HAIKU_PER_DAY / _SONNET_ / _OPUS_), counted in data/claude_usage.json.
Needs ANTHROPIC_API_KEY. CLAUDE_TIERS=false turns the whole thing off."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import is_placeholder
from .env import env, env_bool, env_int

TIERS = ("haiku", "sonnet", "opus")
_DEFAULT_MODEL = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
_DEFAULT_CAP = {"haiku": 40, "sonnet": 12, "opus": 3}

_OPUS = re.compile(r"\b(opus|the best (?:model|ai|brain)|most (?:powerful|advanced) (?:model|ai|brain)|strongest (?:model|ai|brain))\b", re.I)
_SONNET = re.compile(r"\bsonnet\b", re.I)
_HAIKU = re.compile(r"\bhaiku\b", re.I)
_HARD = re.compile(r"\b(write|debug|refactor|implement|architect|design|analy[sz]e|compare|strategy|plan out|step by step|"
                   r"explain in detail|prove|optimi[sz]e|review (?:this|my) code|business plan|legal|tax|diagnos)\w*", re.I)


def key() -> str:
    k = env("ANTHROPIC_API_KEY")
    return "" if (not k or is_placeholder(k)) else k


def enabled() -> bool:
    return bool(key()) and env_bool("CLAUDE_TIERS", True)


def model_for(tier: str) -> str:
    return env(f"CLAUDE_MODEL_{tier.upper()}", _DEFAULT_MODEL[tier])


def cap_for(tier: str) -> int:
    return env_int(f"CLAUDE_MAX_{tier.upper()}_PER_DAY", _DEFAULT_CAP[tier])


def choose(user_text: str, reason: str = "", failed: tuple[str, ...] = ()) -> str:
    """The cheapest tier that fits. `failed` = tiers already tried for this question."""
    t = user_text or ""
    if _OPUS.search(t):
        want = "opus"
    elif _SONNET.search(t):
        want = "sonnet"
    elif _HAIKU.search(t):
        want = "haiku"
    elif len(t) > 700 or _HARD.search(t) or "stuck" in reason or "too hard" in reason:
        want = "sonnet"
    else:
        want = "haiku"
    if want in failed:  # climb one step, but never to Opus on its own unless the owner allowed it
        i = TIERS.index(want) + 1
        want = TIERS[i] if i < len(TIERS) and (TIERS[i] != "opus" or env_bool("CLAUDE_AUTO_OPUS", False)) else ""
    return want


# ------------------------------------------------------------------ daily counters
def _path(settings) -> Path | None:
    d = getattr(settings, "data_dir", None)
    return (Path(d) / "claude_usage.json") if d else None


def used_today(settings, tier: str) -> int:
    p = _path(settings)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
    except (OSError, ValueError):
        data = {}
    return int(data.get(datetime.now().strftime("%Y-%m-%d"), {}).get(tier, 0))


def count_use(settings, tier: str) -> None:
    p = _path(settings)
    if not p:
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        data = {}
    today = datetime.now().strftime("%Y-%m-%d")
    month = today[:7]
    day = {k: v for k, v in data.items() if k.startswith(month)}  # this month only: the file stays tiny
    day.setdefault(today, {})
    day[today][tier] = day[today].get(tier, 0) + 1
    try:
        p.write_text(json.dumps(day), encoding="utf-8")
    except OSError:
        pass


def used_month(settings) -> int:
    """Claude calls made this calendar month, all tiers."""
    p = _path(settings)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
    except (OSError, ValueError):
        data = {}
    month = datetime.now().strftime("%Y-%m")
    return sum(sum(int(n) for n in v.values()) for k, v in data.items() if k.startswith(month) and isinstance(v, dict))


def month_cap() -> int:
    """Hard stop for the whole month (CLAUDE_MAX_CALLS_PER_MONTH), on top of the per-tier daily caps."""
    return env_int("CLAUDE_MAX_CALLS_PER_MONTH", 600)


def allowed(settings, tier: str) -> bool:
    return bool(tier) and used_today(settings, tier) < cap_for(tier) and used_month(settings) < month_cap()


def summary(settings) -> str:
    if not enabled():
        return "Claude tiers: off (no ANTHROPIC_API_KEY)."
    return "Claude tiers: " + ", ".join(
        f"{t} {model_for(t)} {used_today(settings, t)}/{cap_for(t)} today" for t in TIERS) + \
        f". This month {used_month(settings)}/{month_cap()} calls."


def settings_for(settings, tier: str):
    """Settings that talk to Claude `tier` (Haiku/Sonnet/Opus) and never escalate any further."""
    return replace(settings, provider="anthropic", api_key=key(), base_url="", model=model_for(tier),
                   backup_provider="none", free_router_key="")
