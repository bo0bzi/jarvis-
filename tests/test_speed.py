"""Speed work: a system prompt that never changes between turns (cache), clock on the message, one-call web answers,
instant small talk."""
import re
from types import SimpleNamespace

from friday import llm, quick
from friday import tools as T
from friday.config import load_settings
from friday.llm import Brain, build_system_prompt
from friday.memory import Memory
from test_friday import FakeOpenAI, _resp, clean_env, write_env  # noqa: F401


def _brain(clean_env, responses):
    s = load_settings(write_env(clean_env, "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen3:30b\nTOOL_MODE=native\n"))
    ctx = T.ToolContext(settings=s, memory=Memory(s.data_dir), speak=lambda t: None, confirm=lambda p: True)
    fake = FakeOpenAI(responses)
    return Brain(s, ctx.memory, ctx, client=fake), fake


def test_system_prompt_is_identical_between_turns(clean_env, monkeypatch):
    s = load_settings(write_env(clean_env, "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen3:30b\n"))
    mem = Memory(s.data_dir)
    a = build_system_prompt(s, mem, False, False, user_text="what is two plus two")
    # a minute later: a stub clock must not change a single character of the system prompt
    import datetime as real
    class Later(real.datetime):
        @classmethod
        def now(cls, tz=None): return real.datetime(2030, 1, 1, 12, 0)
    monkeypatch.setattr(llm, "datetime", Later)
    b = build_system_prompt(s, mem, False, False, user_text="what is two plus two")
    assert a == b and "Current local time" not in a and "[clock:" in a


def test_clock_rides_on_the_user_message_but_not_the_picker(clean_env):
    b, fake = _brain(clean_env, [_resp("Four.")])
    assert b.chat("what is two plus two") == "Four."
    last = fake.calls[0]["messages"][-1]["content"]
    assert last.startswith("what is two plus two") and re.search(r"\n\[clock: [^\]]+\]$", last)
    assert llm._plain(last) == "what is two plus two"
    assert [m["content"] for m in b.memory.history()][0] == "what is two plus two"  # memory stays clean


def test_instant_small_talk_and_switch(clean_env, monkeypatch):
    brain = SimpleNamespace(ctx=None, on_tool=lambda *a: None, on_trace=lambda *a: None)
    monkeypatch.setenv("INSTANT_SMALL_TALK", "on")
    assert quick.handle(brain, "hi") == "Yes, sir?" and quick.handle(brain, "Thanks!") == "Anytime, sir."
    assert quick.handle(brain, "hi, can you open notepad") is None  # real requests still go to the AI
    monkeypatch.setenv("INSTANT_SMALL_TALK", "off")
    assert quick.handle(brain, "hi") is None


def test_curly_apostrophe_promise_is_caught_and_web_retry_asks_for_the_answer(clean_env, monkeypatch):
    assert llm.is_fake_promise("go to google.com and check for long weekends", "Navigating now. I’ll search for it.")
    from friday import webcontext
    monkeypatch.setattr(webcontext, "gather", lambda brain, text: "LIVE WEB DATA: no stat holidays in November.")
    b, fake = _brain(clean_env, [_resp("Navigating to Google now. I’ll search for long weekends."),
                                 _resp("None: November has no statutory holiday in Ontario.")])
    assert b.chat("go to google.com and check if there is a long weekend in november") == \
        "None: November has no statutory holiday in Ontario."
    retry = fake.calls[1]["messages"][-1]["content"]
    assert "already have the live web data" in retry and "tools" not in fake.calls[1]


def test_weather_goes_straight_to_the_tool():
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: None, on_trace=lambda *a: None)
    orig = T.REGISTRY["get_weather"].func
    T.REGISTRY["get_weather"].func = lambda ctx, city="": calls.append(city) or f"Weather in {city or 'Toronto'}: 5C"
    try:
        assert quick.handle(brain, "what's the weather in Toronto right now") == "Weather in Toronto: 5C"
        assert quick.handle(brain, "weather") == "Weather in Toronto: 5C"
        assert quick.handle(brain, "Jarvis, how's the weather in St. Catharines today?") == "Weather in St. Catharines: 5C"
        assert calls == ["Toronto", "", "St. Catharines"]
        assert quick.handle(brain, "what will the weather be tomorrow") is None   # needs the AI
        assert quick.handle(brain, "is the weather good for riding") is None
    finally:
        T.REGISTRY["get_weather"].func = orig


def test_search_only_borrows_the_previous_topic_when_truly_vague():
    from friday import webcontext as W
    prev = "what is twenty percent of 350"
    q = W._query("go to google.com, check if there is any long weekends in ontario canada in november", prev)
    assert "twenty" not in q and "long weekends" in q and "ontario" in q.lower()
    assert "phonecalls" in W._query("lookup how to do that and give me a gameplan", "set you up to receive phonecalls")


def test_web_answer_is_one_call_without_tools(clean_env, monkeypatch):
    from friday import webcontext
    monkeypatch.setattr(webcontext, "gather", lambda brain, text: "LIVE WEB DATA: no stat holidays in November.")
    b, fake = _brain(clean_env, [_resp("None in November.")])
    assert b.chat("is there a long weekend in ontario in november") == "None in November."
    assert len(fake.calls) == 1 and "tools" not in fake.calls[0]  # one call, no tool menu
    # but a request that DOES something keeps its tools
    b2, fake2 = _brain(clean_env, [_resp("Opening it.")])
    b2.chat("search the web and open the first result")
    assert "tools" in fake2.calls[0]
