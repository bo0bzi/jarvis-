"""Tests for the reliability upgrades: fake-success guard, short answers, placeholder texts, instant time, memory."""
from pathlib import Path

from friday import llm, quick, sms, warmup
from friday.memory import Memory


def test_fake_completion_claims_are_caught():
    # claiming a text was sent when no tool ran
    assert llm.is_fake_promise("text me the weather", "The weather update has been sent to your phone.")
    assert llm.is_fake_promise("set it up", "The Quo skill has been configured successfully.")
    # inventing a Twilio requirement
    assert llm.is_fake_promise("send me a test text", "I need your Twilio account SID to proceed.")
    # a plain question about the past is allowed
    assert not llm.is_fake_promise("did you send it?", "Yes, it was sent earlier.")
    assert not llm.is_fake_promise("what is a motorcycle", "A motorcycle is a two-wheeled vehicle.")


def test_bare_yes_gets_context():
    hist = [{"role": "user", "content": "text me the weather"},
            {"role": "assistant", "content": "Shall I send the forecast to your phone?"}]
    out = llm.resolve_short_reply("yes", hist)
    assert "Shall I send the forecast" in out and "calling the right tool" in out
    assert llm.resolve_short_reply("what is the weather", hist) == "what is the weather"
    assert llm.resolve_short_reply("yes", []) == "yes"


def test_placeholder_texts_are_blocked():
    assert sms.placeholder_in("Forecast: [forecast details will be added here]")
    assert sms.placeholder_in("Hi {{name}}")
    assert not sms.placeholder_in("Tomorrow: overcast, high 15C, low 5C.")


def test_instant_time_question_and_quo_skill_not_hijacked():
    assert quick._TIME_Q.match("what time is it")
    assert quick._TIME_Q.match("jarvis what's the date")
    assert not quick._TIME_Q.match("what time does the shop close")
    assert quick._TEXT_INTENT.search("i need to be able to send a text through quo")
    assert not quick._TEXT_INTENT.search("please find a quo skill")


def test_memory_hides_poisoned_replies(tmp_path: Path):
    m = Memory(tmp_path)
    m.add("user", "send me a test")
    m.add("assistant", "I need your Twilio account SID and authentication token.")
    m.add("user", "what is 2+2")
    m.add("assistant", "4")
    assert m.history() == [{"role": "user", "content": "what is 2+2"}, {"role": "assistant", "content": "4"}]


def test_warmup_skips_cloud_and_when_off(monkeypatch):
    class S:
        provider, base_url, model = "abacus", "https://routellm.abacus.ai/v1", "route-llm"

    assert warmup.warm_up(S, log=lambda m: None) is False
    monkeypatch.setenv("WARMUP", "false")

    class L:
        provider, base_url, model = "custom", "http://localhost:11434/v1", "qwen2.5:14b"

    assert warmup.warm_up(L, log=lambda m: None) is False
