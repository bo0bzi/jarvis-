import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from friday import local_api, notify
from friday.main import Assistant
from friday.voice import is_clear_answer, is_yes


def test_only_real_yes_or_no_counts_as_an_answer():
    for ok in ("yes", "Yeah go ahead", "no", "nope", "cancel that", "okay"):
        assert is_clear_answer(ok)
    for noise in ("what time is it", "", "hello there", "I'm watching the game"):
        assert not is_clear_answer(noise)  # room noise must never be taken as 'no'
    assert is_yes("yes please") and not is_yes("no")


def _fake_assistant():
    answers = []
    a = SimpleNamespace(_approval_tokens={"abc": "tok123"}, bus=SimpleNamespace(
        pending_confirm={"id": "abc", "prompt": "Approve changing my own code?"}, log_event=lambda *x, **k: None),
        answer_confirm=lambda cid, ok: answers.append((cid, ok)) or True)
    return a, answers


def test_token_gates_the_phone_button_and_works_once():
    a, answers = _fake_assistant()
    assert Assistant.api_approve(a, "abc", "wrong", True) is False and answers == []
    assert Assistant.api_approve(a, "zzz", "tok123", True) is False
    assert Assistant.api_approve(a, "abc", "tok123", True) is True and answers == [("abc", True)]
    assert Assistant.api_approve(a, "abc", "tok123", True) is False  # single use
    a2, _ = _fake_assistant()
    pend = Assistant.api_approval(a2)
    assert pend == {"id": "abc", "prompt": "Approve changing my own code?", "token": "tok123"}
    a2.bus.pending_confirm = None
    assert Assistant.api_approval(a2) is None


@pytest.fixture()
def api():
    state = {"calls": []}
    srv = local_api.make_server(lambda t: "ok", "secretkey", port=0,
                                approval=lambda: {"id": "abc", "prompt": "p", "token": "tok"},
                                approve=lambda cid, tok, yes: state["calls"].append((cid, tok, yes)) or (tok == "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def _post(url, body, key=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(
        {"X-API-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_approve_endpoint_needs_the_one_time_token_not_the_api_key(api):
    url, state = api
    assert _post(url + "/v1/approve", {"id": "abc", "token": "tok", "approve": True}) == (200, {"ok": True})
    assert state["calls"][-1] == ("abc", "tok", True)
    assert _post(url + "/v1/approve", {"id": "abc", "token": "bad", "approve": True})[0] == 409
    req = urllib.request.Request(url + "/v1/approval", headers={"X-API-Key": "secretkey"})
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["pending"]["prompt"] == "p"
    with pytest.raises(urllib.error.HTTPError) as e:  # reading what is pending DOES need the API key
        urllib.request.urlopen(url + "/v1/approval", timeout=5)
    assert e.value.code == 401


def test_service_worker_has_approve_deny_buttons_and_notify_carries_data(tmp_path, monkeypatch):
    assert "action: 'approve'" in local_api.SERVICE_WORKER and "/v1/approve" in local_api.SERVICE_WORKER
    seen = []
    monkeypatch.setattr(notify, "_send_webpush", lambda title, body, base=None, data=None: seen.append(data) or 0)
    notify.push("Jarvis needs your OK", "do it?", text_too=False, base=tmp_path,
                data={"approval": {"id": "abc", "token": "t"}})
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.05)
    assert seen == [{"approval": {"id": "abc", "token": "t"}}]
    assert notify.recent(0)[-1]["data"]["approval"]["id"] == "abc"
