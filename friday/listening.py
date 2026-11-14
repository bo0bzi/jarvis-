"""Listening: microphone setup, the always-on listen cycle, and interrupting speech with typed text.

Split out of main.py. The interrupt watcher is what lets you type while Jarvis is talking: the main loop is blocked
inside TextToSpeech.speak(), so a small thread watches the input queue, stops the voice, and hands the text back to
the main loop to answer."""
from __future__ import annotations

import queue
import time

from .approvals import PTT_SIGNAL
from .voice import _norm, find_wake_word, in_followup, is_echo

# Things Whisper "hears" in silence / noise; never send these to the AI when no wake word was said.
NOISE_TRANSCRIPTS = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye", "uh", "um",
                     "hmm", "so", "okay", "the", "i", "oh"}
END_CONVERSATION = {"stop", "never mind", "nevermind", "that's all", "thats all", "that's it", "thats it",
                    "thanks that's all", "no thanks", "nothing", "cancel", "stop listening"}


class ListeningMixin:
    """Mixed into Assistant: every method runs with `self` = the Assistant instance."""

    def _init_audio(self) -> bool:
        self._audio_restart.clear()
        try:
            from .voice import Recorder, SpeechToText

            self.bus.agent("voice_input", "warn", "starting microphone...")
            self.recorder = Recorder(self.s, on_level=self.bus.set_level, hold=self._mic_hold)
            from .env import env_bool

            if env_bool("SMART_ENDPOINT", True):  # answer sooner when a pause follows a finished sentence
                from .voice import sentence_looks_finished

                def _complete(audio) -> bool:
                    stt = self.stt
                    if stt is None or not self._stt_lock.acquire(blocking=False):
                        return False
                    try:
                        return sentence_looks_finished(stt.transcribe(audio))
                    finally:
                        self._stt_lock.release()
                self.recorder.complete_check = _complete
            self.ui.info("Calibrating microphone - please stay quiet for a second...")
            ambient = self.recorder.calibrate()
            self.ui.info(f"Mic ready (noise level {ambient:.4f}, trigger level {self.recorder._trigger():.4f}).")
            self.bus.agent("voice_input", "ok", f"noise {ambient:.4f} · trigger {self.recorder._trigger():.4f}", error="")
            if self.stt is None or getattr(self.stt, "_model_name", None) != (self.s.stt_engine, self.s.whisper_model):
                self.bus.agent("stt", "warn", "loading speech model...")
                self.stt = SpeechToText(self.s)
                self.stt._model_name = (self.s.stt_engine, self.s.whisper_model)
            model = self.s.whisper_model if self.stt.engine == "whisper" else "online"
            self.bus.agent("stt", "ok", f"{self.stt.engine} ({model})", error="")
            self._mic_backoff = 3.0
            return True
        except Exception as e:
            msg = f"Microphone/speech setup failed: {type(e).__name__}: {e}"
            self.recorder = None
            self.bus.agent("voice_input", "error", "not available", error=msg)
            self.ui.set_status("error", msg)
            if self.mode in ("always", "wake"):
                self.ui.info("I'll keep retrying the microphone. You can type (or use the dashboard) meanwhile.")
                self._mic_retry_at = time.time() + self._mic_backoff
                self._mic_backoff = min(60.0, self._mic_backoff * 2)
            else:
                self.ui.info("Switching to TEXT mode. See README > Troubleshooting > Microphone.")
                self.mode = "text"
            return False

    def _mic_hold(self) -> bool:
        """Echo guard + mute: ignore the microphone while Friday speaks (and briefly after) or when muted."""
        return self.bus.flags.get("mic_muted", False) or self.tts.busy()

    def listen_command(self, wait: float = 6.0, abort=None) -> str:
        if not self.recorder or self.bus.flags.get("mic_muted"):
            return ""
        self.ui.set_status("listening", "speak now...")
        abort = abort or (lambda: False)
        try:
            audio = self.recorder.listen(wait_seconds=wait, should_abort=lambda: not self.inputs.empty() or abort())
        except Exception as e:
            self._mic_failed(e)
            return ""
        if audio is None:
            return ""
        self.ui.set_status("thinking", "transcribing...")
        return self.stt.transcribe(audio)

    def _mic_failed(self, e: Exception) -> None:
        self.recorder = None
        self.bus.agent("voice_input", "error", "lost - retrying", error=f"Microphone stopped: {type(e).__name__}: {e}")
        self.ui.set_status("error", f"Microphone stopped ({e}); retrying...")
        self._mic_retry_at = time.time() + self._mic_backoff

    def _process_queued_input(self) -> None:
        item = self.inputs.get()
        if item == PTT_SIGNAL:  # hotkey = talk without the wake word
            heard = self.listen_command()
            self.handle(heard) if heard else self.ui.info("I didn't catch that.")
        elif item:
            self.handle(item)

    def _idle_status(self) -> None:
        wake = " or ".join(f'"{w}"' for w in self.s.wake_words[:2])
        if self.mode in ("always", "wake") and self.bus.flags.get("mic_muted"):
            self.ui.set_status("muted", "microphone muted - unmute from the dashboard, tray or hotkey")
        elif self.mode == "always" and (self.s.conversation_mode or
                                        in_followup(time.time(), self.bus.flags.get("conversation_until", 0))):
            self.ui.set_status("conversation", "listening - just talk, no wake word needed")
        elif self.mode in ("always", "wake"):
            self.ui.set_status("sleeping", f"always listening - say {wake}")
        elif self.mode == "ptt":
            self.ui.set_status("idle", "Press ENTER (or the hotkey) and speak - or type a message and press ENTER.")
        else:
            self.ui.set_status("idle", "Type a message and press ENTER.")

    def _listen_cycle(self) -> None:
        """One pass of always-on listening. Returns to run() after each handled request or state change."""
        mode = self.mode
        if self._audio_restart.is_set() or (self.recorder is None and time.time() >= self._mic_retry_at):
            self.recorder = None
            if not self._init_audio():
                pass
            return
        if self.recorder is None:  # waiting to retry the mic: still serve typed/dashboard input
            try:
                item = self.inputs.get(timeout=max(0.2, min(2.0, self._mic_retry_at - time.time())))
                self.inputs.put(item)
                self._process_queued_input()
            except queue.Empty:
                pass
            return
        status_key = None
        while self.running and self.mode == mode and not self._audio_restart.is_set():
            # refresh the status line when the follow-up window closes
            key = (self.bus.flags.get("mic_muted"), self.s.conversation_mode,
                   in_followup(time.time(), self.bus.flags.get("conversation_until", 0)))
            if key != status_key:
                if status_key is not None:
                    return  # let run() update the status
                status_key = key
            try:
                followup_left = self.bus.flags.get("conversation_until", 0) - time.time()
                audio = self.recorder.listen(
                    wait_seconds=followup_left if followup_left > 0 else 30,
                    should_abort=lambda: not self.inputs.empty() or self._audio_restart.is_set()
                    or self.mode != mode)
            except Exception as e:
                self._mic_failed(e)
                return
            if audio is None:
                if not self.inputs.empty():
                    self._process_queued_input()
                    return
                continue
            self.bus.set_status("thinking", "transcribing...")  # dashboard only; keeps the console quiet
            heard = self.stt.transcribe(audio).strip()
            norm = _norm(heard)
            if not heard or (self.tts.last_spoken and time.time() - self.tts.ended_at < 8
                             and is_echo(heard, self.tts.last_spoken)):
                return
            self.bus.log_event("listener", f"heard: {heard}")
            woke, rest = find_wake_word(heard, self.s.wake_words)
            direct = mode == "always" and (self.s.conversation_mode or
                                           in_followup(time.time(), self.bus.flags.get("conversation_until", 0)))
            if woke:
                if _norm(rest) in NOISE_TRANSCRIPTS:
                    rest = ""
                if not rest:  # just the wake word
                    self.say("Yes?")
                    if mode == "always":
                        self.bus.set_flag("conversation_until", time.time() + max(8.0, self.s.followup_seconds))
                        return
                    rest = self.listen_command()
                    if not rest:
                        self.ui.info("I didn't catch that.")
                        return
                self.handle(rest, via="voice")
                return
            if direct:
                if norm in END_CONVERSATION:
                    self.bus.set_flag("conversation_until", 0.0)
                    self.ui.info("Okay - going back to waiting for the wake word.")
                    return
                if norm in NOISE_TRANSCRIPTS or len(norm) < 2:
                    return
                self.handle(heard, via="voice")
                return
            return  # speech without the wake word: ignore

    # ------------------------------------------------------------------ interrupt by typing
    def _interrupt_watch(self) -> None:
        """Let typed text cut speech short.

        While the voice is talking, the main loop is blocked inside TextToSpeech.speak(), so anything typed just sits
        in the queue. This thread watches that queue: the first item stops the voice and is put back, so the main loop
        answers it as soon as speak() returns. It stays out of the way while an approval is pending, because confirm()
        owns the input queue then."""
        while self.running:
            try:
                if not self.tts.busy() or self.bus.pending_confirm is not None:
                    time.sleep(0.08)
                    continue
                item = self.inputs.get_nowait()
            except queue.Empty:
                time.sleep(0.08)
                continue
            except Exception:
                time.sleep(0.2)
                continue
            if item == PTT_SIGNAL or not str(item).strip():
                continue  # the push-to-talk hotkey is not text: drop it rather than answer it
            self.tts.stop()
            self.bus.log_event("tts", "Interrupted by your text.")
            self.bus.trace("interrupt", "You typed while I was speaking: I stopped and I'm answering that now.")
            self.inputs.put(item)  # the main loop picks it up as soon as speak() returns
