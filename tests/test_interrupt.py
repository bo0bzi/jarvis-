"""Interrupting Jarvis while it is speaking.

The main loop blocks inside TextToSpeech.speak(), so typed text used to wait for the whole answer. These tests pin the
watcher that stops the voice and hands the text back, and the rule that any new request cuts the current answer."""
from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

from friday.approvals import PTT_SIGNAL
from friday.listening import ListeningMixin
from friday.main import Assistant


class FakeTTS:
    def __init__(self, busy=True):
        self._busy = busy
        self.stops = 0

    def busy(self, tail: float = 0.6) -> bool:
        return self._busy

    def stop(self) -> None:
        self.stops += 1
        self._busy = False

    def start_stream(self):
        return None


class FakeBus:
    def __init__(self, pending=None):
        self.pending_confirm = pending
        self.events: list[tuple] = []

    def log_event(self, kind, text, level="info"):
        self.events.append((kind, text))

    def trace(self, kind, text, detail=""):
        self.events.append((kind, text))


def _watcher(tts, inputs, bus, running=True):
    a = SimpleNamespace(tts=tts, inputs=inputs, bus=bus, running=running)
    return a, threading.Thread(target=ListeningMixin._interrupt_watch, args=(a,), daemon=True)


def test_typing_while_speaking_stops_the_voice_and_requeues_the_text():
    tts, inputs, bus = FakeTTS(busy=True), queue.Queue(), FakeBus()
    a, th = _watcher(tts, inputs, bus)
    inputs.put("actually, what's the weather")
    th.start()
    try:
        deadline = time.time() + 3
        while tts.stops == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert tts.stops == 1, "the voice should have been stopped"
        assert inputs.qsize() == 1, "the text must go back for the main loop to answer"
        assert inputs.get_nowait() == "actually, what's the weather"
        assert any(k == "interrupt" for k, _ in bus.events)
    finally:
        a.running = False
        th.join(timeout=2)


def test_text_is_left_alone_when_nothing_is_being_said():
    tts, inputs, bus = FakeTTS(busy=False), queue.Queue(), FakeBus()
    a, th = _watcher(tts, inputs, bus)
    inputs.put("hello")
    th.start()
    try:
        time.sleep(0.3)
        assert tts.stops == 0
        assert inputs.qsize() == 1, "a queued message must not be swallowed while idle"
    finally:
        a.running = False
        th.join(timeout=2)


def test_the_watcher_stays_out_of_the_way_during_an_approval():
    tts, inputs, bus = FakeTTS(busy=True), queue.Queue(), FakeBus(pending={"id": "abc", "prompt": "ok?"})
    a, th = _watcher(tts, inputs, bus)
    inputs.put("yes")
    th.start()
    try:
        time.sleep(0.3)
        assert tts.stops == 0, "confirm() owns the input queue while an approval is pending"
        assert inputs.qsize() == 1
    finally:
        a.running = False
        th.join(timeout=2)


def test_push_to_talk_signal_is_dropped_not_answered():
    tts, inputs, bus = FakeTTS(busy=True), queue.Queue(), FakeBus()
    a, th = _watcher(tts, inputs, bus)
    inputs.put(PTT_SIGNAL)
    th.start()
    try:
        time.sleep(0.3)
        assert tts.stops == 0
        assert inputs.qsize() == 0, "the hotkey signal is not text and must not be re-queued"
    finally:
        a.running = False
        th.join(timeout=2)


def _handle_fake(tts):
    said: list[str] = []
    brain = SimpleNamespace(stream_to=lambda fn: None, chat=lambda t: "the answer", last_error="",
                            last_route="main", streamed_prefix="")
    return SimpleNamespace(
        tts=tts, inputs=queue.Queue(), said=said, brain=brain,
        ui=SimpleNamespace(user=lambda *a, **k: None, set_status=lambda *a, **k: None,
                           assistant=lambda t: said.append(t), info=lambda *a, **k: None),
        bus=SimpleNamespace(agent=lambda *a, **k: None, set_flag=lambda *a, **k: None),
        s=SimpleNamespace(provider="custom", model="m", followup_seconds=0, backup_provider="none",
                          backup_max_per_day=0),
        mode="text", _brain_lock=threading.RLock(),
        say=lambda text: said.append(text),
    )


def test_a_new_request_cuts_the_current_answer_short():
    tts = FakeTTS(busy=True)
    a = _handle_fake(tts)
    Assistant.handle(a, "what time is it")
    assert tts.stops == 1, "a new message must stop whatever is being said"
    assert a.said == ["the answer"]


def test_handle_does_not_stop_speech_when_nothing_is_playing():
    tts = FakeTTS(busy=False)
    a = _handle_fake(tts)
    Assistant.handle(a, "what time is it")
    assert tts.stops == 0
