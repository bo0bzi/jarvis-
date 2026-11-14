"""Phone page server side: live replies, the remote control, the status glance, instant approvals."""
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

import pytest

from friday import brainmode, local_api, phone
from friday.events import EventBus

KEY = "k" * 24


class FakeBrain:
    last_route = "backup (reasoning or memory question)"
    streamed_prefix = ""

    def __init__(self, bus):
        self.bus, self.on = bus, None

    def stream_to(self, fn):
        self.on = fn
        if fn is None:
            self.streamed_prefix = ""

    def chat(self, text):
        self.bus.trace("tool", "get_weather")
        self.bus.trace("heard", "not shown on the phone")
        self.on("It is sunny in Toronto today.")
        self.streamed_prefix = "It is sunny in Toronto today."
        return "It is sunny in Toronto today. Bring a hat."


def _assistant():
    bus = EventBus()
    a = NS(bus=bus, brain=FakeBrain(bus), tts=NS(busy=lambda: False, stop=lambda: None, muted=False),
           ui=NS(user=lambda *x, **k: None), _tls=NS(via=""), _brain_lock=threading.Lock(),
           s=NS(conversation_mode=False), mode="always", ctx=NS(timers=[]), last_checks={}, done=[])
    a.control = lambda action: a.done.append(action) or f"did {action}"
    a.api_approval = lambda: bus.pending_confirm and {**bus.pending_confirm, "token": "tok"}
    return a


def test_ask_stream_sends_sentences_steps_and_the_final_reply():
    a, got = _assistant(), []
    out = phone.ask_stream(a, "weather?", got.append)
    assert {"delta": "It is sunny in Toronto today."} in got
    assert out["reply"].endswith("Bring a hat.") and out["spoken"] == "It is sunny in Toronto today."
    assert out["route"] == "Abacus" and a._tls.via == "" and a.brain.on is None
    assert not a.bus._subs  # the listener for steps is removed again


def test_ask_stream_survives_a_phone_that_went_away():
    a = _assistant()
    a.brain.chat = lambda text: "Done."  # no early sentences: nothing is sent while it works

    def gone(obj):
        raise ConnectionResetError

    assert phone.ask_stream(a, "x", gone)["reply"] == "Done."


def test_route_names():
    assert phone.route_name("main") == "Local AI" and phone.route_name("") == "Local AI"
    assert phone.route_name("backup (think hard)") == "Abacus"
    assert phone.route_name("Claude sonnet (hard question)") == "Claude Sonnet"
    assert phone.route_name("free cloud (general)") == "Free cloud"


def test_control_allows_only_known_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(brainmode, "data_dir", lambda: tmp_path)
    a = _assistant()
    assert phone.control(a, "mute_mic") == "did mute_mic" and a.done == ["mute_mic"]
    assert "Local only" in phone.control(a, "brain:local") and brainmode.get() == "local"
    for bad in ("", "rm -rf", "mode:text; shutdown", "brain:skynet"):
        with pytest.raises(ValueError):
            phone.control(a, bad)
    assert a.done == ["mute_mic"]


def test_status_has_switches_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(brainmode, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("ABACUS_API_KEY", "sk-secret-abacus-123456")
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "1000")
    a = _assistant()
    a.last_checks = {"api": {"id": "api", "name": "AI connection", "status": "ok", "summary": "fine", "details": {"x": 1}}}
    a.bus.flags["mic_muted"] = True
    st = phone.status(a)
    assert st["switches"] == {"mic_muted": True, "voice_muted": False, "conversation_mode": False, "mode": "always",
                              "brain_mode": "auto"}
    assert st["health"] == [{"name": "AI connection", "status": "ok", "summary": "fine"}]
    assert st["credits"].get("allowance") == 1000
    assert "sk-secret" not in json.dumps(st)


def test_events_pass_on_state_and_approvals_only():
    a = _assistant()
    nxt, close = phone.open_events(a)
    a.bus.log_event("x", "noise the phone doesn't need")
    a.bus.set_status("thinking", "weather")
    assert nxt(1) == {"type": "status", "state": "thinking", "detail": "weather"}
    a.bus.pending_confirm = {"id": "c1", "prompt": "Use Claude? Estimate 3-9 credits."}
    a.bus.publish("confirm", {"pending": a.bus.pending_confirm})
    assert nxt(1) == {"type": "approval", "pending": {"id": "c1", "prompt": "Use Claude? Estimate 3-9 credits.", "token": "tok"}}
    assert nxt(0.05) is None
    close()
    assert not a.bus._subs


# ---- over HTTP ----

@pytest.fixture
def api():
    a = _assistant()
    srv = local_api.make_server(lambda t: f"echo {t}", KEY, "127.0.0.1", 0, approval=a.api_approval,
                                ask_stream=lambda text, emit: phone.ask_stream(a, text, emit),
                                control=lambda action: phone.control(a, action),
                                status=lambda: {"switches": {"mode": "always"}},
                                events=lambda: phone.open_events(a))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", a
    srv.shutdown()


def _req(url, body=None, key=KEY):
    return urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                  headers={"X-API-Key": key, "Content-Type": "application/json"})


def test_http_stream_is_one_json_object_per_line(api):
    url, _ = api
    with urllib.request.urlopen(_req(url + "/v1/ask/stream", {"text": "weather?"}), timeout=5) as r:
        assert r.headers["Content-Type"].startswith("application/x-ndjson")
        lines = [json.loads(x) for x in r.read().decode().splitlines() if x.strip()]
    assert {"delta": "It is sunny in Toronto today."} in lines
    assert {"step": "tool", "text": "get_weather"} in lines
    assert lines[-1]["final"] is True and lines[-1]["reply"].endswith("Bring a hat.") and lines[-1]["route"] == "Abacus"


def test_http_control_status_and_events(api):
    url, a = api
    with urllib.request.urlopen(_req(url + "/v1/control", {"action": "stop_speaking"}), timeout=5) as r:
        assert json.loads(r.read()) == {"ok": True, "message": "did stop_speaking"}
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(_req(url + "/v1/control", {"action": "format_c"}), timeout=5)
    assert e.value.code == 400 and a.done == ["stop_speaking"]
    with urllib.request.urlopen(_req(url + "/v1/status"), timeout=5) as r:
        assert json.loads(r.read())["switches"]["mode"] == "always"
    with urllib.request.urlopen(_req(url + "/v1/events"), timeout=5) as r:
        assert json.loads(r.readline()) == {"type": "approval", "pending": None}  # what is waiting right now, first
        a.bus.set_status("speaking")
        assert json.loads(r.readline())["state"] == "speaking"


def test_new_routes_need_the_key_and_answer_501_when_not_wired(api):
    url, _ = api
    for path, body in (("/v1/status", None), ("/v1/events", None), ("/v1/control", {"action": "mute_mic"}),
                       ("/v1/ask/stream", {"text": "hi"})):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(_req(url + path, body, key="wrong"), timeout=5)
        assert e.value.code == 401
    srv = local_api.make_server(lambda t: t, KEY, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        for path, body in (("/v1/status", None), ("/v1/events", None), ("/v1/control", {"action": "mute_mic"}),
                           ("/v1/ask/stream", {"text": "hi"})):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(_req(base + path, body), timeout=5)
            assert e.value.code == 501
    finally:
        srv.shutdown()


def test_phone_page_has_the_four_screens_and_uses_the_new_routes(api):
    url, _ = api
    with urllib.request.urlopen(url + "/", timeout=5) as r:
        html = r.read().decode()
    for needle in ('id="v-talk"', 'id="v-appr"', 'id="v-ctrl"', 'id="v-stat"', "/v1/ask/stream", "/v1/events",
                   "/v1/control", "/v1/status", "Use Abacus instead", "friday_chat"):
        assert needle in html, needle
    assert KEY not in html
