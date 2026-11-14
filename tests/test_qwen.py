"""Qwen3 / Qwen3.5 support: thinking switched off for speed, <think> text never spoken. Fakes only."""
import json
from types import SimpleNamespace

import pytest

from friday import tools as T
from friday.config import load_settings
from friday.llm import Brain, strip_think


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
                           usage=None)


class Fake:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _brain(tmp_path, monkeypatch, model, responses, extra=""):
    for k in ("LLM_PROVIDER", "BACKUP_PROVIDER", "CUSTOM_MODEL", "LOCAL_THINKING", "TOOL_MODE", "ABACUS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"DATA_DIR={tmp_path / 'data'}\nLLM_PROVIDER=custom\nCUSTOM_MODEL={model}\nTOOL_MODE=native\n{extra}")
    s = load_settings(env, override=True)
    fake = Fake(responses)
    return Brain(s, None, T.ToolContext(settings=s), client=fake), fake


def test_strip_think():
    assert strip_think("<think>hmm, let me see</think>\nHello Ibbi") == "Hello Ibbi"
    assert strip_think("Hi <THINK>a\nb</THINK>there") == "Hi there"
    assert strip_think("<think>never closed") == ""
    assert strip_think(None) == ""
    assert strip_think("plain") == "plain"


def test_qwen35_gets_thinking_off_and_clean_reply(tmp_path, monkeypatch):
    b, fake = _brain(tmp_path, monkeypatch, "qwen3.5:9b", [_resp("<think>long thoughts</think>Done, boss.")])
    assert b.chat("hello") == "Done, boss."
    assert fake.calls[0]["extra_body"]["think"] is False


def test_qwen25_untouched(tmp_path, monkeypatch):
    b, fake = _brain(tmp_path, monkeypatch, "qwen2.5:14b", [_resp("hi")])
    assert b.chat("hello") == "hi"
    assert "extra_body" not in fake.calls[0]


def test_local_thinking_opt_in(tmp_path, monkeypatch):
    b, fake = _brain(tmp_path, monkeypatch, "qwen3:14b", [_resp("hi")], extra="LOCAL_THINKING=true\n")
    monkeypatch.setenv("LOCAL_THINKING", "true")
    b.chat("hello")
    assert "extra_body" not in fake.calls[0]


def test_server_rejecting_switch_falls_back(tmp_path, monkeypatch):
    err = RuntimeError("unknown field: think")
    b, fake = _brain(tmp_path, monkeypatch, "qwen3.5:9b", [err, _resp("ok"), _resp("again")])
    assert b.chat("hello") == "ok"
    assert "extra_body" in fake.calls[0] and "extra_body" not in fake.calls[1]
    b.chat("hello again")
    assert "extra_body" not in fake.calls[2]  # remembered: not sent again


def test_other_errors_not_swallowed(tmp_path, monkeypatch):
    b, fake = _brain(tmp_path, monkeypatch, "qwen3.5:9b", [RuntimeError("connection refused")] * 3)
    b.chat("hello")  # brain turns the error into a friendly message
    assert all("extra_body" in c for c in fake.calls)
