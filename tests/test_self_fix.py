"""Listening fixes (cut-offs, noise, talking over Jarvis) and Jarvis fixing himself: settings he may change, code he may read."""
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from friday import config, selfmod, selfsettings, toolpick, voice
from friday import tools as T
from friday.voice import BLOCK, BargeDetector, Recorder, suggest_trigger


# ------------------------------------------------------------------ talking over Jarvis
def _run(det, levels):
    for n, rms in enumerate(levels):
        if det.feed(rms):
            return n
    return None


def test_you_can_interrupt_with_normal_speech_that_has_gaps_between_words():
    echo = [0.03] * 20                                                    # its own voice in the microphone
    speech = ([0.15, 0.12, 0.02, 0.14, 0.16, 0.03, 0.13, 0.15, 0.11, 0.02, 0.14, 0.12] * 3)
    assert _run(BargeDetector(), echo + speech) is not None               # the old "6 loud blocks in a row" rule never fired
    assert _run(BargeDetector(), echo + speech) <= 20 + 12


def test_its_own_voice_a_click_or_a_cough_do_not_interrupt_it():
    echo = [0.05] * 20
    assert _run(BargeDetector(), echo + [0.05] * 300) is None             # steady echo, however long
    assert _run(BargeDetector(), echo + [0.01] * 10 + [0.5] * 4 + [0.01] * 50) is None   # a 0.12 s bang
    assert _run(BargeDetector(level=0.06), [0.0] * 20 + [0.045] * 100) is None            # too quiet to count as talking


def test_the_louder_its_echo_the_louder_you_must_be():
    quiet_room = [0.01] * 20 + [0.08] * 12
    echoey = [0.06] * 20 + [0.08] * 12                                    # speakers close to the mic: 0.08 is barely above it
    assert _run(BargeDetector(), quiet_room) is not None
    assert _run(BargeDetector(), echoey) is None


def test_after_you_interrupt_it_listens_almost_at_once(monkeypatch):
    t = voice.TextToSpeech(NS(tts_engine="none", edge_voice="x", edge_rate="+0%", pyttsx3_rate=180))
    t.ended_at = voice.time.time()
    assert t.busy()                                                       # normal: a 0.6 s tail after it stops
    t.fast_tail_until = voice.time.time() + 3
    t.ended_at = voice.time.time() - 0.2
    assert not t.busy()                                                   # after YOU cut in: the first words are kept


# ------------------------------------------------------------------ noise and cut-offs
def _recorder(blocks, threshold=0.05, silence=0.3):
    state = {"i": 0}

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            b = blocks[min(state["i"], len(blocks) - 1)]
            state["i"] += 1
            return b, False

    rec = Recorder.__new__(Recorder)
    rec.np, rec.sd = np, NS(InputStream=lambda **kw: Stream(), query_devices=lambda: [])
    rec.settings = NS(energy_threshold=threshold, silence_seconds=silence, max_record_seconds=10)
    rec.threshold, rec.device, rec.on_level, rec.hold = threshold, None, lambda r: None, lambda: False
    return rec


def _block(level):
    return np.full((BLOCK, 1), level, dtype="float32")


def test_a_click_or_key_press_does_not_start_a_recording_but_speech_does():
    click = [_block(0.3)] * 3 + [_block(0.0)] * 400
    assert _recorder(click).listen(wait_seconds=0.3) is None
    speech = [_block(0.2)] * 12 + [_block(0.0)] * 400
    audio = _recorder(speech).listen(wait_seconds=2)
    assert audio is not None and len(audio) >= 12 * BLOCK


def test_the_automatic_trigger_is_never_set_by_one_dead_quiet_second():
    assert suggest_trigger(0.0002, 0.0) == voice.MIN_TRIGGER == 0.02      # it used to be 0.004: every noise started a recording
    assert suggest_trigger(0.0134, 0.03) == 0.048                          # typing now and then raises it
    assert suggest_trigger(0.2, 0.3) == 0.08
    noisy = np.zeros((int(1.5 * voice.SAMPLE_RATE), 1), dtype="float32")
    noisy[::5] = 0.03                                                      # bursts of noise
    rec = Recorder.__new__(Recorder)
    rec.np, rec.sd = np, NS(rec=lambda *a, **k: noisy, wait=lambda: None)
    rec.settings, rec.device = NS(energy_threshold=0.0), None
    rec.calibrate()
    assert rec.threshold >= 0.02 and rec.ambient_p90 > 0


# ------------------------------------------------------------------ Jarvis changes his own settings
ENV = "SILENCE_SECONDS=1.0\nSMART_ENDPOINT=true\nAUTONOMY_LEVEL=1\nCREDIT_DAILY_CAP=1000\nABACUS_API_KEY=s2_realkey_1234567890abcd\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    p = tmp_path / ".env"
    p.write_text(ENV, encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", p)
    return p


def test_jarvis_changes_a_listening_setting_applies_it_and_can_put_it_back(env):
    import friday.main  # noqa: F401

    applied = []
    ctx = T.ToolContext(apply_settings=lambda kinds: applied.append(kinds) or {"notes": ["Restarting the microphone."]})
    out = T.dispatch("change_setting", {"key": "silence_seconds", "value": "1.6"}, ctx)
    assert "from 1.0 to 1.6" in out and "in effect now" in out and "undo that setting" in out
    assert "SILENCE_SECONDS=1.6" in env.read_text() and applied == [["live"]]
    assert "already 1.6" in T.dispatch("change_setting", {"key": "SILENCE_SECONDS", "value": "1.6"}, ctx)
    out = T.dispatch("undo_setting_change", {}, ctx)
    assert "back to 1.0" in out and "SILENCE_SECONDS=1.0" in env.read_text()
    assert "nothing to undo" in T.dispatch("undo_setting_change", {}, ctx)


def test_jarvis_can_never_change_permissions_money_providers_or_keys(env):
    import friday.main  # noqa: F401

    ctx = T.ToolContext()
    before = env.read_text()
    for key, value in (("AUTONOMY_LEVEL", "2"), ("CREDIT_DAILY_CAP", "999999"), ("ABACUS_API_KEY", "x" * 24), ("LLM_PROVIDER", "openai"),
                       ("ALLOW_SHELL", "true"), ("NEVER_TOOLS", ""), ("CUSTOM_BASE_URL", "http://evil.example/v1"),
                       ("FRIDAY_API_KEY", "y" * 24), ("DISTILL_ENABLED", "false"), ("NOT_A_SETTING", "1")):
        out = T.dispatch("change_setting", {"key": key, "value": value}, ctx)
        assert "can't change" in out, (key, out)
    assert env.read_text() == before
    assert selfsettings.why_not("SILENCE_SECONDS") == "" and selfsettings.why_not("BARGE_IN_LEVEL") == ""


def test_bad_values_are_refused_and_the_lookup_never_shows_a_secret(env):
    import friday.main  # noqa: F401

    ctx = T.ToolContext()
    assert T.dispatch("change_setting", {"key": "SILENCE_SECONDS", "value": "abc"}, ctx).startswith("Not changed")
    assert "SILENCE_SECONDS=1.0" in env.read_text()
    out = T.dispatch("list_settings", {"search": "silence"}, ctx)
    assert "SILENCE_SECONDS = 1.0" in out and "[I can change it]" in out
    out = T.dispatch("list_settings", {"search": "abacus key"}, ctx)
    assert "(secret)" in out and "s2_realkey" not in out and "[owner only]" in out


def test_the_settings_tools_are_offered_and_the_prompt_says_he_may_fix_himself():
    import friday.main  # noqa: F401
    from friday.prompt import build_system_prompt

    for text in ("it keeps cutting me off, let me finish talking", "the microphone is too sensitive to background noise",
                 "I can't interrupt you", "undo that setting"):
        offered = toolpick.pick(text)
        assert "change_setting" in offered and "list_settings" in offered, text
    assert "settings" in toolpick.groups_for("fix it yourself")
    s = config.load_settings()
    assert "FIXING YOURSELF" in build_system_prompt(s, None, False)


# ------------------------------------------------------------------ Jarvis changes his own code
def test_the_files_that_guard_safety_stay_protected_but_voice_code_is_open():
    for rel in ("friday/approvals.py", "friday/guards.py", "friday/blackbox.py", "friday/distill.py", "friday/phone.py",
                "friday/selfsettings.py", "friday/autonomy.py", "friday/credits.py", "friday/selfmod.py"):
        assert selfmod.check_path(rel), rel
    for rel in ("friday/voice.py", "friday/listening.py", "friday/toolpick.py", "friday/static/phone.html"):
        assert selfmod.check_path(rel) == "", rel


def test_the_drafting_ai_can_ask_to_read_the_files_it_needs():
    seen = []
    proposal = {"explanation": "Wait a little longer.", "edits": [{"path": "friday/voice.py", "old": "x", "new": "y"}],
                "new_files": [], "new_tests": []}

    def ask(system, user, **k):
        seen.append(user)
        return (json.dumps({"need_files": ["friday/voice.py", ".env"]}) if len(seen) == 1 else json.dumps(proposal)), "test-ai"

    prop = selfmod.draft(NS(ask_ai=ask), "make the microphone wait longer before it decides I stopped talking")
    assert len(seen) == 2 and prop["edits"] and prop["who"] == "test-ai"
    assert "friday/voice.py: " in seen[0] and "friday/approvals.py:" in seen[0] and "protected" in seen[0]   # the index of every file
    assert "def listen(" in seen[1] and "Files you asked to read" in seen[1]                               # the file it asked for
    assert "not a file I can show" in seen[1]                                                              # .env is never shown
    assert "change_setting" in selfmod.SYSTEM and "need_files" in selfmod.SYSTEM
