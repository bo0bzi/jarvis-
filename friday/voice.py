"""Microphone recording, speech-to-text, wake-word detection and text-to-speech.

Heavy/optional libraries are imported lazily so text mode works even if audio packages fail.
"""
from __future__ import annotations

import asyncio
import os
import queue
import re
import tempfile
import threading
import time
from collections import deque
from typing import Callable

# Quiet the harmless Hugging Face warnings Windows prints while Whisper downloads (symlinks, no HF token).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
import warnings  # noqa: E402

warnings.filterwarnings("ignore", message=".*symlinks.*")
warnings.filterwarnings("ignore", message=".*unauthenticated requests.*")

SAMPLE_RATE = 16000
BLOCK = 480  # 30 ms at 16 kHz


# --------------------------------------------------------------------------- text helpers
def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.DOTALL)
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"[*_#`>|~]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())


def find_wake_word(transcript: str, wake_words: list[str]) -> tuple[bool, str]:
    """Return (woke, remaining_command). Longest wake phrases are checked first."""
    t = _norm(transcript)
    for w in sorted(wake_words, key=len, reverse=True):
        nw = _norm(w)
        m = re.search(rf"\b{re.escape(nw)}\b", t)
        if m:
            return True, t[m.end():].strip()
    return False, ""


def is_echo(heard: str, last_spoken: str) -> bool:
    """True if a transcript is probably Friday hearing its own voice through the speakers."""
    import difflib

    h, sp = _norm(heard), _norm(last_spoken)
    if not h or not sp:
        return False
    if len(h.split()) >= 3 and h in sp:
        return True
    return difflib.SequenceMatcher(None, h, sp).ratio() > 0.7


def in_followup(now: float, conversation_until: float) -> bool:
    return now < conversation_until


YES_WORDS = {"yes", "yeah", "yep", "sure", "confirm", "confirmed", "do it", "go ahead", "proceed", "ok", "okay",
             "affirmative", "y", "please do"}
NO_WORDS = {"no", "nope", "cancel", "stop", "don't", "dont", "negative", "n", "abort"}


def is_clear_answer(answer: str) -> bool:
    """True only for a real yes or no. Background talk ('what time is it', TV) is NOT an answer and must never count as 'no'."""
    a = _norm(answer)
    return bool(a) and any(re.search(rf"\b{re.escape(w)}\b", a) for w in (*YES_WORDS, *NO_WORDS))


def is_yes(answer: str) -> bool:
    a = _norm(answer)
    if not a or any(re.search(rf"\b{re.escape(n)}\b", a) for n in NO_WORDS):
        return False
    return any(re.search(rf"\b{re.escape(y)}\b", a) for y in YES_WORDS)


# --------------------------------------------------------------------------- microphone
MIN_TRIGGER = 0.02   # the lowest automatic "someone is talking" level
START_BLOCKS = 4     # ~120 ms above the trigger before it counts as speech: a click or a key press is shorter than that


def suggest_trigger(ambient: float, p90: float = 0.0) -> float:
    """Automatic trigger level from the room: 3x the average noise or 1.6x the louder 10% of it (noises that come and go,
    like typing), kept between MIN_TRIGGER and 0.08."""
    return round(min(0.08, max(MIN_TRIGGER, ambient * 3.0, p90 * 1.6)), 4)


class Recorder:
    def __init__(self, settings, on_level: Callable[[float], None] | None = None,
                 hold: Callable[[], bool] | None = None):
        """on_level(rms) is called for every audio block (dashboard meter).
        hold() -> True means "ignore the mic right now" (Friday is speaking, or the mic is muted)."""
        import numpy as np
        import sounddevice as sd

        self.np, self.sd = np, sd
        self.settings = settings
        self.on_level = on_level or (lambda rms: None)
        self.hold = hold or (lambda: False)
        self.device = self._resolve_device(settings.mic_device)
        self.threshold = settings.energy_threshold
        self.ambient = 0.0
        self.complete_check: Callable | None = None  # audio -> True when the sentence so far is finished (smart endpointing)

    def _resolve_device(self, spec: str):
        if not spec:
            return None
        if spec.isdigit():
            return int(spec)
        for i, d in enumerate(self.sd.query_devices()):
            if d["max_input_channels"] > 0 and spec.lower() in d["name"].lower():
                return i
        print(f"[mic] '{spec}' not found, using the default microphone.")
        return None

    def calibrate(self, seconds: float = 1.5) -> float:
        """Measure background noise; if ENERGY_THRESHOLD=0 pick a threshold automatically.
        The trigger is never below MIN_TRIGGER: a room that happened to be dead quiet for one second used to give a trigger
        of 0.004, and then every key press and chair creak started a recording."""
        audio = self.sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            device=self.device)
        self.sd.wait()
        np = self.np
        ambient = float(np.sqrt(np.mean(audio ** 2)))
        flat = np.asarray(audio, dtype="float32").reshape(-1)
        n = len(flat) // BLOCK
        loud = float(np.percentile(np.sqrt(np.mean(flat[:n * BLOCK].reshape(n, BLOCK) ** 2, axis=1)), 90)) if n else 0.0
        self.ambient, self.ambient_p90 = ambient, loud
        if self.settings.energy_threshold <= 0:
            self.threshold = suggest_trigger(ambient, loud)
        return ambient

    def _trigger(self) -> float:
        return self.settings.energy_threshold if self.settings.energy_threshold > 0 else self.threshold

    def listen(self, max_seconds: float | None = None, wait_seconds: float | None = None,
               should_abort: Callable[[], bool] = lambda: False):
        """Record one phrase. Returns float32 numpy audio, or None if nothing was said / aborted."""
        np = self.np
        max_seconds = max_seconds or self.settings.max_record_seconds
        silence_blocks = int(self.settings.silence_seconds * SAMPLE_RATE / BLOCK)
        early_blocks = min(silence_blocks - 1, int(0.5 * SAMPLE_RATE / BLOCK))
        checked = False
        pre_roll = deque(maxlen=int(0.5 * SAMPLE_RATE / BLOCK))
        frames, started, quiet, start_time, loud_run = [], False, 0, time.time(), 0
        with self.sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                                 device=self.device) as stream:
            while True:
                if should_abort():
                    return None
                block, _ = stream.read(BLOCK)
                block = block[:, 0].copy()
                rms = float(np.sqrt(np.mean(block ** 2)))
                self.on_level(rms)
                if self.hold():  # echo guard / muted: throw away anything heard meanwhile
                    pre_roll.clear()
                    frames, started, quiet, loud_run = [], False, 0, 0
                    start_time = time.time()
                    continue
                threshold = self._trigger()
                if not started:
                    pre_roll.append(block)
                    loud_run = loud_run + 1 if rms > threshold else 0
                    if loud_run >= START_BLOCKS:  # sustained, not a click
                        started, frames, speech_start = True, list(pre_roll), time.time()
                    elif wait_seconds and time.time() - start_time > wait_seconds:
                        return None
                    continue
                frames.append(block)
                quiet = quiet + 1 if rms < threshold * 0.8 else 0
                if quiet == 0:
                    checked = False
                if quiet >= silence_blocks or time.time() - speech_start > max_seconds:
                    break
                # smart endpointing: at a short natural pause, if what was said already reads as a finished sentence,
                # stop now instead of waiting out the full silence (saves ~0.5 s on every request)
                if getattr(self, "complete_check", None) and not checked and quiet >= early_blocks and len(frames) > SAMPLE_RATE * 0.8 / BLOCK:
                    checked = True
                    try:
                        if self.complete_check(np.concatenate(frames)):
                            break
                    except Exception:
                        pass
        audio = np.concatenate(frames)
        if len(audio) < SAMPLE_RATE * 0.3:  # shorter than 0.3 s: probably a click
            return None
        return audio


# --------------------------------------------------------------------------- speech-to-text
def _add_cuda_dll_dirs() -> None:
    """On Windows, pip's nvidia-cublas / nvidia-cudnn wheels put their DLLs in site-packages/nvidia/*/bin."""
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    import site
    from pathlib import Path

    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for b in Path(sp, "nvidia").glob("*/bin"):
            try:
                os.add_dll_directory(str(b))
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass


def whisper_devices(pref: str) -> list[tuple[str, str]]:
    """Order to try: WHISPER_DEVICE=auto -> GPU (float16) then CPU (int8); 'cuda' or 'cpu' to force one."""
    pref = (pref or "auto").lower()
    if pref == "cpu":
        return [("cpu", "int8")]
    if pref == "cuda":
        return [("cuda", "float16"), ("cuda", "int8_float16")]
    return [("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")]


_PROBE = r"""
import sys, numpy as np
from friday.voice import _add_cuda_dll_dirs
_add_cuda_dll_dirs()
from faster_whisper import WhisperModel
m = WhisperModel(sys.argv[1], device="cuda", compute_type=sys.argv[2])
list(m.transcribe(np.zeros(16000, dtype="float32"), language="en", beam_size=1)[0])
print("GPU-OK")
"""


def gpu_probe_ok(model: str, ctype: str, run=None) -> bool:
    """Try Whisper on the GPU in a SEPARATE process first. A missing NVIDIA DLL (cuBLAS/cuDNN) can kill the whole
    program without a Python error, so we never risk it inside Friday. The answer is remembered."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    from .env import data_dir

    try:
        import ctranslate2
        ver = ctranslate2.__version__
    except Exception:
        ver = "?"
    nvidia = 0
    try:
        import site
        nvidia = sum(len(list(Path(sp, "nvidia").glob("*/bin"))) for sp in site.getsitepackages())
    except Exception:
        pass
    key = f"{model}|{ctype}|{ver}|{nvidia}"
    cache = data_dir() / "whisper_gpu.json"
    try:
        known = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if key in known:
        return known[key]
    print(f"[stt] Testing the graphics card for speech recognition ({ctype}) - one time, takes a few seconds...")
    run = run or (lambda: subprocess.run([sys.executable, "-c", _PROBE, model, ctype], capture_output=True, text=True,
                                         timeout=180, cwd=str(Path(__file__).resolve().parent.parent)))
    try:
        r = run()
        ok = r.returncode == 0 and "GPU-OK" in (r.stdout or "")
        if not ok:
            print(f"[stt] Graphics card test failed: {((r.stderr or '') + (r.stdout or '')).strip()[-200:]}")
    except Exception as e:
        ok = False
        print(f"[stt] Graphics card test failed: {e}")
    known[key] = ok
    try:
        cache.write_text(json.dumps(known, indent=1), encoding="utf-8")
    except OSError:
        pass
    return ok


def load_whisper(model: str, pref: str, factory=None):
    """Returns (model, device). GPU candidates are first tested in a separate process (gpu_probe_ok), then each
    candidate is test-run on 1 s of silence so problems show up now, not in the middle of a conversation."""
    probe = factory is None
    if factory is None:
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel as factory
        try:  # download once up front (small.en is ~480 MB) so the GPU test doesn't time out on the download
            from faster_whisper.utils import download_model
            print(f"[stt] Making sure Whisper '{model}' is downloaded (first time ~0.5-1.5 GB, please wait)...")
            download_model(model)
        except Exception as e:
            print(f"[stt] Download check skipped ({str(e)[:100]})")
    import numpy as np

    last = None
    for device, ctype in whisper_devices(pref):
        if device == "cuda" and probe and not gpu_probe_ok(model, ctype):
            last = RuntimeError("GPU test failed")
            continue
        try:
            m = factory(model, device=device, compute_type=ctype)
            list(m.transcribe(np.zeros(SAMPLE_RATE, dtype="float32"), language="en", beam_size=1)[0])
            return m, f"{device}/{ctype}"
        except Exception as e:  # no GPU, missing cuBLAS/cuDNN, out of memory...
            last = e
            print(f"[stt] Whisper on {device} ({ctype}) not available: {str(e)[:120]}")
    raise RuntimeError(f"Whisper couldn't load: {last}")


_UNFINISHED_END = {"and", "but", "so", "or", "because", "then", "the", "a", "an", "to", "of", "my", "your", "with", "for",
                   "in", "on", "at", "um", "uh", "like", "if", "that", "is", "are", "was", "i", "also", "plus", "about"}


def sentence_looks_finished(text: str) -> bool:
    """True when a transcript reads like a complete request: 4+ words, ends with . ? or !, and not on a dangling word."""
    t = (text or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", t)
    if len(words) < 4 or t[-1:] not in ".?!" or t.endswith("..."):
        return False
    return words[-1].lower() not in _UNFINISHED_END


class SpeechToText:
    def __init__(self, settings):
        self.settings = settings
        self.engine = settings.stt_engine
        self._whisper = None
        self.device = ""
        if self.engine == "whisper":
            try:
                print(f"[stt] Loading Whisper model '{settings.whisper_model}' (first run downloads it)...")
                self._whisper, self.device = load_whisper(settings.whisper_model, os.environ.get("WHISPER_DEVICE", "auto"))
                print(f"[stt] Whisper ready on {self.device}.")
            except Exception as e:
                print(f"[stt] Whisper unavailable ({e}); falling back to the free Google recognizer.")
                self.engine = "google"

    def _hint(self) -> str:
        """Words Whisper tends to mishear ('Quo' became 'a clock'). Add your own with STT_VOCAB=word1, word2."""
        from .env import env

        vocab = ["Quo", "Jarvis", "Integrity Powersports", "Tailscale", "Ollama", "Abacus", "Netlify", "SMS",
                 "Yamaha", "Honda", "Kawasaki", "Suzuki", "Polaris", "Sea-Doo", "Ski-Doo"]
        vocab += [w.strip() for w in env("STT_VOCAB", "").split(",") if w.strip()]
        return f"{self.settings.assistant_name}, " + ", ".join(self.settings.wake_words) + ". " + ", ".join(vocab) + "."

    def transcribe(self, audio) -> str:
        if audio is None:
            return ""
        if self.engine == "whisper" and self._whisper is not None:
            hint = self._hint()
            segments, _ = self._whisper.transcribe(audio, language="en", beam_size=1, initial_prompt=hint,
                                                   condition_on_previous_text=False)
            return " ".join(s.text for s in segments).strip()
        import numpy as np
        import speech_recognition as sr

        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        try:
            return sr.Recognizer().recognize_google(sr.AudioData(pcm, SAMPLE_RATE, 2))
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            print(f"[stt] Google recognizer error: {e}")
            return ""

    def transcribe_file(self, path: str) -> str:
        """Transcribe an audio file (phone recordings: m4a/mp4/webm/ogg/wav). faster-whisper decodes it with PyAV."""
        if self.engine == "whisper" and self._whisper is not None:
            hint = self._hint()
            segments, _ = self._whisper.transcribe(str(path), language="en", beam_size=1, initial_prompt=hint,
                                                   condition_on_previous_text=False)
            return " ".join(s.text for s in segments).strip()
        from faster_whisper import decode_audio

        return self.transcribe(decode_audio(str(path), sampling_rate=SAMPLE_RATE))


# --------------------------------------------------------------------------- text-to-speech
class TextToSpeech:
    def __init__(self, settings):
        self.settings = settings
        self.muted = False          # user toggle (dashboard / tray / /mute)
        self.failed = False         # both engines failed -> text only
        self.speaking = False
        self.last_spoken = ""
        self.ended_at = 0.0
        self.last_error = ""
        self._lock = threading.Lock()
        self._cancel = threading.Event()  # set by stop(): skip/abort the current speech
        self.playing = False              # True only while audio is actually coming out of the speakers
        self.barge_provider = None        # set by the assistant: () -> {device, level, on_interrupt} or None
        self.fast_tail_until = 0.0        # set when you interrupt: listen again almost at once
        self._engine = None

    @property
    def engine(self) -> str:  # read live so dashboard changes apply immediately
        return self.settings.tts_engine

    def busy(self, tail: float = 0.6) -> bool:
        """True while speaking and for a short tail afterwards (room echo). After YOU interrupted, the tail is tiny, so
        the first words you say are not thrown away."""
        if time.time() < self.fast_tail_until:
            tail = min(tail, 0.12)
        return self.speaking or (time.time() - self.ended_at) < tail

    def speak(self, text: str) -> None:
        text = clean_for_speech(text)
        if self.muted or self.engine == "none" or not text:
            return
        with self._lock:
            self.speaking, self.last_spoken = True, text
            self._cancel.clear()
            self.playing = False
            done = threading.Event()
            self._start_barge(done)  # lets you talk over it to interrupt
            try:
                if self.engine == "edge":
                    try:
                        self._speak_edge(text)
                        self.last_error = ""
                        return
                    except Exception as e:
                        self.last_error = f"Edge voice failed ({type(e).__name__}: {e}); used offline voice."
                        print(f"[tts] {self.last_error}")
                if self.failed:
                    return
                try:
                    self._speak_pyttsx3(text)
                except Exception as e:
                    self.last_error = f"Offline voice failed ({e}). Replies are text only."
                    print(f"[tts] {self.last_error}")
                    self.failed = True
            finally:
                done.set()
                self.playing = False
                self.speaking, self.ended_at = False, time.time()

    def start_stream(self) -> "StreamSpeaker | None":
        """A speaker that starts talking as soon as the first sentence of a reply exists. None if the voice is off."""
        if self.muted or self.engine == "none":
            return None
        return StreamSpeaker(self)

    # ------------------------------------------------------------------ talk-over interruption (barge-in)
    def _start_barge(self, done: threading.Event) -> None:
        cfg = None
        try:
            cfg = self.barge_provider() if self.barge_provider else None
        except Exception:
            cfg = None
        if cfg:
            threading.Thread(target=self._barge_monitor, args=(cfg, done), daemon=True, name="barge-in").start()

    def _barge_monitor(self, cfg: dict, done: threading.Event) -> None:
        """While Jarvis talks, listen on the mic; if you speak up, stop the speech (see BargeDetector)."""
        try:
            import numpy as np
            import sounddevice as sd

            det = None
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                                device=cfg.get("device")) as st:
                while not done.is_set():
                    block, _ = st.read(BLOCK)
                    if not self.playing:  # synthesising the voice: nothing to interrupt yet
                        det = None
                        continue
                    det = det or BargeDetector(cfg.get("level", 0.06))
                    if det.feed(float(np.sqrt(np.mean(block[:, 0] ** 2)))):
                        cfg["on_interrupt"]()
                        return
        except Exception as e:
            print(f"[barge-in] unavailable: {type(e).__name__}: {e}")

    def render(self, text: str) -> tuple[bytes, str]:
        """Make the reply audio as a file (for the phone page) instead of playing it on the PC speakers.
        Ignores 'muted' (that's for the PC speakers). Returns (b"", "") if there's no voice or it fails."""
        text = clean_for_speech(text)
        engine = self.engine
        if engine == "none" or not text:
            return b"", ""
        with self._lock:
            if engine == "edge":
                data = self._render_file(".mp3", lambda p: asyncio.run(edge_tts_save(text, self.settings, p)))
                if data:
                    return data, "audio/mpeg"
            data = self._render_file(".wav", lambda p: self._pyttsx3_save(text, p))
            return (data, "audio/wav") if data else (b"", "")

    @staticmethod
    def _render_file(suffix: str, make) -> bytes:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            make(path)
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            print(f"[tts] Could not make {suffix} audio for the phone: {type(e).__name__}: {e}")
            return b""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _pyttsx3_save(self, text: str, path: str) -> None:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", self.settings.pyttsx3_rate)
        for v in engine.getProperty("voices") or []:
            if "david" in v.name.lower() or "george" in v.name.lower():
                engine.setProperty("voice", v.id)
                break
        engine.save_to_file(text, path)
        engine.runAndWait()
        engine.stop()

    def stop(self) -> None:
        """Interrupt speech (dashboard 'Stop speaking' button / stop hotkey)."""
        self._cancel.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass
        try:
            if self._engine is not None:
                self._engine.stop()  # offline voice (pyttsx3)
        except Exception:
            pass

    def _speak_edge(self, text: str) -> None:
        import sounddevice as sd
        from concurrent.futures import ThreadPoolExecutor

        # Speak sentence by sentence: the next sentence is being made while the current one plays,
        # so the first words come out much sooner on long replies.
        chunks = split_sentences(text)
        with ThreadPoolExecutor(max_workers=1) as pool:
            nxt = pool.submit(self._edge_samples, chunks[0])
            for i in range(len(chunks)):
                samples = nxt.result()
                if i + 1 < len(chunks):
                    nxt = pool.submit(self._edge_samples, chunks[i + 1])
                if self._cancel.is_set():  # interrupted while the voice was still being made
                    return
                self.playing = True
                sd.play(samples, 24000)
                sd.wait()
                if self._cancel.is_set():
                    return

    def _edge_samples(self, text: str):
        import edge_tts
        import miniaudio
        import numpy as np

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            async def _save():
                await edge_tts.Communicate(text, self.settings.edge_voice, rate=self.settings.edge_rate).save(path)

            asyncio.run(_save())
            decoded = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
                                            sample_rate=24000)
            return np.frombuffer(decoded.samples, dtype=np.int16)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _speak_pyttsx3(self, text: str) -> None:
        import pyttsx3

        engine = pyttsx3.init()  # new engine per call: reliable across threads on Windows (SAPI5)
        engine.setProperty("rate", self.settings.pyttsx3_rate)
        voices = engine.getProperty("voices") or []
        for v in voices:  # prefer a male English voice (e.g. "Microsoft David") for the Jarvis feel
            if "david" in v.name.lower() or "george" in v.name.lower():
                engine.setProperty("voice", v.id)
                break
        if self._cancel.is_set():
            return
        engine.say(text)
        self._engine, self.playing = engine, True
        try:
            engine.runAndWait()
        finally:
            self._engine = None
            engine.stop()


class BargeDetector:
    """Decides when you are talking over Jarvis. It is fed one microphone block level (30 ms) at a time while Jarvis speaks.
      1. The first ~0.6 s of its own voice only teaches it how loud that is in the microphone (the echo).
      2. After that you count as talking when at least `need` of the last `window` blocks are louder than
         max(level, factor x echo). Speech has gaps between words, so a run of loud blocks in a row (the old rule) almost
         never happened: you could not interrupt. A click or a cough is too short to fill the window."""

    def __init__(self, level: float = 0.06, factor: float = 1.8, learn_blocks: int = 20, window: int = 12, need: int = 7):
        self.level, self.factor, self.learn_blocks, self.need = level, factor, learn_blocks, need
        self.echo: list[float] = []
        self.recent: deque = deque(maxlen=window)

    def feed(self, rms: float) -> bool:
        if len(self.echo) < self.learn_blocks:
            self.echo.append(rms)
            return False
        echo = sorted(self.echo)[int(0.9 * (len(self.echo) - 1))]
        self.recent.append(rms > max(self.level, self.factor * echo))
        return sum(self.recent) >= self.need


class StreamSpeaker:
    """Speaks sentences as they arrive (feed), several being voiced ahead while one plays; finish() waits until
    everything has been said. Pressing stop (tts.stop) drops whatever is left."""

    def __init__(self, tts: "TextToSpeech"):
        self.tts = tts
        self.started = False
        self._items: queue.Queue = queue.Queue()
        self._pool = None
        self._thread: threading.Thread | None = None

    def feed(self, text: str) -> None:
        from concurrent.futures import Future, ThreadPoolExecutor

        text = clean_for_speech(text)
        if not text or (self.started and self.tts._cancel.is_set()):
            return
        if not self.started:
            self.started = True
            self.tts.last_spoken = ""
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts-synth")
            self._thread = threading.Thread(target=self._run, daemon=True, name="tts-stream")
            self._thread.start()
        self.tts.last_spoken = (self.tts.last_spoken + " " + text).strip()
        if self.tts.engine == "edge":
            fut = self._pool.submit(self._synth, text)
        else:
            fut = Future()
            fut.set_result(None)  # offline voice: it synthesises while speaking
        self._items.put((text, fut))

    def _synth(self, text: str):
        try:
            return self.tts._edge_samples(text)
        except Exception as e:
            self.tts.last_error = f"Edge voice failed ({type(e).__name__}: {e}); used offline voice."
            print(f"[tts] {self.tts.last_error}")
            return None

    def _run(self) -> None:
        import sounddevice as sd

        tts = self.tts
        with tts._lock:
            tts.speaking, tts.playing = True, False
            tts._cancel.clear()
            done = threading.Event()
            tts._start_barge(done)
            try:
                while True:
                    item = self._items.get()
                    if item is None:
                        break
                    text, fut = item
                    if tts._cancel.is_set():
                        continue
                    samples = fut.result()
                    if tts._cancel.is_set():
                        continue
                    if samples is not None:
                        tts.playing = True
                        sd.play(samples, 24000)
                        sd.wait()
                    elif not tts.failed:
                        try:
                            tts._speak_pyttsx3(text)
                        except Exception as e:
                            tts.last_error = f"Offline voice failed ({e}). Replies are text only."
                            tts.failed = True
            finally:
                done.set()
                tts.playing = False
                tts.speaking, tts.ended_at = False, time.time()

    def finish(self) -> None:
        """Wait until everything fed so far has been spoken."""
        if not self.started:
            return
        self._items.put(None)
        if self._thread is not None:
            self._thread.join(timeout=300)
        if self._pool is not None:
            self._pool.shutdown(wait=False)


def split_sentences(text: str, max_chunks: int = 6) -> list[str]:
    """Break a reply into speakable pieces (sentences). Short pieces are joined so the voice doesn't sound choppy,
    and there are never more than max_chunks (the rest is merged into the last piece)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < 40:
            out[-1] += " " + p
        else:
            out.append(p)
    if len(out) > max_chunks:
        out = out[:max_chunks - 1] + [" ".join(out[max_chunks - 1:])]
    return out or [text]


async def edge_tts_save(text: str, settings, path: str) -> None:
    import edge_tts

    await edge_tts.Communicate(text, settings.edge_voice, rate=settings.edge_rate).save(path)


def list_microphones() -> str:
    import sounddevice as sd

    lines = []
    default_in = sd.default.device[0] if sd.default.device else None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            mark = "  <- default" if i == default_in else ""
            lines.append(f"  [{i}] {d['name']}{mark}")
    return "\n".join(lines) or "  (no microphones found)"


def list_microphone_devices() -> list[dict]:
    """Structured list for the dashboard dropdown."""
    import sounddevice as sd

    default_in = sd.default.device[0] if sd.default.device else None
    return [{"index": i, "name": d["name"], "default": i == default_in}
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
