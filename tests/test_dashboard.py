"""Tests for the dashboard, settings editor, status checks, usage tracking and always-on listening.
No microphone, speakers, Windows or network needed (everything external is faked)."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import checks, config  # noqa: E402
from friday import tools as T  # noqa: E402
from friday.settings_store import EnvStore, mask  # noqa: E402
from friday.usage import UsageTracker, extract_tokens  # noqa: E402
from friday.voice import in_followup, is_echo  # noqa: E402

ENV = """# Friday config
LLM_PROVIDER=abacus
ABACUS_API_KEY=s2_realkey_1234567890abcd
ABACUS_MODEL=route-llm
# a comment that must survive
INPUT_MODE=text
TTS_ENGINE=none
TRAY_ICON=false
DASHBOARD_PORT=0
DASHBOARD_OPEN_BROWSER=false
AUTO_CHECK_MINUTES=0
WAKE_WORDS=hey friday,friday
FOLLOWUP_SECONDS=20
"""
ALL_KEYS = ["LLM_PROVIDER", "ABACUS_API_KEY", "ABACUS_MODEL", "INPUT_MODE", "TTS_ENGINE", "TRAY_ICON", "DASHBOARD_PORT",
            "DASHBOARD_OPEN_BROWSER", "AUTO_CHECK_MINUTES", "WAKE_WORDS", "FOLLOWUP_SECONDS", "DATA_DIR", "DEFAULT_CITY",
            "DISABLED_TOOLS", "CONVERSATION_MODE", "EDGE_VOICE", "PTT_HOTKEY", "MUTE_HOTKEY", "STOP_HOTKEY",
            "ENERGY_THRESHOLD", "USER_NAME", "TOOL_MODE"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text(ENV, encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", p)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    T.DISABLED.clear()
    yield p
    T.DISABLED.clear()


# ------------------------------------------------------------------ settings store
def test_env_store_write_keeps_comments_and_backs_up(env):
    store = EnvStore()
    res = store.write({"DEFAULT_CITY": "St. Catharines", "INPUT_MODE": "always"})
    text = env.read_text()
    assert "# a comment that must survive" in text
    assert "INPUT_MODE=always" in text and "DEFAULT_CITY=St. Catharines" in text
    assert res["changed"] == ["DEFAULT_CITY", "INPUT_MODE"]
    assert Path(res["backup"]).exists() and "INPUT_MODE=text" in Path(res["backup"]).read_text()
    assert store.read()["DEFAULT_CITY"] == "St. Catharines"
    assert store.write({"DEFAULT_CITY": "St. Catharines"})["changed"] == []  # no-op, no backup


def test_env_store_validation_and_secret_masking(env):
    store = EnvStore()
    with pytest.raises(ValueError) as e:
        store.write({"FOLLOWUP_SECONDS": "abc", "INPUT_MODE": "telepathy", "NOT_A_SETTING": "1", "USER_NAME": "a\nb"})
    errs = e.value.args[0]
    assert set(errs) == {"FOLLOWUP_SECONDS", "INPUT_MODE", "NOT_A_SETTING", "USER_NAME"}
    view = {f["key"]: f for f in store.public_view()}
    assert view["ABACUS_API_KEY"]["has_value"] and view["ABACUS_API_KEY"]["value"] == mask("s2_realkey_1234567890abcd")
    assert "s2_realkey" not in json.dumps(store.public_view())  # the secret itself is never exposed
    assert view["ANTHROPIC_API_KEY"]["has_value"] is False


def test_env_store_quotes_special_values(env):
    EnvStore().write({"USER_NAME": 'Ibbi #1 "boss"'})
    s = config.load_settings(override=True)
    assert s.user_name == 'Ibbi #1 "boss"'


def test_load_settings_override_and_new_defaults(env):
    s = config.load_settings()
    assert s.input_mode == "text" and s.followup_seconds == 20 and s.dashboard is True
    env.write_text(env.read_text().replace("INPUT_MODE=text", "INPUT_MODE=always"))
    assert config.load_settings().input_mode == "text"  # without override the old env value sticks
    assert config.load_settings(override=True).input_mode == "always"


# ------------------------------------------------------------------ usage + echo + follow-up
def test_usage_tracking(tmp_path):
    u = UsageTracker(tmp_path)
    u.record(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20)), 250)
    u.record(SimpleNamespace(usage=SimpleNamespace(input_tokens=7, output_tokens=3)), 100)
    u.record(None, 50, error=True)
    s = u.summary()
    assert s["session"]["requests"] == 3 and s["session"]["errors"] == 1
    assert s["today"]["input_tokens"] == 107 and s["today"]["output_tokens"] == 23
    assert s["month"]["requests"] == 3
    assert UsageTracker(tmp_path).summary()["today"]["requests"] == 3  # persisted
    assert extract_tokens(SimpleNamespace()) == (0, 0)


def test_echo_guard_and_followup_window():
    spoken = "The weather in Toronto is 5 degrees and cloudy."
    assert is_echo("the weather in toronto is 5 degrees", spoken)
    assert not is_echo("open spotify please", spoken)
    assert not is_echo("", spoken)
    assert in_followup(100.0, 120.0) and not in_followup(130.0, 120.0)


def test_disabled_tools_hidden_and_refused():
    T.DISABLED.add("take_screenshot")
    try:
        assert "take_screenshot" not in [s["function"]["name"] for s in T.openai_tool_specs()]
        assert "take_screenshot" not in [s["name"] for s in T.anthropic_tool_specs()]
        assert "switched off" in T.dispatch("take_screenshot", {}, T.ToolContext())
    finally:
        T.DISABLED.discard("take_screenshot")
    assert "take_screenshot" in [s["function"]["name"] for s in T.openai_tool_specs()]


def test_timer_cancel():
    ctx = T.ToolContext()
    T.dispatch("set_timer", {"seconds": 300, "label": "tea"}, ctx)
    tid = ctx.timers[0]["id"]
    assert T.cancel_timer(ctx, tid) and ctx.timers == []
    assert not T.cancel_timer(ctx, "nope")


# ------------------------------------------------------------------ checks
def test_credit_check_parses_and_never_invents(monkeypatch, env):
    s = config.load_settings()
    monkeypatch.setattr(checks, "_http_json", lambda *a, **k: (
        {"success": True, "result": {"computePointsLeft": 18000.5, "totalComputePoints": 20000}}, 120))
    r = checks.check_abacus_credits(s)
    assert r["status"] == "ok" and r["details"]["credits_left"] == 18000.5 and r["details"]["percent_left"] == 90.0

    monkeypatch.setattr(checks, "_http_json", lambda *a, **k: ({"success": False}, 50))
    r = checks.check_abacus_credits(s)
    assert r["status"] == "warn" and "credits_left" not in r["details"] and "billing_page" in r["details"]

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(checks, "_http_json", boom)
    r = checks.check_abacus_credits(s)
    assert r["status"] == "warn" and "403" in r["summary"]


def test_api_check_reports_bad_key(monkeypatch, env):
    s = config.load_settings()

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            raise Exception("Error code: 401 - invalid api key")
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    r = checks.check_api(s)
    assert r["status"] == "error" and r["summary"].startswith("API key rejected")
    s.api_key = "your-key-here"
    assert checks.check_api(s)["summary"].startswith("No API key")


# ------------------------------------------------------------------ assistant + dashboard
def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


@pytest.fixture
def assistant(env):
    from friday.main import Assistant

    s = config.load_settings(override=True)
    a = Assistant(s, mode="text")
    replies = []
    a.brain.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: (replies.append(kw["messages"][-1]["content"].split("\n[clock:")[0]),  # clock is added by the brain
                             _resp("Sure thing."))[1])))
    a.brain_requests = replies
    yield a
    a.running = False
    if a.dashboard:
        a.dashboard.stop()


def call(a, path, body=None, token=True, host=None):
    url = f"http://127.0.0.1:{a.dashboard.port}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Friday-Token", a.dashboard.token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ct = r.headers.get("Content-Type", "")
            raw = r.read()
            return r.status, (json.loads(raw) if "json" in ct else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode()


def test_dashboard_security(assistant):
    a = assistant
    assert call(a, "/api/state", token=False)[0] == 401
    assert call(a, "/api/control", {"action": "mute_mic"}, token=False)[0] == 401
    assert call(a, "/api/state", host="evil.example.com")[0] == 403
    assert call(a, "/", token=False)[0] == 401
    assert call(a, "/?t=wrong", token=False)[0] == 403
    assert call(a, "/api/health", token=False) == (200, {"ok": True})
    # login link sets an HttpOnly SameSite cookie
    conn = __import__("http.client").client.HTTPConnection("127.0.0.1", a.dashboard.port, timeout=5)
    conn.request("GET", f"/?t={a.dashboard.token}")
    r = conn.getresponse()
    assert r.status == 302 and "HttpOnly" in r.getheader("Set-Cookie") and "SameSite=Strict" in r.getheader("Set-Cookie")
    code, html = call(a, "/")
    assert code == 200 and "J.A.R.V.I.S." in html


def test_dashboard_state_settings_and_tools(assistant, env):
    a = assistant
    code, st = call(a, "/api/state")
    assert code == 200 and st["assistant"]["mode"] == "text" and {x["id"] for x in st["agents"]} >= {"brain", "tts", "dashboard"}
    code, s = call(a, "/api/settings")
    assert code == 200 and "s2_realkey" not in json.dumps(s)
    # live-applied setting
    code, r = call(a, "/api/settings", {"updates": {"DEFAULT_CITY": "St. Catharines", "CONVERSATION_MODE": "true"}})
    assert code == 200 and r["restart_needed"] is False and a.s.default_city == "St. Catharines" and a.s.conversation_mode
    # restart-only setting
    code, r = call(a, "/api/settings", {"updates": {"TRAY_ICON": "true"}})
    assert code == 200 and r["restart_needed"] is True
    # brain setting rebuilds the client; key replaced without being echoed back
    code, r = call(a, "/api/settings", {"updates": {"ABACUS_MODEL": "gpt-4.1-mini"}})
    assert code == 200 and a.s.model == "gpt-4.1-mini"
    code, r = call(a, "/api/settings", {"updates": {"FOLLOWUP_SECONDS": "-5"}})
    assert code == 400 and "FOLLOWUP_SECONDS" in r["fields"]
    # tools on/off persists to .env
    code, _ = call(a, "/api/tools", {"name": "clipboard", "enabled": False})
    assert code == 200 and "clipboard" in T.DISABLED and "DISABLED_TOOLS=clipboard" in env.read_text()
    tools = {t["name"]: t for t in call(a, "/api/tools")[1]["tools"]}
    assert tools["clipboard"]["enabled"] is False and tools["delete_file"]["dangerous"] is True
    call(a, "/api/tools", {"name": "clipboard", "enabled": True})
    assert "clipboard" not in T.DISABLED


def test_dashboard_controls_memory_timers(assistant):
    a = assistant
    assert call(a, "/api/control", {"action": "mute_mic"})[1]["ok"] and a.bus.flags["mic_muted"] is True
    call(a, "/api/control", {"action": "unmute_mic"})
    assert a.bus.flags["mic_muted"] is False
    call(a, "/api/control", {"action": "conversation_on"})
    assert a.s.conversation_mode is True
    call(a, "/api/control", {"action": "mute_voice"})
    assert a.tts.muted is True
    code, r = call(a, "/api/memory/fact", {"fact": "Ibbi owns a Ducati"})
    assert "Ibbi owns a Ducati" in r["facts"]
    call(a, "/api/memory/fact", {"fact": "Ibbi owns a Ducati", "delete": True})
    assert a.memory.facts == []
    T.dispatch("set_reminder", {"message": "call the parts supplier", "in_minutes": 30}, a.ctx)
    st = call(a, "/api/state")[1]
    assert st["timers"][0]["message"].endswith("call the parts supplier")
    assert call(a, "/api/timers/cancel", {"id": st["timers"][0]["id"]})[1]["ok"] and a.ctx.timers == []


def test_dashboard_chat_goes_through_assistant(assistant):
    a = assistant
    assert call(a, "/api/chat", {"text": "hello there"})[0] == 200
    item = a.inputs.get(timeout=2)
    a.handle(item)
    assert a.brain_requests[-1] == "hello there"
    feed = call(a, "/api/state")[1]["feed"]
    assert feed[-2]["role"] == "user" and feed[-2]["via"] == "dashboard" and feed[-1]["text"] == "Sure thing."
    usage = call(a, "/api/usage")[1]
    assert usage["session"]["requests"] >= 1 and usage["today"]["input_tokens"] >= 10


def test_dashboard_approves_dangerous_action(assistant):
    a = assistant
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", T.dispatch("clear_notes", {}, a.ctx)))
    t.start()
    for _ in range(50):
        if a.bus.pending_confirm:
            break
        time.sleep(0.05)
    pending = call(a, "/api/state")[1]["pending_confirm"]
    assert pending and "clear_notes" in pending["prompt"]
    assert call(a, "/api/confirm", {"id": "wrong", "approve": True})[0] == 409
    assert call(a, "/api/confirm", {"id": pending["id"], "approve": True})[0] == 200
    t.join(5)
    assert result["r"] == "All notes cleared." and a.bus.pending_confirm is None


def test_dashboard_checks_endpoint_and_sse(assistant, monkeypatch):
    a = assistant
    monkeypatch.setitem(checks.CHECKS, "internet", lambda s, x: checks._result("internet", "Internet", "ok", "Connected"))
    code, r = call(a, "/api/checks/run", {"id": "internet"})
    assert code == 200 and r["results"][0]["status"] == "ok"
    assert call(a, "/api/checks/run", {"id": "bogus"})[0] == 404
    # Server-sent events: we should receive a published event
    req = urllib.request.Request(f"http://127.0.0.1:{a.dashboard.port}/api/events",
                                 headers={"X-Friday-Token": a.dashboard.token})
    resp = urllib.request.urlopen(req, timeout=5)
    threading.Timer(0.3, lambda: a.bus.log_event("test", "hello sse")).start()
    got = ""
    deadline = time.time() + 5
    while "hello sse" not in got and time.time() < deadline:
        got += resp.readline().decode()
    resp.close()
    assert "hello sse" in got


# ------------------------------------------------------------------ always-listening loop (fake mic + fake STT)
class FakeRecorder:
    def __init__(self, utterances):
        self.utterances = list(utterances)
        self.threshold = 0.01

    def _trigger(self):
        return self.threshold

    def listen(self, max_seconds=None, wait_seconds=None, should_abort=lambda: False):
        if not self.utterances:
            return None
        return self.utterances.pop(0)


class FakeSTT:
    engine = "whisper"

    def transcribe(self, audio):
        return audio  # the "audio" is already text in this test


def test_always_listening_wake_followup_noise_and_echo(assistant):
    a = assistant
    a.mode = "always"
    a.s.conversation_mode = False
    a.s.followup_seconds = 20
    a.stt = FakeSTT()
    a.tts.muted = True
    # 1) speech without wake word is ignored
    a.recorder = FakeRecorder(["what time is it"])
    a._listen_cycle()
    assert a.brain_requests == []
    # 2) wake word + request -> handled, follow-up window opens
    a.recorder = FakeRecorder(["Hey Friday, what's the best oil?"])  # (weather now goes straight to its tool)
    a._listen_cycle()
    assert a.brain_requests[-1] == "what's the best oil"
    assert a.bus.flags["conversation_until"] > time.time()
    # 3) follow-up without wake word -> handled
    a.recorder = FakeRecorder(["and tomorrow"])
    a._listen_cycle()
    assert a.brain_requests[-1] == "and tomorrow"
    # 4) whisper noise hallucination in the window -> ignored
    n = len(a.brain_requests)
    a.recorder = FakeRecorder(["Thank you."])
    a._listen_cycle()
    assert len(a.brain_requests) == n
    # 5) echo of Friday's own last sentence -> ignored
    a.tts.last_spoken, a.tts.ended_at = "Sure thing, the forecast is sunny all week long.", time.time()
    a.recorder = FakeRecorder(["the forecast is sunny all week long"])
    a._listen_cycle()
    assert len(a.brain_requests) == n
    # 6) "that's all" closes the window; then speech without wake word is ignored again
    a.tts.last_spoken = ""
    a.recorder = FakeRecorder(["that's all"])
    a._listen_cycle()
    assert a.bus.flags["conversation_until"] == 0.0
    a.recorder = FakeRecorder(["open spotify"])
    a._listen_cycle()
    assert len(a.brain_requests) == n
    # 7) conversation mode answers everything
    a.s.conversation_mode = True
    a.recorder = FakeRecorder(["open spotify"])
    a._listen_cycle()
    assert a.brain_requests[-1] == "open spotify"


def test_mic_failure_triggers_retry(assistant):
    a = assistant
    a.mode = "always"
    a.stt = FakeSTT()

    class Broken(FakeRecorder):
        def listen(self, **kw):
            raise OSError("device unplugged")
    a.recorder = Broken([])
    a._listen_cycle()
    assert a.recorder is None and a.bus.agents["voice_input"]["health"] == "error"
    assert "unplugged" in a.bus.agents["voice_input"]["error"]


def test_mic_hold_blocks_while_speaking_or_muted(assistant):
    a = assistant
    assert a._mic_hold() is False
    a.tts.speaking = True
    assert a._mic_hold() is True
    a.tts.speaking, a.tts.ended_at = False, 0
    a.set_mic_muted(True)
    assert a._mic_hold() is True


def test_recorder_discards_audio_while_held(monkeypatch):
    """Real Recorder logic with a fake sound device: audio during 'hold' must never be returned."""
    from friday import voice

    blocks = [np.full((voice.BLOCK, 1), 0.2, dtype="float32")] * 20 + \
             [np.full((voice.BLOCK, 1), 0.2, dtype="float32")] * 15 + [np.zeros((voice.BLOCK, 1), dtype="float32")] * 60
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
    fake_sd = SimpleNamespace(InputStream=lambda **kw: Stream(), query_devices=lambda: [])
    rec = voice.Recorder.__new__(voice.Recorder)
    rec.np, rec.sd = np, fake_sd
    rec.settings = SimpleNamespace(energy_threshold=0.05, silence_seconds=0.3, max_record_seconds=10)
    rec.threshold, rec.device = 0.05, None
    levels = []
    rec.on_level = levels.append
    rec.hold = lambda: state["i"] <= 20  # first 20 loud blocks happen while "Friday is speaking"
    audio = rec.listen(wait_seconds=5)
    assert audio is not None
    loud = int((np.abs(audio) > 0.1).sum() // voice.BLOCK)
    assert loud <= 15  # only the blocks after the hold ended were kept
    assert levels and max(levels) > 0.1


# ------------------------------------------------------------------ rebuilt dashboard (5 tabs)
def test_dashboard_page_has_five_tabs_and_the_route_strip(assistant):
    code, html = call(assistant, "/")
    assert code == 200
    for needle in ('data-tab="home"', 'data-tab="activity"', 'data-tab="learning"', 'data-tab="shop"',
                   'data-tab="settings"', 'id="route"', 'id="set-search"', "Use Abacus instead"):
        assert needle in html, needle


def test_limits_carry_credit_summary_and_tools_carry_their_source(assistant, monkeypatch):
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "1000")
    code, d = call(assistant, "/api/limits")
    c = d["credits"]
    assert code == 200 and c["allowance"] == 1000 and len(c["days"]) == 14 and c["days"][-1]["spent"] == c["spent_today"]
    code, d = call(assistant, "/api/tools")
    assert code == 200 and all("source" in t for t in d["tools"])


def test_dashboard_can_switch_the_brain_mode(assistant):
    from friday import brainmode

    code, d = call(assistant, "/api/control", {"action": "brain:local"})
    assert code == 200 and brainmode.get() == "local" and "Local only" in d["message"]
    assert call(assistant, "/api/control", {"action": "brain:skynet"})[0] == 400
    call(assistant, "/api/control", {"action": "brain:auto"})


# ------------------------------------------------------------------ playbooks on the dashboard
def test_dashboard_lists_switches_and_forgets_playbooks(assistant):
    from friday import blackbox, playbooks

    pb, _ = playbooks.clean({"title": "Show the desktop", "when": ["minimize"], "examples": ["minimize all windows"],
                             "steps": [{"tool": "press_keys", "args": {"keys": "win+d"}, "why": "x"}]}, T.REGISTRY)
    item = playbooks.add_or_update(pb, "abacus/route-llm")
    blackbox.begin_turn("minimize all windows", "abacus/route-llm")
    code, d = call(assistant, "/api/playbooks")
    assert code == 200 and d["playbooks"][0]["title"] == "Show the desktop" and d["running"] is False
    assert d["record"]["requests"] == 1 and "full review" in d["report"].lower() or "No full review" in d["report"]
    code, d = call(assistant, "/api/playbooks", {"id": item["id"], "enabled": True})
    assert code == 200 and d["playbooks"][0]["enabled"] is True
    assert call(assistant, "/api/playbooks", {})[0] == 400
    code, d = call(assistant, "/api/playbooks", {"delete": str(item["id"])})
    assert d["removed"] == 1 and d["playbooks"] == []
    assert call(assistant, "/api/playbooks", token=False)[0] == 401
    code, page = call(assistant, "/")
    assert 'id="distill-btn"' in page and 'id="playbooks"' in page
