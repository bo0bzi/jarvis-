"""v2.0: Credit Gate, autonomy dial, safety cage (sandbox) and SMS link - all with fakes, no network."""
import io
import json
import textwrap
from types import SimpleNamespace

import pytest

from friday import autonomy, credits, sandbox, sms
from friday import tools as T
from friday.credits import CreditGate, CreditsDenied


# ---------------------------------------------------------------- Credit Gate
@pytest.fixture
def strict(monkeypatch):
    """The real defaults: nothing pre-approved."""
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "0")
    monkeypatch.setenv("CREDIT_DAILY_CAP", "200")
    monkeypatch.setenv("CREDIT_MONTHLY_CAP", "2000")
    monkeypatch.setenv("CREDITS_PER_1K_TOKENS", "1")


def test_gate_blocks_calls_without_approval(strict):
    g = CreditGate()
    with pytest.raises(CreditsDenied):
        g.before_call()


def test_gate_asks_with_estimate_and_logs_actual(strict):
    asked = []
    g = CreditGate(confirm=lambda p: asked.append(p) or True)
    lo, hi = g.estimate("chat", "hello")
    assert 1 <= lo < hi
    with g.task("say hi", "testing", lo, hi) as ok:
        assert ok
        g.before_call()
        g.charge(1000, 500)  # 1.5k tokens at 1 credit / 1k
    assert "Estimate" in asked[0] and f"{lo} to {hi} credits" in asked[0]
    assert g.spent_today() == 1.5
    assert g.status()["ledger"][-1]["actual"] == 1.5


def test_gate_declined_and_caps(strict, monkeypatch):
    g = CreditGate(confirm=lambda p: False)
    with g.task("x", "y", 1, 5) as ok:
        assert not ok
    assert "declined" in g.last_denial
    monkeypatch.setenv("CREDIT_DAILY_CAP", "3")
    g2 = CreditGate(confirm=lambda p: pytest.fail("must not even ask above the cap"))
    with g2.task("big job", "y", 10, 20) as ok:
        assert not ok
    assert "cap" in g2.last_denial


def test_gate_reasks_when_task_runs_over_budget(strict):
    answers = [True, False]
    asked = []
    g = CreditGate(confirm=lambda p: asked.append(p) or answers.pop(0))
    with g.task("long job", "testing", 1, 2) as ok:
        assert ok
        g.before_call()
        g.charge(3000, 0)  # 3 credits > 2 approved
        with pytest.raises(CreditsDenied):
            g.before_call()
    assert len(asked) == 2 and "more credits" in asked[1]


def test_preapproved_allowance(monkeypatch, strict):
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "10")
    g = CreditGate(confirm=lambda p: pytest.fail("covered by allowance"))
    with g.task("small", "y", 1, 4) as ok:
        assert ok


def test_cloud_main_brain_asks_first(strict, tmp_path, monkeypatch):
    from friday.config import load_settings
    from friday.llm import Brain

    called = []
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: called.append(1) or
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))], usage=None))))
    env = tmp_path / ".env"
    env.write_text(f"DATA_DIR={tmp_path / 'data'}\nLLM_PROVIDER=openai\nOPENAI_API_KEY=test-not-real\nBACKUP_PROVIDER=none\n")
    for k in ("LLM_PROVIDER", "BACKUP_PROVIDER", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(env, override=True)
    credits.GATE = CreditGate(confirm=lambda p: False)
    b = Brain(s, None, T.ToolContext(settings=s), client=fake)
    out = b.chat("hello")
    assert "didn't use the cloud AI" in out and not called
    credits.GATE = CreditGate(confirm=lambda p: True)
    assert b.chat("hello") == "hi" and called


# ---------------------------------------------------------------- autonomy dial
def _t(**kw):
    base = dict(name="x", description="", params={}, func=lambda ctx: "", dangerous=False, source="built-in")
    base.update(kw)
    return T.Tool(**base)


def test_autonomy_levels(monkeypatch):
    safe, risky, send = _t(), _t(dangerous=True), _t(dangerous=True, risk="send")
    learned, system = _t(source="skill:x"), _t(dangerous=True, risk="system")
    monkeypatch.setenv("AUTONOMY_LEVEL", "0")
    assert autonomy.needs_approval(learned) and autonomy.needs_approval(risky) and not autonomy.needs_approval(safe)
    monkeypatch.setenv("AUTONOMY_LEVEL", "1")
    assert not autonomy.needs_approval(learned) and autonomy.needs_approval(risky)
    monkeypatch.setenv("AUTONOMY_LEVEL", "2")
    assert not autonomy.needs_approval(risky) and autonomy.needs_approval(system)
    assert autonomy.needs_approval(send)  # sending / deleting / spending always asks
    monkeypatch.setenv("NEVER_TOOLS", "x")
    assert autonomy.blocked(safe)


# ---------------------------------------------------------------- safety cage
def test_detect_permissions():
    assert sandbox.detect_permissions("import random\nx = random.random()") == []
    assert sandbox.detect_permissions("import urllib.request") == ["internet"]
    assert "files" in sandbox.detect_permissions("open('a.txt', 'w').write('x')")
    assert "run" in sandbox.detect_permissions("import subprocess")
    assert sandbox.detect_permissions("import smtplib") == ["email", "internet"]
    card = sandbox.permission_card("demo", ["internet"], [{"name": "t", "description": "d"}], "github.com/a/b")
    assert "use the internet" in card and "safety cage" in card


CAGED = textwrap.dedent('''
    from friday.tools import tool

    @tool("cage_add", "Add.", {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}})
    def cage_add(ctx, a: int = 1, b: int = 2):
        return str(a + b)

    @tool("cage_read", "Read a file.", {"type": "object", "properties": {"p": {"type": "string"}}})
    def cage_read(ctx, p: str):
        return open(p).read()

    @tool("cage_net", "Internet.")
    def cage_net(ctx):
        import socket
        socket.getaddrinfo("example.com", 80)
        return "connected"

    @tool("cage_env", "Env.")
    def cage_env(ctx):
        import os
        return "KEY=" + os.environ.get("ABACUS_API_KEY", "none")
''')


def test_sandbox_runs_pure_code_and_blocks_the_rest(tmp_path, monkeypatch):
    skill = tmp_path / "cagetest.py"
    skill.write_text(CAGED)
    secret = tmp_path / ".env"
    secret.write_text("ABACUS_API_KEY=very-secret")
    monkeypatch.setenv("ABACUS_API_KEY", "very-secret")
    assert sandbox.run_tool(skill, "cage_add", {"a": 2, "b": 3}, []) == "5"
    assert "Safety cage" in sandbox.run_tool(skill, "cage_read", {"p": str(secret)}, [])
    assert "Safety cage" in sandbox.run_tool(skill, "cage_read", {"p": str(sandbox.PROJECT_ROOT / "README.md")}, [])
    assert "internet permission" in sandbox.run_tool(skill, "cage_net", {}, [])
    assert sandbox.run_tool(skill, "cage_env", {}, []) == "KEY=none"  # keys never reach skills


# ---------------------------------------------------------------- SMS (Quo) with a fake server
class FakeQuo:
    def __init__(self):
        self.inbox, self.sent = [], []

    def __call__(self, req, timeout=0):
        url = req.full_url
        if req.get_method() == "POST":
            self.sent.append(json.loads(req.data))
            body = {"data": {"id": "MSG"}}
        elif "/phone-numbers" in url:
            body = {"data": [{"id": "PN1", "number": "+19055550000"}]}
        elif "/messages" in url:
            body = {"data": list(self.inbox)}
        else:
            raise AssertionError(url)
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def quo(monkeypatch):
    for k, v in {"SMS_ENABLED": "true", "QUO_API_KEY": "test", "OWNER_PHONE": "(905) 555-1234",
                 "FRIDAY_SMS_NUMBER": "905-555-0000", "SMS_PIN": "4321"}.items():
        monkeypatch.setenv(k, v)
    fake = FakeQuo()
    got = []
    link = sms.SmsLink(on_message=lambda text, images: got.append(text) or f"echo {text}", opener=fake)
    return link, fake, got


def _msg(i, frm, text, direction="incoming"):
    return {"id": f"M{i}", "from": frm, "text": text, "direction": direction, "createdAt": f"2026-11-10T12:00:0{i}Z"}


def test_sms_first_poll_only_marks_start(quo):
    link, fake, got = quo
    fake.inbox = [_msg(1, "+19055551234", "old message")]
    assert link.poll_once() == 0 and not got and "since" in link.state


def test_sms_answers_owner_ignores_strangers(quo):
    link, fake, got = quo
    link.poll_once()
    fake.inbox = [_msg(1, "+19055551234", "what's on today"), _msg(2, "+14165559999", "hi who is this"),
                  _msg(3, "+19055550000", "my own reply", "outgoing")]
    assert link.poll_once() == 2
    assert got == ["what's on today"]
    assert fake.sent == [{"content": "echo what's on today", "from": "PN1", "to": ["+19055551234"]}]
    assert link.poll_once() == 0  # already seen


def test_sms_approval_needs_pin(quo):
    import threading

    link, fake, got = quo
    link.poll_once()
    link.pending = {"prompt": "ok?", "answer": None, "event": threading.Event()}
    fake.inbox = [_msg(1, "+19055551234", "YES 1111")]
    link.poll_once()
    assert link.pending["answer"] is None and "Wrong or missing PIN" in fake.sent[-1]["content"]
    fake.inbox.append(_msg(2, "+19055551234", "yes 4321"))
    link.poll_once()
    assert link.pending["answer"] is True and not got


def test_sms_not_configured_tool():
    assert "isn't set up" in T.dispatch("text_me", {"message": "hi"}, T.ToolContext())
