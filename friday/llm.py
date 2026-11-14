"""The LLM 'brain': OpenAI-compatible (Abacus RouteLLM / OpenAI / custom) or Anthropic Claude,
with a tool-calling loop and a JSON fallback for models without native function calling.

The routing ladder lives in router.py, the honesty guards in guards.py and the system prompt in prompt.py; everything
they define is re-exported here so `from friday.llm import ...` keeps working."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Callable

from . import brainmode, claude_tiers
from . import tools as T
from .credits import CreditsDenied, get_gate
from .env import env, env_int, is_cloud
from .guards import (DATA_FAIL, DATA_NUDGE, ESCALATE_TOKEN, HONEST_FAIL, NUDGE, NUDGE_WEB, STUCK_REPLY,
                     _DOES_THINGS_RE, _honest_fail, _plain, data_unbacked, is_fake_promise, is_guard_fail,
                     resolve_short_reply, unbacked_claim)
from .prompt import build_system_prompt
from .router import FREE_COOLDOWN_SECONDS, RouterMixin
from .usage import extract_tokens

__all__ = [
    "Brain", "MAX_TOOL_ROUNDS", "max_steps", "build_system_prompt", "parse_json_tool_call", "strip_think",
    "is_guard_fail", "is_fake_promise", "data_unbacked", "unbacked_claim", "resolve_short_reply",
    "ESCALATE_TOKEN", "STUCK_REPLY", "HONEST_FAIL", "NUDGE", "NUDGE_WEB", "DATA_NUDGE", "DATA_FAIL",
    "FREE_COOLDOWN_SECONDS", "_honest_fail", "_plain",
]

MAX_TOOL_ROUNDS = 12  # default; AGENT_MAX_STEPS in .env overrides it (the 'thinking loop' length)


def max_steps() -> int:
    return max(2, min(40, env_int("AGENT_MAX_STEPS", MAX_TOOL_ROUNDS)))


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_RE = re.compile(r"<think.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Qwen3/3.5 'thinking' models may put their private reasoning in  thinking tags: never speak or parse it."""
    return _THINK_RE.sub("", text or "").strip()


def parse_json_tool_call(text: str) -> tuple[str, dict] | None:
    """Detect a JSON tool call in model text: {"tool": "...", "args": {...}}."""
    if not text or "tool" not in text:
        return None
    cleaned = text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:]
    m = _JSON_RE.search(cleaned)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("tool"), str) and T.ALIASES.get(obj["tool"], obj["tool"]) in T.REGISTRY:
        args = obj.get("args") or obj.get("arguments") or {}
        return T.ALIASES.get(obj["tool"], obj["tool"]), args if isinstance(args, dict) else {}
    return None


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


class Brain(RouterMixin):
    sub: bool = False  # True for the free / backup / Claude helper brains: they answer, they never hand over onward
    on_sentence: Callable[[str], None] | None = None  # voice turns: called with each finished sentence while the AI writes
    streamed_prefix: str = ""                          # the start of the reply that was already handed to on_sentence

    def stream_to(self, fn: Callable[[str], None] | None) -> None:
        self.on_sentence = fn
        self.streamed_prefix = ""

    def _speak_ready(self, content: str, upto: int, user_text: str, ran: bool) -> tuple[int, bool]:
        """Hand finished sentences of the reply-so-far to on_sentence. Returns (new position, blocked).
        Only plain prose is spoken early: JSON/tool-style text, ESCALATE, and anything the honesty guards would
        reject (a claim with no tool behind it) are never spoken - they wait for the full reply and its checks."""
        if content.lstrip().startswith(("{", "```")) or "<think" in content or "TOOL_RESULT" in content:
            return upto, content.lstrip().startswith(("{", "```"))
        pending = content[upto:]
        cuts = list(_SENT_SPLIT.finditer(pending))
        if not cuts:
            return upto, False
        chunk = pending[:cuts[-1].start()]
        if len(chunk.strip()) < 30:  # too short to be worth a separate breath: wait for the next sentence
            return upto, False
        if (ESCALATE_TOKEN in chunk.upper() or unbacked_claim(chunk, self._turn_tools)
                or (not ran and is_fake_promise(user_text, chunk))):
            return upto, True
        try:
            self.on_sentence(chunk.strip())
        except Exception:
            return upto, True
        upto += cuts[-1].end()
        self.streamed_prefix = content[:upto].strip()
        return upto, False

    def _streamed_call(self, kwargs: dict, user_text: str, ran: bool):
        """One model call, streamed: sentences go to the voice while the rest is still being written."""
        from types import SimpleNamespace as NS

        def fn(**kw):
            self.streamed_prefix = ""
            content, calls, upto, blocked = "", {}, 0, False
            for chunk in self.client.chat.completions.create(stream=True, **kw):
                if not getattr(chunk, "choices", None):
                    continue
                d = chunk.choices[0].delta
                if getattr(d, "content", None):
                    content += d.content
                for tc in getattr(d, "tool_calls", None) or []:
                    c = calls.setdefault(tc.index if tc.index is not None else len(calls),
                                         {"id": "", "name": "", "args": ""})
                    c["id"] = tc.id or c["id"]
                    if tc.function:
                        c["name"] += tc.function.name or ""
                        c["args"] += tc.function.arguments or ""
                if content and not calls and not blocked:
                    upto, blocked = self._speak_ready(content, upto, user_text, ran)
            if calls:
                self.streamed_prefix = ""  # it was a tool turn: any spoken lead-in is not part of the final reply
            msg = NS(content=content, tool_calls=[NS(id=c["id"] or f"call_{i}",
                                                      function=NS(name=c["name"], arguments=c["args"] or "{}"))
                                                  for i, c in sorted(calls.items())] or None)
            return NS(choices=[NS(message=msg)], usage=None)

        return self._create(fn, **kwargs)

    def _model_call(self, messages: list, specs, user_text: str, ran: bool):
        kwargs: dict = {"model": self.settings.model, "messages": messages}
        if specs is not None:
            kwargs.update(tools=specs, tool_choice="auto")
        if self.on_sentence is not None and not self.cloud and not self.is_anthropic:
            return self._streamed_call(kwargs, user_text, ran)
        return self._create(self.client.chat.completions.create, **kwargs)

    def __init__(self, settings, memory=None, ctx: T.ToolContext | None = None, client: Any = None,
                 on_tool: Callable[[str, dict], None] | None = None, usage=None,
                 on_trace: Callable[..., None] | None = None):
        self.settings = settings
        self.on_trace = on_trace or (lambda kind, text, detail="": None)  # live "how I'm thinking" feed
        self.memory = memory
        self.ctx = ctx or T.ToolContext(settings=settings, memory=memory)
        self.on_tool = on_tool or (lambda name, args: None)
        self.usage = usage  # optional UsageTracker (token counts shown on the dashboard)
        self.last_error = ""
        self.last_route = ""  # "main" or "backup (<reason>)" - which brain answered last
        self.backup: Brain | None = None
        self._local_cache: dict = {}
        self._no_escalate = False
        self._extra_groups: set[str] = set()  # tool groups loaded with load_tools this turn
        self._recent_tools: list[list[str]] = []  # tools used in the last turns (tool picker keeps them)
        self._turn_tools: list[str] = []
        self.reload(client)

    def reload(self, client: Any = None, backup_client: Any = None) -> None:
        """(Re)build the API client(s) from the current settings (called after the dashboard changes them)."""
        self.json_mode = self.settings.tool_mode == "json"
        self.cloud = is_cloud(self.settings)
        self._local_cache = {}
        self.is_anthropic = self.settings.provider == "anthropic"
        self.client = client or self._make_client()
        self.backup = None
        if getattr(self.settings, "has_backup", False):
            try:
                self.backup = Brain(self.settings.backup_settings(), self.memory, self.ctx, client=backup_client,
                                    on_tool=self.on_tool, usage=self.usage, on_trace=self._trace)
                self.backup.sub = True
            except Exception as e:  # e.g. SDK missing - keep the main brain working
                self.last_error = f"Backup AI unavailable: {e}"
        self.free_router = None  # free cloud tier (OmniRoute): tried before the paid backup
        if getattr(self.settings, "has_free_router", False):
            try:
                self.free_router = Brain(self.settings.free_router_settings(), self.memory, self.ctx,
                                         on_tool=self.on_tool, usage=self.usage, on_trace=self._trace)
                self.free_router.sub = True
            except Exception as e:
                self.last_error = f"Free cloud tier unavailable: {e}"
        self.ctx.ask_ai = self.complete  # lets learn_skill write code with the strongest brain
        self.ctx.brain = self

    def _trace(self, kind: str, text: str, detail: str = "") -> None:
        try:
            from . import blackbox

            blackbox.note(kind, text, brain=self.label, cloud=self.cloud)  # the full record the nightly review studies
        except Exception:
            pass
        try:
            self.on_trace(kind, text, detail)
        except Exception:
            pass

    # ------------------------------------------------------------------ plain completion (no tools)
    def _raw_complete(self, system: str, user: str) -> str:
        if self.is_anthropic:
            resp = self._create(self.client.messages.create, model=self.settings.model, max_tokens=4000,
                                system=system, messages=[{"role": "user", "content": user}])
            return " ".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        resp = self._create(self.client.chat.completions.create, model=self.settings.model,
                            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return resp.choices[0].message.content or ""

    @property
    def label(self) -> str:
        return f"{self.settings.provider}/{self.settings.model}"

    def _create(self, fn, **kwargs):
        """Call the API, timing it and recording token usage."""
        gate = get_gate() if self.cloud else None
        if gate is not None:
            gate.before_call(self.label)  # raises CreditsDenied without approval
        if self._no_think and "messages" in kwargs and "model" in kwargs:
            # Thinking models (Qwen3 / 3.5) think for many seconds before answering - too slow for voice.
            kwargs["extra_body"] = {"think": False, "reasoning_effort": "none"}
        t = time.perf_counter()
        try:
            try:
                resp = fn(**kwargs)
            except Exception as e:
                if "extra_body" not in kwargs or not any(w in str(e).lower() for w in ("think", "reasoning")):
                    raise
                self._no_think = False  # this server rejects the switch: don't send it again
                kwargs.pop("extra_body")
                resp = fn(**kwargs)
        except Exception:
            if self.usage is not None:
                self.usage.record(None, (time.perf_counter() - t) * 1000, error=True)
            raise
        if self.usage is not None:
            self.usage.record(resp, (time.perf_counter() - t) * 1000)
        if gate is not None:
            tin, tout = extract_tokens(resp)
            if not tin and not tout:  # service didn't report usage: estimate from the text sent
                tin = len(json.dumps(kwargs.get("messages", ""), default=str)) // 4
                tout = 300
            gate.charge(tin, tout)
        return resp

    @property
    def _no_think(self) -> bool:
        if getattr(self, "_think_rejected", False) or self.cloud or self.is_anthropic:
            return False
        name = str(getattr(self.settings, "model", "")).lower()
        # Only Qwen3 is switched to non-thinking: it has an "instruct" mode built for it. Nemotron was measured to INVENT
        # numbers without thinking (and is slow with it), so it is left on its own default.
        return "qwen3" in name and env("LOCAL_THINKING", "false").lower() != "true"

    @_no_think.setter
    def _no_think(self, value: bool) -> None:
        self._think_rejected = not value

    # ------------------------------------------------------------------ setup
    def _make_client(self):
        if self.is_anthropic:
            import anthropic

            return anthropic.Anthropic(api_key=self.settings.api_key)
        from openai import OpenAI

        return OpenAI(api_key=self.settings.api_key or "not-needed", base_url=self.settings.base_url or None,
                      timeout=env_int("AI_TIMEOUT_SECONDS", 240))  # big local models need time to load the first time

    # ------------------------------------------------------------------ public
    def chat(self, user_text: str) -> str:
        """Answer with the main brain. Hand over to the backup brain (costs credits) only when:
        you ask for it ("think hard"...), the main brain errors / gets stuck, or it replies ESCALATE."""
        self.last_route = self.last_route if self._no_escalate else "main"
        self.streamed_prefix = ""
        if not self._no_escalate:
            direct = self._direct(user_text)
            if direct is not None:
                return direct
            if self.sub:  # a helper brain just answers: the routing below belongs to the main brain
                return self._chat_inner(user_text)
            if not self.cloud and brainmode.get() == "teach":
                out = self._teach(user_text)  # Abacus does the work; the local AI only takes over jobs it was tested on
                if out is not None:
                    return out
            if not self.cloud and brainmode.get() == "online" and claude_tiers.enabled():
                # online mode: Claude answers directly (cheapest tier that fits; each call approved by the Credit Gate)
                self._open_turn(user_text, "claude")
                out, declined = self._try_claude(user_text, "online mode")
                if out is not None:
                    return out
                if self.backup is not None and self._backup_allowed():
                    self._trace("escalate", "online mode: Claude didn't answer" + (" (you declined)" if declined else "") +
                                "; going back to Abacus")
                    return self._escalate(user_text, "you declined Claude" if declined else "Claude didn't answer",
                                          skip_free=True, no_claude=True)
                self._trace("escalate", "online mode: Claude didn't answer" + (" (you declined)" if declined else "") +
                            "; answering locally")
            from . import split

            if (not self.cloud and brainmode.get() == "auto" and split.enabled() and self._backup_allowed()
                    and split.wants_reasoning(user_text)):
                # thinking it through, or recalling what was said before: straight to Abacus (it shares the memory)
                self._open_turn(user_text, "abacus")
                return self._escalate(user_text, "reasoning or memory question", skip_free=True)
            out = self._split_free(user_text)
            if out is not None:
                return out
        if self.cloud and get_gate().ticket is None:  # main brain is a paid cloud AI: ask first
            gate = get_gate()
            lo, hi = gate.estimate("chat", user_text, self._context_chars())
            with gate.task(f"answer: \"{user_text[:70]}\"", "your main AI brain is set to a cloud service",
                           lo, hi, self.label) as ok:
                if not ok:
                    return ("Okay, I didn't use the cloud AI. " + (gate.last_denial if "cap" in gate.last_denial else
                            "Tip: run setup_local_ai.bat so I can answer for free on this PC."))
                return self._chat_inner(user_text)
        return self._chat_inner(user_text)

    def _open_turn(self, user_text: str, who: str) -> None:
        """Start the black-box record of a request that goes straight to a stronger brain (so its steps are not filed under
        the previous request)."""
        try:
            from . import blackbox

            blackbox.begin_turn(user_text, who)
        except Exception:
            pass

    def _tool_names(self, user_text: str) -> list[str] | None:
        """None = all tools (cloud brains); otherwise the picked set for the local brain."""
        from . import toolpick

        if not toolpick.enabled(self):
            return None
        recent = [n for turn in self._recent_tools[-2:] for n in turn]
        return toolpick.pick(user_text, recent, self._extra_groups)

    def _end_turn(self) -> None:
        if self._turn_tools:
            self._recent_tools = (self._recent_tools + [list(dict.fromkeys(self._turn_tools))])[-2:]
        self._turn_tools = []
        self._texted_this_turn = False
        self._web_ready = False
        self._extra_groups = set()

    def _chat_inner(self, user_text: str) -> str:
        try:
            return self._chat_inner2(user_text)
        finally:
            self._end_turn()

    def _chat_inner2(self, user_text: str) -> str:
        if not self.sub:  # a helper brain taking over is the same request, not a new one
            try:
                from . import blackbox

                blackbox.begin_turn(user_text, self.label)
            except Exception:
                pass
        self._trace("heard", user_text[:400], f"brain: {self.label}")
        try:
            from . import lessons

            used = lessons.matching(user_text, self.ctx)
            self._trace("lessons", (f"following {len(used)} taught lesson(s): " + "; ".join(l["title"] for l in used))
                        if used else "no taught lessons apply to this message")
        except Exception:
            pass
        if self._has_escalation() and not self._no_escalate and self._wants_backup(user_text):
            if self._can_escalate():
                return self._escalate(user_text, "you asked")
        history = self.memory.history() if self.memory is not None else []
        model_text = resolve_short_reply(user_text, history)  # a bare "yes" gets its meaning spelled out
        if model_text != user_text:
            self._trace("context", "short answer: told the AI what you are answering and to act now")
        # The clock rides on the user's message (not the system prompt), so the long system prompt never changes
        # between turns and Ollama reuses its cached copy instead of re-reading it (~3 s saved per message).
        model_text += f"\n[clock: {datetime.now():%A %d %B %Y %H:%M}]"
        try:
            if getattr(self, "no_prefetch", False):  # a background project: its long brief is not a search query
                raise RuntimeError("no web prefetch")
            from . import webcontext  # read links / search the web for real before the model answers

            before = len(history)
            history = webcontext.history_with_web(self, history, user_text)
            self._web_ready = len(history) > before  # the answer is already in front of it: no tool rounds needed
        except Exception:
            pass
        try:
            if self.is_anthropic:
                reply = self._chat_anthropic(history, model_text)
            else:
                reply = self._chat_openai(history, model_text)
        except CreditsDenied as e:
            self.last_error = ""
            return f"I stopped before using more cloud credits: {e}"
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:300]}"
            if self.settings.escalate_on_error and not self._no_escalate and self._can_escalate():
                print(f"[brain] main AI failed ({self.last_error[:120]}); using backup AI")
                return self._escalate(user_text, "main AI failed")
            # Don't store failures in memory.
            return self._friendly_error(e)
        self.last_error = ""
        reply = (reply or "").strip()
        faked = is_guard_fail(reply)  # the local AI kept claiming things it never did: hand over to a stronger brain
        if self._has_escalation() and not self._no_escalate and (
                reply.strip(" .\"'`").upper() == ESCALATE_TOKEN or reply == STUCK_REPLY or faked):
            if self._can_escalate():
                why = ("the local AI kept claiming actions it never took" if faked else
                       "main AI got stuck" if reply == STUCK_REPLY else "too hard for main AI")
                return self._escalate(user_text, why)
            reply = "That one is beyond my local brain, and today's backup limit is used up. Try again tomorrow or raise the limit."
        reply = reply or "Done."
        self._trace("reply", reply[:700], f"answered by {self.last_route or 'main'} / {self.label}")
        if self.memory is not None and reply != STUCK_REPLY and not is_guard_fail(reply):  # failures would poison later turns
            self.memory.add("user", user_text)
            self.memory.add("assistant", reply)
        return reply

    # ------------------------------------------------------------------ OpenAI-compatible
    def _chat_openai(self, history: list[dict], user_text: str) -> str:
        if not self.json_mode:
            try:
                return self._openai_native(history, user_text)
            except Exception as e:
                if self.settings.tool_mode == "auto" and _looks_like_tool_rejection(e):
                    self.json_mode = True  # remember for the rest of the session
                else:
                    raise
        return self._openai_json(history, user_text)

    def _openai_native(self, history, user_text) -> str:
        sent_text, user_text = user_text, _plain(user_text)  # the model sees the clock; tool picking and guards don't
        messages = [{"role": "system", "content": build_system_prompt(self.settings, self.memory, False,
                                                                        self._can_self_escalate, user_text=user_text)},
                    *history, {"role": "user", "content": sent_text}]
        ran, nudged = False, False
        for _ in range(max_steps()):
            specs = T.openai_tool_specs(self._tool_names(user_text))  # re-picked: load_tools can add groups
            if getattr(self, "_web_ready", False) and not ran and not _DOES_THINGS_RE.search(user_text):
                # The live web data is already in front of it and the user only wants information: answer in ONE
                # model call. (Offering tools here made it open browsers and loop, ~30 s instead of ~8 s.)
                resp = self._model_call(messages, None, user_text, ran)
            else:
                resp = self._model_call(messages, specs, user_text, ran)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                text = strip_think(msg.content or "")
                parsed = parse_json_tool_call(text)  # some models answer with text-form tool calls
                if parsed:
                    name, args = parsed
                    result = self._run_tool(name, args)
                    ran = True
                    messages += [{"role": "assistant", "content": text},
                                 {"role": "user", "content": f"TOOL_RESULT {name}: {result}"}]
                    continue
                miss = unbacked_claim(text, self._turn_tools)
                if not miss and not nudged and data_unbacked(user_text, self._turn_tools) and not str(text).startswith("I can't"):
                    nudged = True
                    self._trace("guard", "answer about your own data had no tool behind it - making it check")
                    messages += [{"role": "assistant", "content": text}, {"role": "system", "content": DATA_NUDGE}]
                    continue
                if not miss and nudged and data_unbacked(user_text, self._turn_tools) and not str(text).startswith("I can't"):
                    return DATA_FAIL
                if (not ran and is_fake_promise(user_text, text)) or miss:
                    if nudged:
                        return _honest_fail(miss)
                    nudged = True
                    self._trace("guard", "caught a claim with no action behind it" + (f" ('{miss}')" if miss else ""))
                    messages += [{"role": "assistant", "content": text},
                                 {"role": "system", "content": NUDGE_WEB if getattr(self, "_web_ready", False) else NUDGE}]
                    continue
                return text
            ran = True
            thought = strip_think(msg.content or "")
            if thought:
                self._trace("thought", thought[:600])
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                               for tc in tool_calls],
            })
            for tc in tool_calls:
                result = self._run_tool(tc.function.name, tc.function.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return STUCK_REPLY

    def _openai_json(self, history, user_text) -> str:
        sent_text, user_text = user_text, _plain(user_text)
        ran, nudged = False, False
        for _ in range(max_steps()):
            system = build_system_prompt(self.settings, self.memory, True, self._can_self_escalate,
                                         self._tool_names(user_text), user_text=user_text)
            if _ == 0:
                messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": sent_text}]
            else:
                messages[0] = {"role": "system", "content": system}
            resp = self._create(self.client.chat.completions.create, model=self.settings.model, messages=messages)
            text = strip_think(resp.choices[0].message.content or "")
            parsed = parse_json_tool_call(text)
            if not parsed:
                miss = unbacked_claim(text, self._turn_tools)
                if (not ran and is_fake_promise(user_text, text)) or miss:
                    if nudged:
                        return _honest_fail(miss)
                    nudged = True
                    self._trace("guard", "caught a claim with no action behind it" + (f" ('{miss}')" if miss else ""))
                    messages += [{"role": "assistant", "content": text}, {"role": "user", "content": NUDGE}]
                    continue
                return text
            ran = True
            name, args = parsed
            result = self._run_tool(name, args)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"TOOL_RESULT {name}: {result}"}]
        return STUCK_REPLY

    # ------------------------------------------------------------------ Anthropic
    def _chat_anthropic(self, history, user_text) -> str:
        sent_text, user_text = user_text, _plain(user_text)
        system = build_system_prompt(self.settings, self.memory, False, self._can_self_escalate, user_text=user_text)
        messages = [*history, {"role": "user", "content": sent_text}]
        specs = T.anthropic_tool_specs()
        for _ in range(max_steps()):
            resp = self._create(self.client.messages.create, model=self.settings.model, max_tokens=1024, system=system,
                                messages=messages, tools=specs)
            blocks = list(resp.content)
            tool_uses = [b for b in blocks if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                return strip_think(" ".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"))
            assistant_content = []
            for b in blocks:
                if b.type == "text":
                    assistant_content.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    assistant_content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            messages.append({"role": "assistant", "content": assistant_content})
            results = [{"type": "tool_result", "tool_use_id": b.id, "content": self._run_tool(b.name, b.input)}
                       for b in tool_uses]
            messages.append({"role": "user", "content": results})
        return STUCK_REPLY

    # ------------------------------------------------------------------ helpers
    @property
    def _can_self_escalate(self) -> bool:
        return self._has_escalation() and not self._no_escalate and getattr(self.settings, "self_escalate", False)

    def _run_tool(self, name: str, args) -> str:
        try:
            shown = json.loads(args) if isinstance(args, str) and args.strip() else (args or {})
        except json.JSONDecodeError:
            shown = {"raw": args}
        self.on_tool(name, shown if isinstance(shown, dict) else {})
        if name == "text_me" and getattr(self, "_texted_this_turn", False):  # small models repeat themselves
            self._trace("tool", "text_me blocked: already texted you during this request")
            return ("Error: you already texted the owner during this request, so I did NOT send another. Don't send "
                    "again: just tell the user in one short sentence what you texted.")
        self._turn_tools.append(name)
        self._trace("tool", f"{name}({_short_args(shown)})")
        t0 = time.perf_counter()
        result = T.dispatch(name, args, self.ctx)
        try:
            from . import blackbox

            blackbox.record_action(name, shown, str(result), time.perf_counter() - t0, brain=self.label, cloud=self.cloud)
        except Exception:
            pass
        if name == "text_me" and str(result).startswith("Text sent"):
            self._texted_this_turn = True
        self._trace("result", f"{name} -> {len(str(result)):,} chars in {time.perf_counter() - t0:.1f}s",
                    str(result)[:700])
        return result

    def _friendly_error(self, e: Exception) -> str:
        msg = str(e)
        low = msg.lower()
        local = self.settings.provider == "custom" and any(h in (self.settings.base_url or "") for h in ("localhost", "127.0.0.1"))
        if local and ("connect" in low or "refused" in low or "timed out" in low or "timeout" in low):
            return "I can't reach the local AI on this PC. Make sure Ollama or your local AI server is running."
        if local and "not found" in low and "model" in low:
            return f"The local AI doesn't have the model {self.settings.model}. Run the local AI setup again to download it."
        if "401" in msg or "unauthorized" in low or "invalid api key" in low or "authentication" in low:
            return "My API key was rejected. Please check the key in the .env file."
        if "429" in msg or "rate limit" in low or "quota" in low or "credits" in low:
            return "I've hit a rate limit or run out of credits on the AI service. Try again in a moment."
        if "connect" in low or "timed out" in low or "timeout" in low:
            return "I can't reach the AI service. Please check your internet connection."
        if "model" in low and ("not found" in low or "does not exist" in low or "invalid" in low):
            return "The AI model name in the .env file wasn't accepted. Check the MODEL setting."
        return f"Sorry, something went wrong talking to the AI service: {msg[:200]}"


def _short_args(args) -> str:
    if not isinstance(args, dict):
        return str(args)[:120]
    return ", ".join(f"{k}={str(v)[:80]!r}" for k, v in args.items())


def _looks_like_tool_rejection(e: Exception) -> bool:
    low = str(e).lower()
    status = getattr(e, "status_code", None)
    return ("tool" in low or "function" in low) and (status in (400, 422, None) or "400" in low or "422" in low)
