"""Shared test setup: isolate data, and pre-approve cloud credits so fake cloud clients work.
(The real Friday never pre-approves anything unless you set CREDIT_PREAPPROVED_PER_DAY yourself.)"""
import pytest

from friday import credits


@pytest.fixture(autouse=True)
def _isolated_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "1000000")
    monkeypatch.setenv("CREDIT_DAILY_CAP", "1000000")
    monkeypatch.setenv("CREDIT_MONTHLY_CAP", "1000000")
    monkeypatch.setenv("INSTANT_SMALL_TALK", "off")  # old tests use "hi"/"hello" to exercise the AI itself
    # never let a test reach the real OmniRoute / your real .env keys (tests that need them set their own)
    for k in ("FREE_ROUTER_KEY", "FREE_ROUTER_URL", "FREE_ROUTER_MODEL", "FREE_ROUTER_MAX_PER_DAY",
              "ANTHROPIC_API_KEY", "ABACUS_API_KEY", "OPENAI_API_KEY", "QUO_API_KEY"):
        monkeypatch.delenv(k, raising=False)  # a test must NEVER be able to call a paid AI or text with a real key
    monkeypatch.setenv("CLAUDE_TIERS", "false")  # tests that exercise the Claude tiers switch them on with a fake key
    from friday import web

    monkeypatch.setattr(web, "DNS_CHECK", False)  # keep tests off the network: no DNS lookups for fake hostnames
    credits.GATE = None
    yield
    credits.GATE = None
