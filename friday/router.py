"""Which brain answers: the local model, the free cloud tier, Abacus, or Claude.

Split out of llm.py so the routing order can be read (and tested) on its own. Brain inherits this mixin, so every
method still runs with `self` = the Brain instance and nothing about the call path changes."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from . import brainmode, claude_tiers
from . import tools as T
from .credits import CreditsDenied, get_gate
from .env import env, is_local_url
from .guards import ESCALATE_TOKEN, STUCK_REPLY, is_guard_fail

FREE_COOLDOWN_SECONDS = 300  # how long the free cloud tier is skipped after it reports a rate limit


class RouterMixin:
    """The escalation ladder: free cloud (freellmapi / OmniRoute) -> Abacus -> Claude, each behind the Credit Gate."""

    # ------------------------------------------------------------------ one-off completions (no tools)
    def _local_brain(self, model: str = "") -> "Any | None":
        """A tool-less local (Ollama) brain, e.g. the coder model. None if no local server is configured."""
        from dataclasses import replace

        from .llm import Brain

        if not self.cloud and not model:
            return self
        base = self.settings.base_url if not self.cloud else env("CUSTOM_BASE_URL", "http://localhost:11434/v1")
        if not is_local_url(base):
            return None
        model = model or (self.settings.model if not self.cloud else env("CUSTOM_MODEL", "qwen2.5:7b"))
        if model not in self._local_cache:
            try:
                s = replace(self.settings, provider="custom", base_url=base, model=model, backup_provider="none",
                            api_key=env("CUSTOM_API_KEY") or "not-needed")
            except TypeError:  # not a dataclass (tests)
                return None
            self._local_cache[model] = Brain(s, None, T.ToolContext(settings=s), usage=self.usage)
        return self._local_cache[model]

    def _cloud_brain(self) -> "Any | None":
        """For one-off hard tasks (writing code): Abacus first, Claude Sonnet only when Abacus isn't available."""
        if self.backup is not None and self.backup.cloud and self._backup_allowed():
            return self.backup
        if claude_tiers.enabled() and claude_tiers.allowed(self.settings, "sonnet"):
            try:
                return self._claude_brain("sonnet")
            except Exception:
                pass
        if self.backup is not None and self.backup.cloud:
            return self.backup
        return self if self.cloud else None

    def complete(self, system: str, user: str, prefer: str = "local", task: str = "a one-off AI task",
                 kind: str = "code") -> tuple[str, str]:
        """One-off completion (no tools). prefer="local": the free local coder model (LOCAL_CODER_MODEL) first.
        prefer="cloud": the strongest cloud AI - only after the Credit Gate approved it (raises CreditsDenied)."""
        if prefer != "cloud":
            local = self._local_brain(env("LOCAL_CODER_MODEL"))
            if local is not None:
                try:
                    return local._raw_complete(system, user), local.label + " (local, free)"
                except Exception as e:
                    print(f"[brain] local AI failed for a one-off task ({type(e).__name__}: {str(e)[:80]})")
                    if self._cloud_brain() is None:
                        raise
        cloud = self._cloud_brain()
        if cloud is None:
            raise RuntimeError("No AI available for this task (local AI failed and no cloud AI is set).")
        gate = get_gate()
        lo, hi = gate.estimate(kind, user, len(system))
        with gate.task(task, "it needs the strongest AI" if prefer == "cloud" else "the local AI couldn't do it",
                       lo, hi, cloud.label, always_ask=cloud.is_anthropic) as ok:  # Claude always asks
            if not ok:
                raise CreditsDenied(gate.last_denial or "Cloud AI not approved.")
            if cloud is self.backup:
                self._count_backup_use()
            return cloud._raw_complete(system, user), cloud.label

    # ------------------------------------------------------------------ backup budget (per day, saved to disk)
    def _budget_path(self):
        d = getattr(self.settings, "data_dir", None)
        return (d / "backup_usage.json") if d else None

    def backup_used_today(self) -> int:
        p = self._budget_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
        except (OSError, ValueError):
            data = {}
        return int(data.get(datetime.now().strftime("%Y-%m-%d"), 0))

    def _count_backup_use(self) -> None:
        p = self._budget_path()
        if not p:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            p.write_text(json.dumps({today: self.backup_used_today() + 1}), encoding="utf-8")
        except OSError:
            pass

    def _backup_allowed(self) -> bool:
        if self.backup is not None and (brainmode.get() == "teach" or "abacus" in str(getattr(self.backup, "label", "")).lower()):
            return True  # Abacus has no limit in Friday (teach mode: it IS the main brain). Only Claude is still capped.
        return self.backup is not None and self.backup_used_today() < max(0, self.settings.backup_max_per_day)

    # ---- free cloud tier (OmniRoute): its own small daily counter, no credits involved
    def _free_path(self):
        d = getattr(self.settings, "data_dir", None)
        return (d / "free_router_usage.json") if d else None

    def free_used_today(self) -> int:
        p = self._free_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
        except (OSError, ValueError):
            data = {}
        return int(data.get(datetime.now().strftime("%Y-%m-%d"), 0))

    def _count_free_use(self) -> None:
        p = self._free_path()
        if p:
            try:
                p.write_text(json.dumps({datetime.now().strftime("%Y-%m-%d"): self.free_used_today() + 1}),
                             encoding="utf-8")
            except OSError:
                pass

    def _free_allowed(self) -> bool:
        return (self.free_router is not None and time.time() >= getattr(self, "_free_cool_until", 0.0)
                and self.free_used_today() < max(0, self.settings.free_router_max_per_day))

    def _free_cooldown(self, error: str) -> None:
        """The free tier said it is rate-limited / out of quota: leave it alone for a few minutes instead of
        failing on it again with every request."""
        if re.search(r"429|rate.?limit|exhausted|quota|cooldown", error or "", re.I):
            self._free_cool_until = time.time() + FREE_COOLDOWN_SECONDS
            self._trace("escalate", f"free cloud tier is rate-limited: skipping it for {FREE_COOLDOWN_SECONDS // 60} minutes")

    def _claude_allowed(self) -> bool:
        return claude_tiers.enabled() and any(claude_tiers.allowed(self.settings, t) for t in claude_tiers.TIERS)

    def _can_escalate(self) -> bool:
        """Is any stronger brain available right now (free cloud tier, the paid backup, then Claude)?"""
        if self.sub or brainmode.get() == "local":  # local-only mode: nothing ever leaves the PC
            return False
        return self._free_allowed() or self._claude_allowed() or self._backup_allowed()

    def _has_escalation(self) -> bool:
        if self.sub or brainmode.get() == "local":
            return False
        return self.backup is not None or self.free_router is not None or claude_tiers.enabled()

    def _claude_brain(self, tier: str) -> "Any":
        from .llm import Brain

        cache = self.__dict__.setdefault("_claude_cache", {})
        if tier not in cache:
            saved = (self.ctx.ask_ai, getattr(self.ctx, "brain", None))  # a new Brain must not hijack the shared context
            try:
                cache[tier] = Brain(claude_tiers.settings_for(self.settings, tier), self.memory, self.ctx,
                                    on_tool=self.on_tool, usage=self.usage, on_trace=self._trace)
                cache[tier].sub = True
            finally:
                self.ctx.ask_ai, self.ctx.brain = saved
        return cache[tier]

    def _try_claude(self, user_text: str, reason: str) -> tuple[str | None, bool]:
        """Ask Claude: the cheapest tier that fits (Haiku, Sonnet; Opus only by name). Each call is approved through the
        Credit Gate and counted against that tier's daily cap. Returns (answer or None, declined_by_owner)."""
        failed: list[str] = []
        while True:
            tier = claude_tiers.choose(user_text, reason, tuple(failed))
            if not tier:
                return None, False
            if not claude_tiers.allowed(self.settings, tier):
                self._trace("escalate", f"Claude {tier} is at today's limit ({claude_tiers.cap_for(tier)})")
                failed.append(tier)
                continue
            try:
                brain = self._claude_brain(tier)
            except Exception as e:
                self._trace("escalate", f"Claude {tier} unavailable: {type(e).__name__}: {str(e)[:100]}")
                failed.append(tier)
                continue
            self._trace("escalate", f"asking Claude {tier} ({brain.label}): {reason}")
            gate = get_gate()
            lo, hi = gate.estimate("hard", user_text, self._context_chars())
            with gate.task(f"answer with Claude {tier}: \"{user_text[:60]}\"", reason, lo, hi, brain.label,
                           always_ask=True) as ok:  # Claude is never covered by the daily allowance: always ask
                if not ok:
                    return None, True
                claude_tiers.count_use(self.settings, tier)
                try:
                    reply = brain.chat(user_text)
                except Exception as e:
                    self._trace("escalate", f"Claude {tier} failed: {type(e).__name__}: {str(e)[:100]}")
                    failed.append(tier)
                    continue
                if brain.last_error or reply == STUCK_REPLY or is_guard_fail(reply):
                    failed.append(tier)
                    continue
                self.last_route = f"Claude {tier} ({reason})"
                self.last_error = ""
                return reply, False

    def _try_free(self, user_text: str, reason: str) -> str | None:
        """Ask the free cloud tier. Returns its answer, or None if it failed (then the caller goes on to Abacus)."""
        r = self.free_router
        self._trace("escalate", f"trying the free cloud tier ({r.label}): {reason}")
        self._count_free_use()
        try:
            reply = r.chat(user_text)
        except Exception as e:
            self._trace("escalate", f"free cloud tier failed: {type(e).__name__}: {str(e)[:100]}")
            self._free_cooldown(str(e))
            return None
        if r.last_error or reply == STUCK_REPLY or is_guard_fail(reply) or reply.strip(" .\"'`").upper() == ESCALATE_TOKEN:
            self._trace("escalate", f"free cloud tier couldn't answer ({(r.last_error or 'no usable answer')[:100]})")
            self._free_cooldown(r.last_error)
            return None
        self.last_route = f"free cloud ({reason})"
        self.last_error = ""
        return reply

    def _wants_backup(self, user_text: str) -> bool:
        low = user_text.lower()
        if claude_tiers.enabled() and re.search(r"\b(opus|sonnet|haiku|claude)\b", low):  # asked for Claude by name
            return True
        return any(p and p in low for p in getattr(self.settings, "escalate_phrases", []))

    def _retry_local(self, user_text: str, why: str) -> str:
        """No stronger brain will answer: let the local brain give its best try (never loops back here)."""
        if self.cloud:  # nothing local to fall back to
            return "Okay, I won't use the cloud AI for that."
        self._no_escalate = True
        try:
            self.last_route = f"main ({why})"
            return self.chat(user_text)
        finally:
            self._no_escalate = False

    def _try_backup(self, user_text: str, reason: str) -> tuple[str | None, bool, str]:
        """Ask the backup brain (Abacus). Returns (answer or None, declined_by_owner, its reply if it failed)."""
        self._trace("escalate", f"asking the backup brain ({self.backup.label}): {reason}")
        gate = get_gate()
        lo, hi = gate.estimate("hard", user_text, self._context_chars())
        with gate.task(f"answer: \"{user_text[:70]}\"", reason, lo, hi, self.backup.label) as ok:
            if not ok:
                return None, True, ""
            self._count_backup_use()
            reply = self.backup.chat(user_text)  # backup stores the exchange in memory on success
        if self.backup.last_error or reply == STUCK_REPLY or is_guard_fail(reply):
            self._trace("escalate", f"backup brain couldn't answer ({(self.backup.last_error or 'no usable answer')[:100]})")
            return None, False, reply
        self.last_error = ""
        self.last_route = f"backup ({reason})"
        return reply, False, ""

    def _escalate(self, user_text: str, reason: str, skip_free: bool = False, no_claude: bool = False) -> str:
        """Order: free cloud (freellmapi / OmniRoute) -> Abacus (reasoning + memory) -> Claude (last resort).
        Naming one yourself ('use abacus', 'ask claude') goes straight to it. Claude always asks first, with its
        estimate; if the owner says no, the request goes back to Abacus."""
        low = user_text.lower()
        if brainmode.get() == "teach":
            skip_free = True  # the teacher is Abacus: a weaker free brain would only teach bad habits
        named_abacus = "abacus" in low or no_claude
        named_claude = (not no_claude and claude_tiers.enabled()
                        and bool(re.search(r"\b(opus|sonnet|haiku|claude)\b", low)))
        # Tier 2: free cloud. Costs no credits.
        if self._free_allowed() and not (skip_free or named_abacus or named_claude):
            out = self._try_free(user_text, reason)
            if out is not None:
                return out
        # Tier 3: Abacus (credits, always through the Credit Gate, daily cap)
        failed_reply = ""
        if self.backup is not None and self._backup_allowed() and not named_claude:
            out, declined, failed_reply = self._try_backup(user_text, reason)
            if out is not None:
                return out
            if declined:
                return self._retry_local(user_text, "cloud declined")
        # Tier 4: Claude, last (Haiku -> Sonnet; Opus only when you ask by name). Credit Gate + daily caps.
        if claude_tiers.enabled() and not named_abacus:
            out, declined = self._try_claude(user_text, reason)
            if out is not None:
                return out
            if declined:
                if self.backup is not None and self._backup_allowed():  # "no" to Claude = go back to Abacus
                    out, _, again = self._try_backup(user_text, "you declined Claude")
                    if out is not None:
                        return out
                    failed_reply = again or failed_reply
                if not failed_reply:
                    return self._retry_local(user_text, "cloud declined")
        if failed_reply:  # Abacus was the last brain available and it failed: say what it said
            self.last_error = f"Backup AI: {self.backup.last_error}" if self.backup.last_error else ""
            self.last_route = f"backup ({reason})"
            return failed_reply
        return self._retry_local(user_text, "no stronger AI available")

    def _teach(self, user_text: str) -> str | None:
        """Teach mode. Abacus does the work (and every step it takes is recorded for the nightly review); the local AI only
        takes a request when a TESTED playbook covers that kind of job, or when it is about passwords / keys / logins.
        Returns the answer, or None to let the local brain answer."""
        from . import blackbox, playbooks, split

        if self.backup is None or not self._backup_allowed():
            return None
        graduated = playbooks.match(user_text)
        if graduated:
            self._trace("context", f"teach mode: the local AI takes this one (tested playbook: {graduated[0]['title']})")
            return None
        if split.touches_secrets(user_text):
            self._trace("context", "teach mode: this is about passwords or keys, so it stays on this PC")
            return None
        blackbox.begin_turn(user_text, self.backup.label)  # the teacher's steps belong to this request
        return self._escalate(user_text, "teach mode: the online AI does the job", skip_free=True, no_claude=True)

    def _context_chars(self) -> int:
        hist = self.memory.history() if self.memory is not None else []
        return 3000 + sum(len(str(m.get("content", ""))) for m in hist) + len(json.dumps(T.openai_tool_specs()))

    def _split_free(self, user_text: str) -> str | None:
        """Brain split (auto mode): general requests go to the fast free cloud brain first; private ones stay local.
        None = not routed there (or it failed), so the local brain answers as usual."""
        from . import split

        if (self.cloud or brainmode.get() != "auto" or not split.enabled() or not self._free_allowed()
                or not split.wants_free(user_text) or self._wants_backup(user_text)):
            return None
        out = self._try_free(user_text, "fast general request")
        if out is None:
            return None
        self.last_route = "free cloud (split)"  # the free brain already traced its own reply
        return out

    def _direct(self, user_text: str) -> str | None:
        """Commands handled without the AI (install <link>, list skills, status). Free and can't be fumbled."""
        try:
            from . import quick

            reply = quick.handle(self, user_text)
        except Exception as e:
            reply = f"Error: {type(e).__name__}: {str(e)[:300]}"
        if reply is None:
            return None
        self.last_route = "direct command"
        self.last_error = ""
        if self.memory is not None:
            self.memory.add("user", user_text)
            self.memory.add("assistant", reply)
        return reply
