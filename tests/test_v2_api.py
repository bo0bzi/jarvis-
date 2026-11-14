"""v2.0 local API: key required, /v1/ask and OpenAI-style chat, localhost-only by default."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from friday import local_api

KEY = "k" * 24


@pytest.fixture
def api():
    srv = local_api.make_server(lambda text: f"echo {text}", KEY, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _call(url, body=None, key=KEY):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_api_requires_key(api):
    with pytest.raises(urllib.error.HTTPError) as e:
        _call(api + "/v1/health", key="wrong")
    assert e.value.code == 401
    assert _call(api + "/v1/health")["ok"]


def test_api_ask_and_openai(api):
    assert _call(api + "/v1/ask", {"text": "hi"}) == {"reply": "echo hi"}
    out = _call(api + "/v1/chat/completions", {"model": "friday", "messages": [
        {"role": "system", "content": "x"}, {"role": "user", "content": "what jobs are open"}]})
    assert out["choices"][0]["message"]["content"] == "echo what jobs are open"


def test_api_off_without_key_and_local_only(monkeypatch):
    monkeypatch.delenv("FRIDAY_API_KEY", raising=False)
    assert local_api.start(lambda t: t) is None
    monkeypatch.setenv("FRIDAY_API_KEY", "short")
    assert local_api.start(lambda t: t) is None
    monkeypatch.setenv("FRIDAY_API_KEY", KEY)
    monkeypatch.setenv("API_PORT", "0")
    srv = local_api.start(lambda t: t)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.shutdown()



def test_phone_page_served_without_key_and_has_no_secret(api):
    with urllib.request.urlopen(api + "/", timeout=5) as r:
        html = r.read().decode()
        assert r.headers["Content-Type"].startswith("text/html")
    assert "/v1/ask" in html and "X-API-Key" in html
    assert KEY not in html
    with pytest.raises(urllib.error.HTTPError) as e:  # everything else still needs the key
        urllib.request.urlopen(api + "/v1/health", timeout=5)
    assert e.value.code == 401


# ---- v2.0.3: talk to Friday from the phone (voice in via PC Whisper, voice out in Friday's PC voice) ----

def _raw(url, data, key=KEY, ctype="audio/mp4"):
    req = urllib.request.Request(url, data=data, headers={"X-API-Key": key, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read(), r.headers["Content-Type"]


@pytest.fixture
def voice_api():
    seen = {}

    def listen(path):
        with open(path, "rb") as f:
            seen["data"], seen["path"] = f.read(), path
        return "  turn on the lights  "

    srv = local_api.make_server(lambda t: f"echo {t}", KEY, "127.0.0.1", 0,
                                speak=lambda t: (b"ID3fake-mp3:" + t.encode(), "audio/mpeg"), listen=listen)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_speak_returns_audio_bytes(voice_api):
    url, _ = voice_api
    data, ctype = _raw(url + "/v1/speak", json.dumps({"text": "hello"}).encode(), ctype="application/json")
    assert data == b"ID3fake-mp3:hello" and ctype == "audio/mpeg"


def test_listen_transcribes_uploaded_file(voice_api):
    url, seen = voice_api
    data, _ = _raw(url + "/v1/listen", b"\x00\x01fake-m4a-audio", ctype="audio/mp4")
    assert json.loads(data) == {"text": "turn on the lights"}
    assert seen["data"] == b"\x00\x01fake-m4a-audio" and seen["path"].endswith(".mp4")
    import os
    assert not os.path.exists(seen["path"])  # temp recording cleaned up
    _raw(url + "/v1/listen", b"webm", ctype="audio/webm;codecs=opus")
    assert seen["path"].endswith(".webm")


def test_voice_endpoints_need_key(voice_api):
    url, _ = voice_api
    for path, body in (("/v1/speak", json.dumps({"text": "x"}).encode()), ("/v1/listen", b"audio")):
        with pytest.raises(urllib.error.HTTPError) as e:
            _raw(url + path, body, key="wrong")
        assert e.value.code == 401


def test_voice_endpoints_501_when_not_wired(api):
    for path, body in (("/v1/speak", json.dumps({"text": "x"}).encode()), ("/v1/listen", b"audio")):
        with pytest.raises(urllib.error.HTTPError) as e:
            _raw(api + path, body)
        assert e.value.code == 501


def test_listen_rejects_too_big_and_empty(voice_api, monkeypatch):
    url, _ = voice_api
    monkeypatch.setattr(local_api, "MAX_AUDIO", 10)
    with pytest.raises(urllib.error.HTTPError) as e:
        _raw(url + "/v1/listen", b"x" * 50)
    assert e.value.code == 413
    with pytest.raises(urllib.error.HTTPError) as e:
        _raw(url + "/v1/listen", b"")
    assert e.value.code == 400


def test_speak_500_when_no_voice():
    srv = local_api.make_server(lambda t: t, KEY, "127.0.0.1", 0, speak=lambda t: (b"", ""))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _raw(f"http://127.0.0.1:{srv.server_address[1]}/v1/speak", b'{"text": "hi"}', ctype="application/json")
        assert e.value.code == 500
    finally:
        srv.shutdown()


def test_audio_suffix():
    assert local_api.audio_suffix("audio/mp4") == ".mp4"
    assert local_api.audio_suffix("audio/x-m4a") == ".m4a"
    assert local_api.audio_suffix("audio/wav") == ".wav"
    assert local_api.audio_suffix("") == ".webm"


def test_phone_page_has_mic_button(api):
    with urllib.request.urlopen(api + "/", timeout=5) as r:
        html = r.read().decode()
    assert 'id="mic"' in html and "/v1/listen" in html and "/v1/speak" in html
    assert "MediaRecorder" in html and 'id="hands"' in html and "tailscale serve" in html


def test_render_silent_when_tts_off():
    from types import SimpleNamespace
    from friday.voice import TextToSpeech
    tts = TextToSpeech(SimpleNamespace(tts_engine="none", edge_voice="x", edge_rate="+0%", pyttsx3_rate=180))
    assert tts.render("hello there") == (b"", "")
    tts.settings.tts_engine = "pyttsx3"
    assert tts.render("   ") == (b"", "")


def test_phone_access_script_present():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    bat = (root / "phone_access.bat").read_bytes()
    assert b"\r\n" in bat and b"scripts\\phone_access.ps1" in bat
    ps = (root / "scripts" / "phone_access.ps1").read_text()
    assert "serve --bg $port" in ps and "Friday Phone Access.lnk" in ps
    assert "serve --https=443 off" in ps
