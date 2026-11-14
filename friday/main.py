"""Friday entry point: wires together voice, brain, tools, memory, UI and the dashboard.

The approval flow lives in approvals.py, the microphone/listen cycle and the text-interrupt watcher in listening.py,
and the command line in cli.py. Assistant inherits the first two, so every method still runs on one object."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser

from . import __version__
from . import tools as T
from .approvals import PTT_SIGNAL, ApprovalsMixin
from .config import INPUT_MODES, load_settings
from .events import EventBus
from .listening import END_CONVERSATION, NOISE_TRANSCRIPTS, ListeningMixin
from .llm import Brain
from .memory import Memory
from .tools import REGISTRY, ToolContext
from .ui import UI
from .usage import UsageTracker
from .voice import TextToSpeech

__all__ = ["Assistant", "PTT_SIGNAL", "EXIT_PHRASES", "NOISE_TRANSCRIPTS", "END_CONVERSATION", "HELP",
           "main", "run_check", "open_dashboard_from_cli"]

EXIT_PHRASES = {"exit", "quit", "goodbye", "good bye", "bye friday", "shut down friday", "go to sleep friday", "/quit", "/exit"}
HELP = """Commands: /help  /mode always|wake|ptt|text  /mute (voice)  /mic (mute microphone)  /chat (conversation mode)
          /dashboard  /clear (forget conversation)  /tools  /quit
Say or type things like: "what's the weather", "open spotify", "play lo-fi on youtube",
"set a timer for 5 minutes", "take a screenshot", "note that I need milk", "turn the volume to 30"."""


class Assistant(ApprovalsMixin, ListeningMixin):
    def __init__(self, settings, mode: str | None = None, start_dashboard: bool | None = None):
        self.s = settings
        self.mode = mode or settings.input_mode
        if self.mode not in INPUT_MODES:
            self.mode = "always"
        self.running = True
        self.inputs: "queue.Queue[str]" = queue.Queue()
        self.bus = EventBus()
        self.usage = UsageTracker(settings.data_dir)
        self.last_checks: dict[str, dict] = {}
        self._confirm_answers: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self._audio_restart = threading.Event()
        self._mic_retry_at = 0.0
        self._mic_backoff = 3.0
        self.dashboard = None
        self.dashboard_url = ""
        T.DISABLED.clear()
        T.DISABLED.update(settings.disabled_tools)
        from . import skills
        skills.load_all()  # learned skills from the skills folder (broken ones are reported, not fatal)
        from . import skill_hub
        threading.Thread(target=skill_hub.start_all, daemon=True).start()  # MCP servers from GitHub

        self.memory = Memory(settings.data_dir, settings.memory_max_messages)
        self.tts = TextToSpeech(settings)
        self.tts.barge_provider = self._barge_cfg  # talk over Jarvis to interrupt it
        self.ui = UI(settings.assistant_name, tray=settings.tray_icon, on_quit=self.quit,
                     on_toggle_mute=self.toggle_mute, bus=self.bus, on_toggle_mic=self.toggle_mic,
                     on_open_dashboard=self.open_dashboard)
        self.ctx = ToolContext(settings=settings, memory=self.memory, speak=self._announce, confirm=self.confirm,
                               control=self.control, apply_settings=self.apply_settings)
        from . import credits, sms
        from .env import env

        # Credit Gate: every paid/online AI call must be approved (voice, dashboard or SMS with PIN).
        credits.GATE = credits.CreditGate(confirm=self.confirm,
                                          balance_fn=credits.abacus_balance if env("ABACUS_API_KEY") else None)
        from . import updates

        credits.GATE.on_warn = lambda msg: updates.send("credits", msg, important=True)  # every 1,000 Abacus credits
        self._brain_lock = threading.RLock()  # voice, dashboard, SMS and routines share one conversation
        self._tls = threading.local()
        self.sms = sms.get_link()
        self.sms.on_message = self._sms_message
        from . import lessons
        lessons.DATA_DIR = settings.data_dir  # where taught ways of thinking are saved
        self.bus.on_trace_entry = self._save_trace
        try:  # reminders set before the last restart come back (missed ones are announced)
            from . import tools as _tools

            _tools.restore_timers(self.ctx)
            from . import agenda

            agenda.schedule_reminders(self.ctx)  # "coming up in 15 minutes" alerts for calendar events
            from . import notify, selfmod

            _t = threading.Timer(90, selfmod.mark_verified)  # after a self-change: "this version runs fine"
            _t.daemon = True
            _t.start()
            _st = selfmod.state()
            if _st.get("status") == "rolled_back" and not _st.get("told"):
                notify.push("Jarvis", "My last self-change didn't start properly, so I put the old version back.")
                _st["told"] = True
                selfmod._save_state(_st)
        except Exception as e:
            print(f"[reminders] could not restore saved reminders: {e}")
        self.brain = Brain(settings, self.memory, self.ctx, on_tool=self._on_tool, usage=self.usage,
                           on_trace=self._on_trace)
        from . import warmup
        threading.Thread(target=lambda: warmup.warm_up(
            settings, log=lambda m: self.bus.log_event("brain", m)), daemon=True, name="warmup").start()
        self.recorder = self.stt = None
        self._stt_lock = threading.Lock()  # phone voice requests share one Whisper model
        self._phone_stt = None
        self._refresh_agents()
        if start_dashboard if start_dashboard is not None else settings.dashboard:
            self._start_dashboard()
        if self.mode != "text":
            self._init_audio()

    # ------------------------------------------------------------------ setup
    def _start_dashboard(self) -> None:
        try:
            from .dashboard import DashboardServer

            self.dashboard = DashboardServer(self, port=self.s.dashboard_port)
            self.dashboard.start()
            self.dashboard_url = self.dashboard.login_url
            self.ctx.dashboard_url = self.dashboard_url
            self.bus.agent("dashboard", "ok", f"http://127.0.0.1:{self.dashboard.port}")
            self.ui.info(f"Dashboard: {self.dashboard_url}")
        except Exception as e:
            self.dashboard = None
            self.bus.agent("dashboard", "error", "not running", error=f"Dashboard failed to start: {e}")
            self.ui.info(f"(dashboard disabled: {e})")

    def _start_input_threads(self) -> None:
        def stdin_reader():
            while self.running:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    if self.dashboard is not None:  # no console (e.g. started hidden): keep running for the dashboard
                        return
                    self.inputs.put("/quit")
                    return
                self.inputs.put(line)

        threading.Thread(target=stdin_reader, daemon=True).start()
        threading.Thread(target=self._interrupt_watch, daemon=True, name="text-interrupt").start()
        hotkeys = [(self.s.ptt_hotkey, lambda: self.inputs.put(PTT_SIGNAL), "push-to-talk"),
                   (self.s.mute_hotkey, self.toggle_mic, "mute microphone"),
                   (self.s.stop_hotkey, self.stop_speaking, "stop speaking")]
        if self.mode != "text" and any(h for h, _, _ in hotkeys):
            try:
                import keyboard

                for key, fn, label in hotkeys:
                    if key:
                        keyboard.add_hotkey(key, fn)
                        self.ui.info(f"Hotkey {key}: {label}")
            except Exception as e:
                self.ui.info(f"(hotkeys disabled: {e})")
        threading.Thread(target=self._background_loop, daemon=True).start()
        threading.Thread(target=self.sms.run_forever, args=(lambda: self.running,), daemon=True).start()
        from . import local_api, phone
        self.api = local_api.start(self._api_ask, speak=self._api_speak, listen=self._api_listen,
                                   dashboard_link=self._api_dashboard_link, approval=self.api_approval,
                                   approve=self.api_approve, live=self.api_live,
                                   ask_stream=lambda text, emit: phone.ask_stream(self, text, emit),
                                   control=lambda action: phone.control(self, action),
                                   status=lambda: phone.status(self),
                                   events=lambda: phone.open_events(self))  # only if FRIDAY_API_KEY is set

    def _background_loop(self) -> None:
        """Periodic status checks + agent refresh for the dashboard."""
        last_check = 0.0
        time.sleep(3)
        while self.running:
            self._refresh_agents()
            try:
                from . import routines

                routines.tick(self)  # morning briefing, nightly self-improvement, parts reminders
                from . import updates

                updates.tick(self)  # problems, credit warnings and check-ins: Jarvis tells you without being asked
            except Exception as e:
                self.bus.log_event("scheduler", f"routine failed: {e}", level="error")
            minutes = self.s.auto_check_minutes
            if minutes and minutes > 0 and time.time() - last_check > minutes * 60:
                last_check = time.time()
                self.run_checks()
            time.sleep(5)

    # ------------------------------------------------------------------ status / agents
    def _on_tool(self, name: str, args: dict) -> None:
        self.ui.set_status("tool", f"{name} {args if args else ''}")
        self.bus.agent("tools", detail=f"last: {name}")
        self.bus.log_event("tools", f"{name} {args if args else ''}")

    def _on_trace(self, kind: str, text: str, detail: str = "") -> None:
        """Every step of how Jarvis is thinking: shown in the console, the dashboard Thinking tab, and saved."""
        self.bus.trace(kind, text, detail)
        from .env import env_bool

        if env_bool("TRACE_CONSOLE", True):
            icon = {"heard": "EAR", "lessons": "RULES", "thought": "THINK", "web": "WEB", "tool": "DO",
                    "result": "GOT", "reply": "SAY", "escalate": "BACKUP"}.get(kind, kind.upper())
            print(f"\033[90m  [{icon}] {text[:300]}\033[0m", flush=True)  # not ui.info: that also fills the event log

    def _save_trace(self, entry: dict) -> None:
        import json

        try:
            p = self.s.data_dir / "trace.jsonl"
            if p.exists() and p.stat().st_size > 2_000_000:  # keep it small: start over after ~2 MB
                p.replace(p.with_suffix(".old.jsonl"))
            from .blackbox import redact

            safe = {**entry, "text": redact(entry.get("text", "")), "detail": redact(entry.get("detail", ""))}  # no keys on disk
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(safe, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _refresh_agents(self) -> None:
        b = self.bus
        n_tools = len(REGISTRY)
        b.agent("tools", "ok", f"{n_tools - len(T.DISABLED)}/{n_tools} enabled", activity=False)
        b.agent("scheduler", "ok", f"{len(self.ctx.timers)} active timer(s)/reminder(s)", activity=False)
        b.agent("memory", "ok", f"{len(self.memory.messages)} messages · {len(self.memory.facts)} facts", activity=False)
        listen = {"always": "always listening (wake word + follow-up)", "wake": "wake word each time",
                  "ptt": "push-to-talk", "text": "text only"}[self.mode]
        if self.s.conversation_mode and self.mode == "always":
            listen = "conversation mode (no wake word needed)"
        muted = b.flags.get("mic_muted")
        b.agent("listener", "warn" if muted else "ok", ("MIC MUTED · " if muted else "") + listen, activity=False)
        tts_detail = "muted" if self.tts.muted else f"{self.s.tts_engine}" + (
            f" · {self.s.edge_voice}" if self.s.tts_engine == "edge" else "")
        b.agent("tts", "error" if self.tts.failed else "warn" if (self.tts.last_error or self.tts.muted) else "ok",
                tts_detail, error=self.tts.last_error, activity=False)
        brain_health = "error" if self.brain.last_error else b.agents["brain"]["health"]
        if brain_health == "unknown":
            brain_health = "ok" if not self.s.validate() else "error"
        brain_detail = f"{self.s.provider} / {self.s.model}"
        if self.s.has_backup:
            brain_detail += f" · backup {self.s.backup_provider} ({self.brain.backup_used_today()}/{self.s.backup_max_per_day} today)"
        b.agent("brain", brain_health, brain_detail, error=self.brain.last_error, activity=False)
        if self.s.has_free_router:
            fr = self.last_checks.get("free_router")
            health = {"ok": "ok", "warn": "warn", "error": "error"}.get(fr["status"], "unknown") if fr else "unknown"
            b.agent("free_router", health, f"{self.s.free_router_model} · used {self.brain.free_used_today()}/"
                    f"{self.s.free_router_max_per_day} today · tried before Abacus", activity=False,
                    error=fr["summary"] if fr and fr["status"] in ("error", "warn") else "")
        else:
            b.agent("free_router", "info", "not set up (optional) - Settings > Free cloud (OmniRoute)", activity=False)
        if self.mode == "text":
            b.agent("voice_input", "info", "off (text mode)", activity=False)

    def run_checks(self, only: str | None = None) -> list[dict]:
        from . import checks

        self.bus.log_event("checks", f"Running {'all status checks' if not only else only + ' check'}...")
        if only:
            results = [checks.CHECKS[only](self.s, self)]
        else:
            results = checks.run_all(self.s, self)
        for r in results:
            self.last_checks[r["id"]] = r
            if r["status"] in ("error", "warn"):
                self.bus.log_event("checks", f"{r['name']}: {r['summary']}", level=r["status"])
            if r["id"] == "api":
                self.bus.agent("brain", "ok" if r["status"] == "ok" else "error", f"{self.s.provider} / {self.s.model}",
                               error="" if r["status"] == "ok" else r["summary"])
        self._refresh_agents()  # keeps the OmniRoute agent's health in step with its latest check
        self.bus.publish("checks", {"results": list(self.last_checks.values())})
        return results

    # ------------------------------------------------------------------ output
    def say(self, text: str) -> None:
        self.ui.assistant(text)
        self.ui.set_status("speaking")
        self.bus.agent("tts", detail=None)
        self.tts.speak(text)

    def _announce(self, text: str) -> None:  # used by timers/reminders from other threads
        self.ui.assistant(text)
        self.bus.agent("scheduler", detail=f"fired: {text[:60]}")
        self.tts.speak(text)

    # ------------------------------------------------------------------ controls (tray, hotkeys, dashboard, voice)
    def toggle_mute(self) -> bool:
        self.tts.muted = not self.tts.muted
        self.bus.set_flag("voice_muted", self.tts.muted)
        if self.tts.muted:
            self.tts.stop()
        self._refresh_agents()
        return self.tts.muted

    def toggle_mic(self) -> bool:
        return self.set_mic_muted(not self.bus.flags.get("mic_muted", False))

    def set_mic_muted(self, muted: bool) -> bool:
        self.bus.set_flag("mic_muted", muted)
        self.bus.log_event("listener", "Microphone muted." if muted else "Microphone listening.")
        if muted:
            self.bus.set_flag("conversation_until", 0.0)
            self.ui.set_status("muted", "microphone muted")
        self._refresh_agents()
        return muted

    def _barge_cfg(self):
        """Settings for the talk-over monitor, or None when it can't/shouldn't run (no mic, muted, text mode)."""
        if not self.s.barge_in or self.mode == "text" or self.recorder is None or self.bus.flags.get("mic_muted"):
            return None
        return {"device": self.recorder.device, "level": self.s.barge_in_level, "on_interrupt": self._barged_in}

    def _barged_in(self) -> None:
        self.tts.stop()
        self.tts.fast_tail_until = time.time() + 3.0  # start listening almost at once: don't lose your first words
        self.bus.log_event("tts", "Interrupted by your voice.")
        self.bus.trace("interrupt", "You talked over me: I stopped speaking and I'm listening.")
        self.bus.set_flag("conversation_until", time.time() + max(8.0, self.s.followup_seconds))  # no wake word needed

    def stop_speaking(self) -> None:
        self.tts.stop()
        self.bus.log_event("tts", "Speech stopped.")

    def set_mode(self, new: str) -> str:
        if new not in INPUT_MODES:
            return f"Unknown mode {new}."
        if new != "text" and not self.recorder:
            self.mode = new
            self._init_audio()
        self.mode = new
        self.inputs.put("")  # wake the main loop so it switches immediately
        self._refresh_agents()
        return f"Mode is now {self.mode}."

    def control(self, action: str) -> str:
        """Single entry point for controls coming from voice tools and the dashboard."""
        if action in ("mute_mic", "unmute_mic"):
            self.set_mic_muted(action == "mute_mic")
            return "Microphone muted. Unmute it from the dashboard, tray icon or hotkey." if action == "mute_mic" \
                else "Microphone is listening."
        if action == "toggle_mic":
            return "Microphone muted." if self.toggle_mic() else "Microphone listening."
        if action in ("mute_voice", "unmute_voice"):
            if self.tts.muted != (action == "mute_voice"):
                self.toggle_mute()
            return "Voice muted." if self.tts.muted else "Voice on."
        if action in ("conversation_on", "conversation_off"):
            self.s.conversation_mode = action == "conversation_on"
            self._refresh_agents()
            return "Conversation mode on. I'll answer without the wake word." if self.s.conversation_mode \
                else "Conversation mode off. Say my name when you need me."
        if action == "stop_speaking":
            self.stop_speaking()
            return "Stopped."
        if action == "restart_audio":
            self._audio_restart.set()
            self.inputs.put("")
            return "Restarting the microphone and speech recognition."
        if action == "restart_brain":
            try:
                self.brain.reload()
                self.brain.last_error = ""
                self.bus.agent("brain", "ok", f"{self.s.provider} / {self.s.model}", error="")
                return "AI brain reconnected."
            except Exception as e:
                self.bus.agent("brain", "error", error=f"Restart failed: {e}")
                return f"Restart failed: {e}"
        if action == "restart_friday":
            threading.Thread(target=self.restart_process, daemon=True).start()
            return "Restarting Friday..."
        if action.startswith("mode:"):
            return self.set_mode(action.split(":", 1)[1])
        return f"Unknown action {action}."

    def apply_settings(self, applies: list[str]) -> dict:
        """Reload .env after the dashboard saved it and apply what can be applied live.

        The new values are swapped in as one dict update rather than field by field: the audio and brain threads read
        self.s while this runs, and a half-applied Settings is a torn read (e.g. a new base_url with the old model)."""
        new = load_settings(override=True)
        old_mode = self.s.input_mode
        fresh = {f: getattr(new, f) for f in self.s.__dataclass_fields__ if f != "data_dir"}
        self.s.__dict__.update(fresh)  # one atomic swap; identity is preserved for ctx/brain/tts
        T.DISABLED.clear()
        T.DISABLED.update(self.s.disabled_tools)
        self.ui.name = self.s.assistant_name
        notes = []
        if "brain" in applies:
            notes.append(self.control("restart_brain"))
        if "audio" in applies and self.mode != "text":
            notes.append(self.control("restart_audio"))
        if self.s.input_mode != old_mode:
            notes.append(self.set_mode(self.s.input_mode))
        self._refresh_agents()
        return {"restart_needed": "restart" in applies, "notes": notes}

    def open_dashboard(self) -> None:
        if self.dashboard_url:
            webbrowser.open(self.dashboard_url)

    def restart_process(self) -> None:
        time.sleep(0.8)  # let the HTTP response go out
        args = [sys.executable, "-m", "friday", *sys.argv[1:]]
        kw = {"cwd": os.getcwd()}
        try:  # the new process must re-read .env: drop the values this one copied from it (they would override edits)
            from dotenv import dotenv_values

            env = dict(os.environ)
            for k in dotenv_values(".env"):
                env.pop(k, None)
            kw["env"] = env
        except Exception:
            pass
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if self.dashboard is not None:
            self.dashboard.stop()
        try:
            from . import runlog

            runlog.mark_clean("restart requested")
        except Exception:
            pass
        subprocess.Popen(args, **kw)
        os._exit(0)

    def quit(self) -> None:
        self.running = False
        self.inputs.put("/quit")

    # ------------------------------------------------------------------ input
    def api_live(self) -> dict:
        """Everything the phone page shows about the brain: state, which AI is running, gauges, the last thinking steps."""
        from .dashboard import hud_data

        h = hud_data(self)
        h["state"] = self.bus.status.get("state", "")
        h["detail"] = self.bus.status.get("detail", "")
        h["provider"] = self.s.provider
        h["trace"] = [{"time": t["time"][11:19], "kind": t["kind"], "text": str(t["text"])[:160]}
                      for t in list(self.bus.traces)[-14:]]
        h["control_left"] = __import__("friday.computer", fromlist=["x"]).control_left()
        return h

    # ------------------------------------------------------------------ main loop
    def handle(self, text: str, via: str = "") -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("\x00DASH"):
            via, text = "dashboard", text[len("\x00DASH"):]
        low = text.lower().strip(" .!?")
        if low in EXIT_PHRASES:
            self.say("Goodbye. I'll be here if you need me.")
            self.running = False
            return
        if low.startswith("/"):
            self._command(low)
            return
        if self.tts.busy():  # a new request always cuts the current answer short
            self.tts.stop()
        self.ui.user(text, via=via)
        self.ui.set_status("thinking")
        self.bus.agent("brain", detail=f"{self.s.provider} / {self.s.model}")
        from .env import env_bool

        stream = self.tts.start_stream() if env_bool("STREAM_VOICE", True) else None  # talk while the AI is still writing
        with self._brain_lock:
            self.brain.stream_to(stream.feed if stream else None)
            try:
                reply = self.brain.chat(text)
            finally:
                spoken = self.brain.streamed_prefix if stream and stream.started else ""
                self.brain.stream_to(None)
        self.bus.agent("brain", "error" if self.brain.last_error else "ok", error=self.brain.last_error)
        if self.brain.last_route.startswith("backup"):
            used = self.brain.backup_used_today()
            self.ui.info(f"Answered by backup AI ({self.s.backup_provider}) - {self.brain.last_route[7:].strip('()')}. "
                         f"Backup uses today: {used}/{self.s.backup_max_per_day}")
        if stream is not None and stream.started:
            self.ui.assistant(reply)
            self.ui.set_status("speaking")
            rest = reply[len(spoken):].strip() if spoken and reply.startswith(spoken) else reply
            if rest:
                stream.feed(rest)  # only what hasn't been said yet (everything, if the reply changed)
            stream.finish()
        else:
            self.say(reply)
        if self.mode == "always" and self.s.followup_seconds > 0:
            self.bus.set_flag("conversation_until", time.time() + self.s.followup_seconds)

    def _sms_message(self, text: str, images: list) -> str:
        """A text from the owner's phone (photos are described by the LOCAL vision model first)."""
        from .sms import describe_image

        if images:
            seen = [describe_image(img, text) for img in images[:3]]
            text = (text + "\n" if text else "Here's a photo.\n") + "\n".join(
                f"[Photo {i + 1}, as seen by the local vision model: {d}]" for i, d in enumerate(seen))
        self.ui.user(text, via="text message")
        self._tls.via = "sms"
        try:
            with self._brain_lock:
                return self.brain.chat("(Sent by text message - keep the answer short, plain text.) " + text)
        finally:
            self._tls.via = ""

    def _api_ask(self, text: str) -> str:
        if self.tts.busy():  # asking from the phone/dashboard cuts the current answer short
            self.tts.stop()
        self.ui.user(text, via="local API")
        self._tls.via = "api"
        try:
            with self._brain_lock:
                return self.brain.chat(text)
        finally:
            self._tls.via = ""

    def _api_speak(self, text: str) -> tuple[bytes, str]:
        """Phone page: Friday's own voice as audio bytes (played on the phone, not on the PC speakers)."""
        return self.tts.render(text)

    def _api_listen(self, path: str) -> str:
        """Phone page: transcribe a recording with this PC's Whisper (loaded once, reused)."""
        with self._stt_lock:
            stt = self.stt or self._phone_stt
            if stt is None:
                from .voice import SpeechToText
                stt = self._phone_stt = SpeechToText(self.s)
            return stt.transcribe_file(path)

    def _api_dashboard_link(self) -> str:
        """Phone page 'Dashboard' button: the https Tailscale address of the dashboard, with its login token.
        Needs `tailscale serve --bg --https=8443 8765` (see README / phone_access.ps1). Only sent to the phone
        after it proved it has the API key."""
        import json
        import shutil
        import subprocess

        from .env import env

        if getattr(self, "dashboard", None) is None:
            return ""
        base = env("DASHBOARD_PUBLIC_URL").rstrip("/")
        if not base:
            exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
            try:
                out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=8).stdout
                name = (json.loads(out).get("Self", {}).get("DNSName") or "").rstrip(".")
            except Exception:
                name = ""
            if not name:
                return ""
            base = f"https://{name}:{env('DASHBOARD_HTTPS_PORT', '8443')}"
        return f"{base}/?t={self.dashboard.token}"

    def ask_brain(self, text: str) -> str:
        """For the local API and routines: one request through the shared brain."""
        with self._brain_lock:
            return self.brain.chat(text)

    def _command(self, cmd: str) -> None:
        if cmd == "/help":
            self.ui.info(HELP)
        elif cmd == "/mute":
            self.ui.info("Voice muted." if self.toggle_mute() else "Voice unmuted.")
        elif cmd == "/mic":
            self.ui.info("Microphone muted." if self.toggle_mic() else "Microphone listening.")
        elif cmd == "/chat":
            self.ui.info(self.control("conversation_off" if self.s.conversation_mode else "conversation_on"))
        elif cmd == "/dashboard":
            self.open_dashboard() if self.dashboard_url else self.ui.info("Dashboard is off.")
        elif cmd == "/clear":
            self.memory.clear()
            self.ui.info("Conversation memory cleared (long-term facts kept).")
        elif cmd == "/tools":
            self.ui.info("\n".join(f"  {t.name}{' (asks permission)' if t.dangerous else ''}"
                                   f"{' [disabled]' if t.name in T.DISABLED else ''}: {t.description}"
                                   for t in REGISTRY.values()))
        elif cmd.startswith("/mode"):
            new = cmd.split()[-1]
            self.ui.info(self.set_mode(new) if new in INPUT_MODES else "Usage: /mode " + "|".join(INPUT_MODES))
        else:
            self.ui.info("Unknown command. " + HELP)

    def run(self) -> None:
        self._start_input_threads()
        greeting_name = f", {self.s.user_name}" if self.s.user_name else ""
        self.say(f"{self.s.assistant_name} online. How can I help{greeting_name}?")
        while self.running:
            self._idle_status()
            try:
                if self.mode in ("always", "wake"):
                    self._listen_cycle()
                else:
                    item = self.inputs.get()
                    if item in ("", PTT_SIGNAL) and self.mode == "ptt":
                        if item == "" and self._audio_restart.is_set():
                            self._init_audio()
                            continue
                        heard = self.listen_command()
                        self.handle(heard) if heard else self.ui.info("I didn't catch that.")
                    else:
                        self.handle(item if item != PTT_SIGNAL else "")
            except KeyboardInterrupt:
                break
            except Exception as e:  # keep the assistant alive no matter what
                self.ui.set_status("error", f"{type(e).__name__}: {e}")
                time.sleep(0.5)
        if self.dashboard is not None:
            self.dashboard.stop()
        self.ui.close()


# The command line lives in cli.py; re-exported here so `python -m friday` and `from friday.main import main` work.
from .cli import main, open_dashboard_from_cli, run_check  # noqa: E402
