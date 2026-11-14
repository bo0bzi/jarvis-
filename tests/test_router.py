"""The escalation ladder, pinned: free cloud tier -> Abacus -> Claude, and the shortcuts that skip a tier.

These tests drive RouterMixin directly with a stub, so the order is asserted without a real model or network."""
from __future__ import annotations

import pytest

from friday import brainmode, claude_tiers, split
from friday.router import RouterMixin


class Stub(RouterMixin):
    """A RouterMixin with the four brains replaced by recorders."""

    def __init__(self, free=None, backup=None, claude=None, free_ok=True, backup_ok=True, cloud=False):
        self.calls: list[str] = []
        self.sub = False
        self.cloud = cloud
        self.settings = type("S", (), {"data_dir": None, "backup_max_per_day": 5, "free_router_max_per_day": 5,
                                       "escalate_phrases": ["think hard"], "has_backup": backup is not None,
                                       "has_free_router": free is not None})()
        self.memory = None
        self.ctx = None
        self.last_route = ""
        self.last_error = ""
        self._free_ok = free_ok
        self._backup_ok = backup_ok
        self._free_answer = free
        self._backup_answer = backup
        self._claude_answer = claude
        self.free_router = object() if free is not None else None
        self.backup = object() if backup is not None else None

    def _trace(self, kind, text, detail=""):
        pass

    def _context_chars(self):
        return 100

    def _free_allowed(self):
        return self._free_ok and self.free_router is not None

    def _backup_allowed(self):
        return self._backup_ok and self.backup is not None

    def _try_free(self, user_text, reason):
        self.calls.append("free")
        return self._free_answer

    def _try_backup(self, user_text, reason):
        self.calls.append("backup")
        if self._backup_answer is None:
            return None, True, ""  # declined
        return self._backup_answer, False, ""

    def _try_claude(self, user_text, reason):
        self.calls.append("claude")
        return self._claude_answer, False

    def _retry_local(self, user_text, why):
        self.calls.append("local")
        return f"local:{why}"


@pytest.fixture()
def claude_on(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_TIERS", "true")
    return claude_tiers


def test_free_tier_answers_first_and_stops_the_ladder():
    r = Stub(free="free answer", backup="abacus answer")
    assert r._escalate("what's the news", "general") == "free answer"
    assert r.calls == ["free"]  # Abacus and Claude were never asked


def test_free_failure_falls_through_to_abacus():
    r = Stub(free=None, backup="abacus answer")
    assert r._escalate("what's the news", "general") == "abacus answer"
    assert r.calls == ["backup"]  # the free tier was never allowed, so it was skipped entirely


def test_abacus_declined_goes_back_to_local_not_claude(claude_on):
    r = Stub(free=None, backup="abacus answer", claude="claude answer")
    r._backup_answer = None  # the owner said no to the Abacus approval
    assert r._escalate("hard one", "reasoning").startswith("local:")
    assert r.calls == ["backup", "local"]  # a "no" to Abacus never escalates further


def test_claude_is_last_resort_after_abacus_fails(claude_on):
    r = Stub(free=None, backup=None, claude="claude answer")
    r._backup_answer = None
    r._backup_ok = False  # Abacus not allowed at all
    assert r._escalate("hard one", "reasoning") == "claude answer"
    assert r.calls == ["claude"]


def test_naming_claude_skips_free_and_abacus(claude_on):
    r = Stub(free="free answer", backup="abacus answer", claude="claude answer")
    assert r._escalate("ask claude about this", "you asked") == "claude answer"
    assert r.calls == ["claude"]


def test_naming_abacus_skips_claude(claude_on):
    r = Stub(free="free answer", backup="abacus answer", claude="claude answer")
    assert r._escalate("use abacus for this", "you asked") == "abacus answer"
    assert r.calls == ["backup"]


def test_skip_free_goes_straight_to_abacus():
    r = Stub(free="free answer", backup="abacus answer")
    assert r._escalate("remember what we said", "memory question", skip_free=True) == "abacus answer"
    assert r.calls == ["backup"]


def test_no_stronger_brain_available_answers_locally():
    r = Stub(free=None, backup=None)
    assert r._escalate("anything", "nothing left").startswith("local:")
    assert r.calls == ["local"]


def test_local_only_mode_never_escalates(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    brainmode.set_mode("local")
    try:
        r = Stub(free="free answer", backup="abacus answer")
        assert r._can_escalate() is False
        assert r._has_escalation() is False
    finally:
        brainmode.set_mode("auto")


def test_split_routing_is_off_for_a_cloud_main_brain():
    r = Stub(free="free answer", cloud=True)
    assert r._split_free("what's the weather") is None
    assert r.calls == []


def test_split_routing_sends_general_requests_to_the_free_tier(monkeypatch):
    monkeypatch.setattr(split, "enabled", lambda: True)
    monkeypatch.setattr(split, "wants_free", lambda t: True)
    r = Stub(free="free answer")
    assert r._split_free("what's the weather") == "free answer"
    assert r.calls == ["free"]


def test_split_routing_keeps_private_requests_local(monkeypatch):
    monkeypatch.setattr(split, "enabled", lambda: True)
    monkeypatch.setattr(split, "wants_free", lambda t: False)
    r = Stub(free="free answer")
    assert r._split_free("my bank password") is None
    assert r.calls == []
