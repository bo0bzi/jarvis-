"""Credit Gate - the strict rule: Friday NEVER uses a paid / online AI (Abacus, OpenAI, Claude API...) unless you
approved it first, with an estimate of how many credits the task needs.

How it works
  * Local AI (Ollama on this PC) is free and never asks.
  * Before any cloud AI call a task must be authorised: Friday tells you what, why and an estimated credit range,
    and waits for your yes (voice, dashboard, or SMS with your PIN).
  * The approved amount is a budget. If the task runs past it, Friday stops and asks again.
  * Daily and monthly hard caps refuse everything above them.
  * Every cloud task is logged with estimate vs actual. The credits-per-token rate is re-calibrated from Abacus'
    real credit balance when it can be read, so estimates get better over time.
The gate sits inside Brain._create (the only place API calls are made), so no skill or tool can bypass it.
"""
from __future__ import annotations

import json
import math
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable

from . import activity
from .env import data_dir, env_float, env_int
from .tools import tool


class CreditsDenied(Exception):
    """Raised when a cloud AI call is attempted without (enough) approval."""


DEFAULT_RATE = 3.0  # credits per 1,000 tokens until calibrated (deliberately cautious = over-estimates)
ALLOWANCE = "your pre-approved daily allowance"
STANDING = "your standing OK for the full review (no Friday-side cap; limit it on Abacus itself)"
UNLIMITED = "Abacus has no limit in Friday (you set it on Abacus itself); Friday only warns every 1,000 credits"


def is_abacus(provider: str) -> bool:
    """Abacus (RouteLLM) calls: no cap, no question, only a warning every ABACUS_WARN_EVERY credits."""
    return "abacus" in str(provider or "").lower()


class CreditGate:
    def __init__(self, confirm: Callable[[str], bool] | None = None, balance_fn: Callable[[], float | None] | None = None):
        self.confirm = confirm or (lambda prompt: False)
        self.balance_fn = balance_fn  # returns Abacus credits left (for calibration) or None
        self._lock = threading.RLock()
        self._tl = threading.local()   # each thread (chat, every background project) has its own open task
        self._open: list[dict] = []     # every open task, to notice when two overlap (then Abacus balance calibration is skipped)
        self.on_warn: Callable[[str], None] | None = None  # set by the assistant: says / pushes the credit warning
        self.last_denial = ""
        self._last_how = ""
        self.data = self._load()

    @property
    def ticket(self) -> dict | None:
        return getattr(self._tl, "ticket", None)

    @ticket.setter
    def ticket(self, value: dict | None) -> None:
        self._tl.ticket = value

    # ------------------------------------------------------------------ storage
    @property
    def path(self):
        return data_dir() / "credits.json"

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("days", {})
        d.setdefault("ledger", [])
        d.setdefault("preapproved_used", {})
        d.setdefault("review_days", {})  # the nightly full review: shown, but never counted against the caps
        d.setdefault("abacus_days", {})  # everyday Abacus use: shown, never capped, only warned about
        if "abacus_total" not in d:  # what was spent before this was tracked separately: no warning for the past
            d["abacus_total"] = round(sum(d["days"].values()) + sum(d["review_days"].values()), 2)
            d["abacus_warned"] = int(d["abacus_total"] // max(1, env_int("ABACUS_WARN_EVERY", 1000) or 1000))
        return d

    def _save(self) -> None:
        self.data["ledger"] = self.data["ledger"][-500:]
        for k in sorted(self.data["days"])[:-400]:
            del self.data["days"][k]
        for k in sorted(self.data["review_days"])[:-400]:
            del self.data["review_days"][k]
        for k in sorted(self.data["abacus_days"])[:-400]:
            del self.data["abacus_days"][k]
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ------------------------------------------------------------------ numbers
    @property
    def rate(self) -> float:
        """Credits per 1,000 tokens: CREDITS_PER_1K_TOKENS if you set it, else the calibrated value, else default."""
        manual = env_float("CREDITS_PER_1K_TOKENS", 0)
        return manual if manual > 0 else float(self.data.get("rate") or DEFAULT_RATE)

    def to_credits(self, tokens: int) -> float:
        return round(tokens / 1000 * self.rate, 2)

    def spent_today(self) -> float:
        return round(self.data["days"].get(datetime.now().strftime("%Y-%m-%d"), 0.0), 2)

    def review_today(self) -> float:
        return round(self.data["review_days"].get(datetime.now().strftime("%Y-%m-%d"), 0.0), 2)

    def abacus_today(self) -> float:
        """Everyday Abacus use plus the nightly review, today. No limit; shown so you can see it."""
        d = datetime.now().strftime("%Y-%m-%d")
        return round(self.data["abacus_days"].get(d, 0.0) + self.data["review_days"].get(d, 0.0), 2)

    def spent_month(self) -> float:
        m = datetime.now().strftime("%Y-%m")
        return round(sum(v for k, v in self.data["days"].items() if k.startswith(m)), 2)

    @staticmethod
    def caps() -> tuple[int, int]:
        return env_int("CREDIT_DAILY_CAP", 200), env_int("CREDIT_MONTHLY_CAP", 2000)

    def estimate(self, kind: str, text: str = "", context_chars: int = 0) -> tuple[int, int]:
        """Rough credit range for a task. kind: chat, hard, code, research, summary."""
        prompt_tokens = (context_chars + len(text)) // 4 + 400
        # kind: (min rounds, max rounds, output tokens per round)
        r_lo, r_hi, out = {"chat": (1, 3, 400), "hard": (1, 5, 1200), "code": (2, 4, 3500),
                           "research": (3, 8, 1500), "summary": (1, 2, 800)}.get(kind, (1, 4, 800))
        rounds = (r_lo, r_hi)
        lo_tokens = rounds[0] * (prompt_tokens + out)
        hi_tokens = rounds[1] * (prompt_tokens + out) * 1.3  # 30% safety margin
        return max(1, math.floor(self.to_credits(int(lo_tokens)))), max(2, math.ceil(self.to_credits(int(hi_tokens))))

    # ------------------------------------------------------------------ approvals
    def status(self) -> dict:
        day, month = self.caps()
        t = self.ticket
        return {"spent_today": self.spent_today(), "review_today": self.review_today(), "abacus_today": self.abacus_today(),
                "abacus_total": round(self.data.get("abacus_total", 0.0), 1),
                "abacus_warn_every": env_int("ABACUS_WARN_EVERY", 1000), "spent_month": self.spent_month(),
                "daily_cap": day,
                "monthly_cap": month, "rate_per_1k_tokens": self.rate, "calibrated": bool(self.data.get("rate")),
                "active_task": {k: t[k] for k in ("task", "approved", "spent")} if t else None,
                "ledger": self.data["ledger"][-30:], "last_denial": self.last_denial}

    def _cap_problem(self, amount: float) -> str:
        day, month = self.caps()
        if self.spent_today() + amount > day:
            return f"that would pass today's credit cap ({self.spent_today():g} of {day} used)"
        if self.spent_month() + amount > month:
            return f"that would pass this month's credit cap ({self.spent_month():g} of {month} used)"
        return ""

    def authorize(self, task: str, reason: str, lo: int, hi: int, provider: str = "cloud AI", extra: bool = False,
                  always_ask: bool = False, uncapped: bool = False) -> bool:
        """Ask the user (unless covered by a pre-approved daily allowance). Returns True if approved.
        always_ask=True (used for Claude) ignores the allowance: the owner is asked every time, with the estimate.
        The allowance counts the credits REALLY spent under it today, not the estimates.

        The lock is NOT held while the owner is being asked: confirm() can wait minutes (voice, dashboard or a text
        message), and holding the lock across it froze charge()/status() for the whole approval window."""
        unlimited = (uncapped or is_abacus(provider)) and not always_ask
        with self._lock:
            problem = "" if unlimited else self._cap_problem(lo)
            if problem:
                self.last_denial = f"Refused: {task} - {problem}. Raise the cap in Dashboard > Settings > Credits."
                activity.log("credits", self.last_denial)
                return False
            today = datetime.now().strftime("%Y-%m-%d")
            pre = env_int("CREDIT_PREAPPROVED_PER_DAY", 0)
            used_pre = self.data["preapproved_used"].get(today, 0)
            spent, day_cap = self.spent_today(), self.caps()[0]
            entry = {"time": datetime.now().isoformat(timespec="seconds"), "task": task[:120], "reason": reason,
                     "provider": provider, "estimate": [lo, hi], "approved": False, "how": "", "actual": 0.0}
            covered = pre > 0 and not always_ask and used_pre + lo <= pre
            if covered:
                entry.update(approved=True, how=ALLOWANCE)
            if unlimited:  # Abacus, and the nightly review: the owner sets the limit on Abacus itself
                covered = True
                entry.update(approved=True, how=STANDING if uncapped else UNLIMITED)
        if not covered:  # ask outside the lock: this is the slow part
            more = "more credits for" if extra else "the cloud AI for"
            prompt = (f"Credit check. I need {more}: {task}. Reason: {reason}. Using {provider}. "
                      f"Estimate {lo} to {hi} credits. Today so far {spent:g} of {day_cap}. "
                      f"Approve up to {hi} credits?")
            ok = bool(self.confirm(prompt))
            entry.update(approved=ok, how="you approved" if ok else "you declined")
        with self._lock:
            self.data["ledger"].append(entry)
            self._save()
            activity.log("credits", f"{'Approved' if entry['approved'] else 'Declined'} ~{lo}-{hi} credits: {task} "
                                    f"({entry['how']})", estimate=[lo, hi])
            if not entry["approved"]:
                self.last_denial = f"You declined the cloud AI for: {task}."
            self._last_how = entry["how"]
            return entry["approved"]

    @contextmanager
    def task(self, task: str, reason: str, lo: int, hi: int, provider: str = "cloud AI", always_ask: bool = False,
             uncapped: bool = False):
        """with gate.task(...) as ok:  - opens a budget; nested tasks add to the outer budget."""
        with self._lock:
            outer = self.ticket
        if outer is not None:
            ok = self.authorize(task, reason, lo, hi, provider, extra=True,
                                always_ask=always_ask or bool(outer.get("always_ask")),
                                uncapped=uncapped or bool(outer.get("uncapped")))
            if ok:
                outer["approved"] += hi
            yield ok
            return
        ok = self.authorize(task, reason, lo, hi, provider, always_ask=always_ask, uncapped=uncapped)
        if not ok:
            yield False
            return
        unlimited = (uncapped or is_abacus(provider)) and not always_ask
        self.ticket = {"task": task, "reason": reason, "approved": float(hi), "spent": 0.0, "tokens": 0,
                       "estimate": [lo, hi], "provider": provider, "start_balance": self._balance(),
                       "always_ask": always_ask, "pre": self._last_how == ALLOWANCE, "uncapped": uncapped and not always_ask,
                       "unlimited": unlimited}
        with self._lock:
            for other in self._open:
                other["overlap"] = True
            if self._open:
                self.ticket["overlap"] = True
            self._open.append(self.ticket)
        try:
            yield True
        finally:
            self._close()

    def before_call(self, provider: str = "cloud AI") -> None:
        """Called right before every cloud API request."""
        t = self.ticket
        if t is None:
            raise CreditsDenied("No approval for cloud AI use. (Friday's Credit Gate blocks unapproved credit use.)")
        if t["spent"] >= t["approved"] and t.get("unlimited"):  # Abacus / the review: keep going, nothing to ask
            t["approved"] += max(self.estimate("chat")[1], int(t["approved"] * 0.5))
        if t["spent"] >= t["approved"]:
            lo, hi = self.estimate("chat")
            if not self.authorize(f"continue '{t['task']}' (used {t['spent']:g} of {t['approved']:g} approved)",
                                  "the task needed more steps than estimated", lo, max(hi, int(t["approved"] * 0.5)),
                                  provider, extra=True, always_ask=bool(t.get("always_ask"))):
                raise CreditsDenied("The task ran past its approved credits and you declined more.")
            t["approved"] += max(hi, int(t["approved"] * 0.5))

    def charge(self, input_tokens: int, output_tokens: int) -> float:
        cost = self.to_credits(int(input_tokens) + int(output_tokens)) or 0.01
        warning = ""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            t = self.ticket or {}
            bucket = "review_days" if t.get("uncapped") else "abacus_days" if t.get("unlimited") else "days"
            self.data[bucket][today] = round(self.data[bucket].get(today, 0.0) + cost, 3)
            if bucket != "days":
                warning = self._count_abacus(cost)
            if self.ticket is not None:
                self.ticket["spent"] = round(self.ticket["spent"] + cost, 3)
                self.ticket["tokens"] += int(input_tokens) + int(output_tokens)
                if self.ticket.get("pre"):  # running on the daily allowance: count what it really costs
                    self.data["preapproved_used"] = {today: round(self.data["preapproved_used"].get(today, 0) + cost, 3)}
            self._save()
        if warning:  # outside the lock: it may speak and push to the phone
            activity.log("credits", warning)
            if self.on_warn is not None:
                try:
                    self.on_warn(warning)
                except Exception:
                    pass
        return cost

    def _count_abacus(self, cost: float) -> str:
        """Add to the running Abacus total. Returns the warning to give when it just passed another multiple of
        ABACUS_WARN_EVERY (default 1000; 0 = never warn), else ''. Nothing is ever refused."""
        self.data["abacus_total"] = round(self.data.get("abacus_total", 0.0) + cost, 3)
        every = env_int("ABACUS_WARN_EVERY", 1000)
        if every <= 0:
            return ""
        passed = int(self.data["abacus_total"] // every)
        if passed <= self.data.get("abacus_warned", 0):
            return ""
        self.data["abacus_warned"] = passed
        return (f"Abacus credits used so far: {self.data['abacus_total']:,.0f}, past {passed * every:,.0f}. "
                f"Today: {self.abacus_today():,.0f}. Friday puts no limit on Abacus; set one on abacus.ai if you want one.")

    # ------------------------------------------------------------------ calibration
    def _balance(self) -> float | None:
        if self.balance_fn is None:
            return None
        try:
            return self.balance_fn()
        except Exception:
            return None

    def _close(self) -> None:
        t, self.ticket = self.ticket, None
        if t is None:
            return
        with self._lock:
            if t in self._open:
                self._open.remove(t)
        end = self._balance() if t.get("start_balance") is not None and not t.get("overlap") else None
        actual_note = ""
        if end is not None and t["tokens"] > 0:
            real = t["start_balance"] - end
            if 0 < real < max(50.0, t["spent"] * 20):  # ignore deltas caused by other use of the account
                new_rate = real * 1000 / t["tokens"]
                old = float(self.data.get("rate") or DEFAULT_RATE)
                self.data["rate"] = round(0.6 * old + 0.4 * new_rate, 4) if self.data.get("rate") else round(new_rate, 4)
                actual_note = f" (Abacus balance says {real:g})"
        for e in reversed(self.data["ledger"]):
            if e.get("task") == t["task"][:120] and e.get("approved"):
                e["actual"] = round(t["spent"], 2)
                break
        self._save()
        activity.log("credits", f"Used {t['spent']:g} credits{actual_note} for: {t['task']} "
                                f"(estimate {t['estimate'][0]}-{t['estimate'][1]})", actual=t["spent"])


GATE: CreditGate | None = None  # set by the assistant (or lazily by get_gate)


def get_gate() -> CreditGate:
    global GATE
    if GATE is None:
        GATE = CreditGate()
    return GATE


def abacus_balance() -> float | None:
    """Credits left on Abacus (from the same endpoint the Status check uses), or None."""
    from types import SimpleNamespace

    from .checks import check_abacus_credits
    from .env import env

    r = check_abacus_credits(SimpleNamespace(provider="abacus", api_key=env("ABACUS_API_KEY")))
    left = (r.get("details") or {}).get("credits_left")
    return float(left) if isinstance(left, (int, float)) else None


@tool("credit_status", "Show cloud AI credit use: Abacus (no limit, warns every 1,000), spent today/month on the capped cloud AIs "
      "(Claude), and the last approvals with estimate vs actual.")
def credit_status(ctx) -> str:
    g = get_gate()
    s = g.status()
    lines = [f"Abacus: {s['abacus_today']:g} credits today, {s['abacus_total']:g} in total. No limit set by Friday; I warn you "
             f"every {s['abacus_warn_every']:,} credits."
             if s["abacus_warn_every"] else
             f"Abacus: {s['abacus_today']:g} credits today, {s['abacus_total']:g} in total. No limit and no warnings.",
             f"Other cloud AIs (Claude) today {s['spent_today']:g} of {s['daily_cap']} cap; this month {s['spent_month']:g} of "
             f"{s['monthly_cap']}. Rate {s['rate_per_1k_tokens']:g} credits per 1k tokens"
             f"{' (calibrated)' if s['calibrated'] else ' (estimate)'}."]
    for e in s["ledger"][-5:]:
        lines.append(f"- {e['time'][5:16]} {e['task'][:60]}: estimate {e['estimate'][0]}-{e['estimate'][1]}, "
                     f"{'approved' if e['approved'] else 'declined'}, used {e.get('actual', 0):g}")
    return "\n".join(lines)


def wait_seconds() -> int:
    return env_int("APPROVAL_WAIT_SECONDS", 180)  # long enough to pick up the phone and tap the notification

