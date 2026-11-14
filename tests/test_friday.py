"""Unit tests that run without a microphone, speakers, Windows or network access."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import tools as T  # noqa: E402
from friday.config import is_placeholder, load_settings  # noqa: E402
from friday.llm import Brain, build_system_prompt, parse_json_tool_call  # noqa: E402
from friday.memory import Memory  # noqa: E402
from friday.voice import clean_for_speech, find_wake_word, is_yes  # noqa: E402

ENV_KEYS = ["LLM_PROVIDER", "ABACUS_API_KEY", "ABACUS_BASE_URL", "ABACUS_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "WAKE_WORDS", "INPUT_MODE", "TOOL_MODE", "DATA_DIR",
            "ENERGY_THRESHOLD", "TRAY_ICON", "USER_NAME", "CUSTOM_BASE_URL", "CUSTOM_API_KEY", "CUSTOM_MODEL",
            "BACKUP_PROVIDER", "BACKUP_MAX_PER_DAY", "ESCALATE_ON_ERROR", "SELF_ESCALATE", "ESCALATE_PHRASES",
            "FREE_ROUTER_KEY", "FREE_ROUTER_URL", "FREE_ROUTER_MODEL", "FREE_ROUTER_MAX_PER_DAY"]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def write_env(path: Path, text: str) -> Path:
    p = path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------------ config
def test_config_defaults_to_abacus(clean_env):
    s = load_settings(write_env(clean_env, "ABACUS_API_KEY=abc123realkey\n"))
    assert s.provider == "abacus"
    assert s.base_url == "https://routellm.abacus.ai/v1"
    assert s.model == "route-llm"
    assert s.api_key == "abc123realkey"
    assert s.validate() == []
    assert (clean_env / "data").is_dir()


def test_config_placeholder_key_is_rejected(clean_env):
    s = load_settings(write_env(clean_env, "ABACUS_API_KEY=your-abacus-routellm-key-here\n"))
    assert any("API key" in p for p in s.validate())
    assert is_placeholder("your-key-here") and not is_placeholder("s2_abcdef")


def test_config_anthropic_and_wake_words(clean_env):
    s = load_settings(write_env(clean_env, (
        "LLM_PROVIDER=anthropic\nANTHROPIC_API_KEY=sk-ant-test\nANTHROPIC_MODEL=claude-x\n"
        "WAKE_WORDS= Hey Friday , JARVIS \nINPUT_MODE=text\nENERGY_THRESHOLD=abc\n")))
    assert (s.provider, s.api_key, s.model) == ("anthropic", "sk-ant-test", "claude-x")
    assert s.wake_words == ["hey friday", "jarvis"]
    assert s.input_mode == "text"
    assert s.energy_threshold == 0.0  # invalid number falls back to default


def test_env_example_parses(clean_env):
    example = Path(__file__).resolve().parent.parent / ".env.example"
    s = load_settings(example)
    assert s.provider == "abacus" and s.validate()  # placeholder key -> must complain


# ------------------------------------------------------------------ tools
@pytest.fixture
def ctx(clean_env):
    s = load_settings(write_env(clean_env, "ABACUS_API_KEY=k\nALLOW_SHELL=true\n"))
    mem = Memory(s.data_dir)
    return T.ToolContext(settings=s, memory=mem, speak=lambda t: None, confirm=lambda p: False)


def test_all_tool_schemas_valid():
    specs = T.openai_tool_specs()
    assert len(specs) == len(T.REGISTRY) >= 20
    for spec in specs:
        fn = spec["function"]
        assert "strict" not in fn
        assert fn["parameters"]["type"] == "object"
        for req in fn["parameters"].get("required", []):
            assert req in fn["parameters"]["properties"]
    json.dumps(specs)
    json.dumps(T.anthropic_tool_specs())


def test_dispatch_basic_and_errors(ctx):
    assert "It is" in T.dispatch("get_time_date", {}, ctx)
    assert "unknown tool" in T.dispatch("nope", {}, ctx)
    assert "not valid JSON" in T.dispatch("add_note", "{bad", ctx)
    assert "bad arguments" in T.dispatch("add_note", {}, ctx)  # missing required arg
    # Unknown extra args are dropped instead of crashing.
    assert T.dispatch("get_time_date", {"bogus": 1}, ctx).startswith("It is")


def test_notes_roundtrip(ctx):
    assert T.dispatch("read_notes", {}, ctx) == "You have no notes yet."
    assert T.dispatch("add_note", json.dumps({"text": "buy milk"}), ctx) == "Note saved."
    assert "buy milk" in T.dispatch("read_notes", {"last_n": 5}, ctx)


def test_dangerous_tools_need_confirmation(ctx, tmp_path):
    asked = []
    ctx.confirm = lambda prompt: asked.append(prompt) or False
    T.dispatch("add_note", {"text": "keep me"}, ctx)
    assert T.dispatch("clear_notes", {}, ctx).startswith("Cancelled")
    assert "keep me" in T.dispatch("read_notes", {}, ctx)
    assert T.dispatch("run_shell_command", {"command": "echo hi"}, ctx).startswith("Cancelled")
    assert len(asked) == 2 and "permission" in asked[0]
    ctx.confirm = lambda prompt: True
    assert T.dispatch("clear_notes", {}, ctx) == "All notes cleared."
    out = T.dispatch("run_shell_command", {"command": "echo hello-friday"}, ctx)
    assert "hello-friday" in out and "Exit code 0" in out


def test_shell_can_be_disabled(ctx):
    ctx.confirm = lambda prompt: True
    ctx.settings.allow_shell = False
    assert "disabled" in T.dispatch("run_shell_command", {"command": "echo x"}, ctx)


def test_delete_file_outside_home_blocked(ctx):
    ctx.confirm = lambda prompt: True
    assert "only delete" in T.dispatch("delete_file", {"path": "/etc/hosts"}, ctx)


def test_search_files_restricted_to_home(ctx, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "budget_2026.xlsx").write_text("x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert "budget_2026.xlsx" in T.dispatch("search_files", {"pattern": "budget"}, ctx)
    assert "budget_2026.xlsx" in T.dispatch("search_files", {"pattern": "*.xlsx", "folder": "Documents"}, ctx)
    assert "only search inside" in T.dispatch("search_files", {"pattern": "*", "folder": "/etc"}, ctx)


def test_timer_and_reminder(ctx):
    spoken = []
    ctx.speak = spoken.append
    assert "Timer set" in T.dispatch("set_timer", {"seconds": 1, "label": "tea timer"}, ctx)
    assert "remind you" in T.dispatch("set_reminder", {"message": "stretch", "at_time": "23:59"}, ctx)
    assert "stretch" in T.dispatch("list_reminders", {}, ctx)
    import time
    time.sleep(1.5)
    assert spoken == ["Your tea timer is done."]
    assert "HH:MM" in T.dispatch("set_reminder", {"message": "x", "at_time": "noonish"}, ctx)
    for t in ctx.timers:
        t["timer"].cancel()


def test_web_tools_open_browser(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr(T.webbrowser, "open", opened.append)
    from friday import web
    monkeypatch.setattr(web.webbrowser, "open", opened.append)
    monkeypatch.setattr(web, "search", lambda q: [{"title": "Iron Man", "url": "https://x.test", "snippet": ""}])
    assert "Iron Man" in T.dispatch("web_search", {"query": "iron man", "open_in_browser": True}, ctx)
    T.dispatch("open_website", {"url": "github"}, ctx)
    assert opened == ["https://www.google.com/search?q=iron+man", "https://github.com"]
    monkeypatch.setattr(T, "_http_get", lambda url, timeout=10: 'xx"videoId":"dQw4w9WgXcQ"yy')
    assert "Playing" in T.dispatch("play_youtube", {"query": "song"}, ctx)
    assert opened[-1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_weather_parsing(ctx, monkeypatch):
    def fake(url, timeout=10):
        if "geocoding" in url:
            return json.dumps({"results": [{"name": "Toronto", "country": "Canada", "latitude": 43.7, "longitude": -79.4}]})
        return json.dumps({"current": {"temperature_2m": 3.2, "apparent_temperature": 0.1, "relative_humidity_2m": 70,
                                       "weather_code": 3, "wind_speed_10m": 12},
                           "daily": {"temperature_2m_max": [5], "temperature_2m_min": [-1],
                                     "precipitation_probability_max": [20]}})
    monkeypatch.setattr(T, "_http_get", fake)
    out = T.dispatch("get_weather", {}, ctx)
    assert "Toronto" in out and "overcast" in out and "3.2" in out


def test_system_info_and_memory_tools(ctx):
    assert "RAM" in T.dispatch("system_info", {}, ctx)
    T.dispatch("remember_fact", {"fact": "Ibbi likes coffee"}, ctx)
    assert ctx.memory.facts == ["Ibbi likes coffee"]
    assert "1" in T.dispatch("forget_fact", {"text": "coffee"}, ctx)


# ------------------------------------------------------------------ voice helpers
def test_wake_word_detection():
    ww = ["hey friday", "friday", "jarvis"]
    assert find_wake_word("Hey Friday, what's the weather?", ww) == (True, "what's the weather")
    assert find_wake_word("Jarvis open notepad", ww) == (True, "open notepad")
    assert find_wake_word("friday.", ww) == (True, "")
    assert find_wake_word("I love fridays", ww)[0] is False
    assert find_wake_word("random talk", ww)[0] is False


def test_yes_no_and_speech_cleanup():
    assert is_yes("Yes, go ahead") and is_yes("okay") and is_yes("y")
    assert not is_yes("no") and not is_yes("no, don't") and not is_yes("") and not is_yes("maybe later")
    assert clean_for_speech("**Hello** see https://x.com\n- item") == "Hello see the link item"


# ------------------------------------------------------------------ memory
def test_memory_persists_and_trims(tmp_path):
    m = Memory(tmp_path, max_messages=4)
    for i in range(5):
        m.add("user", f"q{i}")
        m.add("assistant", f"a{i}")
    m.remember("fact one")
    m2 = Memory(tmp_path, max_messages=4)
    assert [x["content"] for x in m2.history()] == ["q3", "a3", "q4", "a4"]
    assert m2.facts == ["fact one"]
    (tmp_path / "memory.json").write_text("{corrupt")
    assert Memory(tmp_path).messages == []


# ------------------------------------------------------------------ LLM client (mocked)
def _resp(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tc(id_, name, args):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeOpenAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_brain_native_tool_loop(ctx):
    client = FakeOpenAI([_resp(tool_calls=[_tc("c1", "add_note", {"text": "call mom"})]),
                         _resp(content="Noted, I'll remember to call mom.")])
    seen = []
    brain = Brain(ctx.settings, ctx.memory, ctx, client=client, on_tool=lambda n, a: seen.append((n, a)))
    reply = brain.chat("note that I need to call mom")
    assert reply == "Noted, I'll remember to call mom."
    assert seen == [("add_note", {"text": "call mom"})]
    assert "call mom" in T.dispatch("read_notes", {}, ctx)
    second = client.calls[1]["messages"]
    assert second[0]["role"] == "system" and "tools" in client.calls[0]
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "Note saved."}
    assert [m["role"] for m in ctx.memory.history()] == ["user", "assistant"]


def test_brain_auto_falls_back_to_json_mode(ctx):
    err = Exception("Error code: 400 - tools are not supported for this model")
    client = FakeOpenAI([err,
                         _resp(content='{"tool": "get_time_date", "args": {}}'),
                         _resp(content="It's late, boss.")])
    brain = Brain(ctx.settings, ctx.memory, ctx, client=client)
    assert brain.chat("can you check the clock for me") == "It's late, boss."
    assert brain.json_mode is True
    assert "tools" not in client.calls[1] and "TOOLS." in client.calls[1]["messages"][0]["content"]
    assert client.calls[2]["messages"][-1]["content"].startswith("TOOL_RESULT get_time_date: It is")


def test_brain_friendly_errors_not_saved(ctx):
    client = FakeOpenAI([Exception("Error code: 401 - invalid api key")])
    brain = Brain(ctx.settings, ctx.memory, ctx, client=client)
    assert "API key" in brain.chat("hi")
    assert ctx.memory.history() == []


def test_parse_json_tool_call():
    assert parse_json_tool_call('```json\n{"tool": "get_weather", "args": {"city": "Paris"}}\n```') == ("get_weather", {"city": "Paris"})
    assert parse_json_tool_call('{"tool": "not_a_tool"}') is None
    assert parse_json_tool_call("just talking about tools") is None


def test_brain_anthropic_tool_loop(ctx):
    ctx.settings.provider = "anthropic"
    calls = []
    responses = [
        SimpleNamespace(content=[SimpleNamespace(type="text", text="Checking."),
                                 SimpleNamespace(type="tool_use", id="tu1", name="get_time_date", input={})]),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="It is noon.")]),
    ]

    def create(**kw):
        calls.append(json.loads(json.dumps(kw, default=str)))
        return responses.pop(0)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    brain = Brain(ctx.settings, ctx.memory, ctx, client=client)
    assert brain.chat("time?") == "It is noon."
    assert calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    assert calls[0]["tools"][0]["input_schema"]["type"] == "object"


def test_cli_help_runs():
    import subprocess
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "friday", "--help"], cwd=root, capture_output=True, text=True)
    assert r.returncode == 0 and "--text" in r.stdout


# ---------------------------------------------------------------- local main brain + backup brain
@pytest.fixture
def hybrid(clean_env):
    s = load_settings(write_env(clean_env, "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen2.5:7b\nABACUS_API_KEY=k\n"
                                           "BACKUP_PROVIDER=abacus\nBACKUP_MAX_PER_DAY=2\nTOOL_MODE=native\n"))
    mem = Memory(s.data_dir)
    return T.ToolContext(settings=s, memory=mem, speak=lambda t: None, confirm=lambda p: False)


def _hybrid_brain(hctx, main_resps, backup_resps):
    main, backup = FakeOpenAI(main_resps), FakeOpenAI(backup_resps)
    brain = Brain(hctx.settings, hctx.memory, hctx)
    brain.reload(client=main, backup_client=backup)
    return brain, main, backup


def test_hybrid_settings(hybrid):
    s = hybrid.settings
    assert s.provider == "custom" and s.base_url == "http://localhost:11434/v1" and s.model == "qwen2.5:7b"
    b = s.backup_settings()
    assert (b.provider, b.api_key, b.model, b.base_url) == ("abacus", "k", "route-llm", "https://routellm.abacus.ai/v1")
    assert not b.has_backup and s.validate() == []


def test_local_answers_without_backup(hybrid):
    brain, main, backup = _hybrid_brain(hybrid, [_resp(content="Hi boss.")], [])
    assert brain.chat("hello") == "Hi boss."
    assert brain.last_route == "main" and backup.calls == [] and brain.backup_used_today() == 0
    assert "ESCALATE" in main.calls[0]["messages"][0]["content"]


def test_backup_on_local_failure_and_abacus_has_no_daily_cap(hybrid):
    down = Exception("Connection error: connection refused")
    brain, main, backup = _hybrid_brain(hybrid, [down, down, down, down, down, down],
                                        [_resp(content="Backup 1."), _resp(content="Backup 2."), _resp(content="Backup 3.")])
    assert brain.chat("q1") == "Backup 1." and brain.last_route == "backup (main AI failed)"
    assert brain.chat("q2") == "Backup 2." and brain.backup_used_today() == 2
    # the settings cap is 2, but the backup is Abacus and Abacus has no limit in Friday: it keeps answering
    assert brain.chat("q3") == "Backup 3." and len(backup.calls) == 3
    assert [m["content"] for m in hybrid.memory.history()] == ["q1", "Backup 1.", "q2", "Backup 2.", "q3", "Backup 3."]


def test_backup_on_phrase_and_self_escalation(hybrid):
    brain, main, backup = _hybrid_brain(hybrid, [_resp(content="ESCALATE")],
                                        [_resp(content="Deep answer."), _resp(content="Hard answer.")])
    assert brain.chat("think hard about my shop pricing") == "Deep answer." and main.calls == []
    assert brain.chat("prove the riemann hypothesis") == "Hard answer."
    assert brain.last_route == "backup (too hard for main AI)" and brain.backup_used_today() == 2


def test_no_backup_configured_keeps_old_behaviour(ctx):
    assert ctx.settings.has_backup is False
    brain = Brain(ctx.settings, ctx.memory, ctx, client=FakeOpenAI([_resp(content="ok")]))
    assert brain.backup is None and "ESCALATE" not in build_system_prompt(ctx.settings, None, False)
