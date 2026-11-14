"""Phase 2: the three concurrency bugs.

12. authorize() held the credit-gate lock across the owner's approval wait, freezing charge()/status().
13. Memory.add mutated the list outside the lock that save() took, so two turns could drop one.
14. apply_settings wrote Settings field by field while the audio and brain threads were reading it."""
from __future__ import annotations

import threading
import time

import pytest

from friday import config, credits
from friday.memory import Memory


# ---------------------------------------------------------------- 12. credit gate lock scope
@pytest.fixture()
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setenv("CREDIT_DAILY_CAP", "1000000")
    monkeypatch.setenv("CREDIT_MONTHLY_CAP", "1000000")
    monkeypatch.delenv("CREDIT_PREAPPROVED_PER_DAY", raising=False)
    return credits.CreditGate(confirm=lambda prompt: True)


def test_charge_is_not_blocked_while_an_approval_is_pending(gate):
    """The owner takes 2s to answer; charge() must still land during that window."""
    started, release = threading.Event(), threading.Event()

    def slow_confirm(prompt):
        started.set()
        release.wait(5)
        return True

    gate.confirm = slow_confirm
    out: list[bool] = []
    th = threading.Thread(target=lambda: out.append(gate.authorize("task", "because", 1, 2)), daemon=True)
    th.start()
    assert started.wait(3), "authorize should have reached the prompt"

    t0 = time.perf_counter()
    gate.charge(100, 50)  # must not wait for the approval
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"charge() blocked for {elapsed:.2f}s behind the approval"
    assert gate.spent_today() > 0

    release.set()
    th.join(timeout=5)
    assert out == [True]


def test_status_is_readable_while_an_approval_is_pending(gate):
    started, release = threading.Event(), threading.Event()

    def slow_confirm(prompt):
        started.set()
        release.wait(5)
        return False

    gate.confirm = slow_confirm
    th = threading.Thread(target=lambda: gate.authorize("task", "because", 1, 2), daemon=True)
    th.start()
    assert started.wait(3)
    t0 = time.perf_counter()
    st = gate.status()
    assert time.perf_counter() - t0 < 1.0
    assert "spent_today" in st
    release.set()
    th.join(timeout=5)


def test_a_declined_approval_is_still_recorded(gate):
    gate.confirm = lambda prompt: False
    assert gate.authorize("task", "because", 1, 2) is False
    assert gate.data["ledger"][-1]["approved"] is False
    assert gate.data["ledger"][-1]["how"] == "you declined"
    assert "declined" in gate.last_denial


def test_an_approved_call_is_recorded_once(gate):
    assert gate.authorize("task", "because", 1, 2) is True
    assert len([e for e in gate.data["ledger"] if e["task"] == "task"]) == 1


def test_the_cap_is_still_enforced_before_asking(gate, monkeypatch):
    monkeypatch.setenv("CREDIT_DAILY_CAP", "1")
    gate.data["days"][credits.datetime.now().strftime("%Y-%m-%d")] = 5.0
    asked: list[str] = []
    gate.confirm = lambda p: asked.append(p) or True
    assert gate.authorize("task", "because", 1, 2) is False
    assert asked == [], "a request over the cap must be refused without bothering the owner"


# ---------------------------------------------------------------- 13. Memory.add lock
def test_concurrent_adds_never_lose_a_turn(tmp_path):
    mem = Memory(tmp_path, max_messages=200)
    n = 60

    def writer(i):
        mem.add("user", f"m{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(mem.messages) == n, f"expected {n} turns, kept {len(mem.messages)}"
    assert len({m["content"] for m in mem.messages}) == n


def test_history_never_starts_with_an_assistant_turn(tmp_path):
    mem = Memory(tmp_path, max_messages=4)
    mem.add("assistant", "hello")
    assert mem.messages == [] or mem.messages[0]["role"] == "user"


def test_the_file_on_disk_matches_memory_after_adds(tmp_path):
    import json

    mem = Memory(tmp_path, max_messages=10)
    mem.add("user", "first")
    mem.add("assistant", "second")
    on_disk = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    assert [m["content"] for m in on_disk["messages"]] == ["first", "second"]


# ---------------------------------------------------------------- 14. atomic settings swap
class FakeAssistant:
    """Just enough of Assistant to exercise apply_settings."""

    def __init__(self, settings):
        self.s = settings
        self.mode = "text"
        self.ui = type("UI", (), {"name": ""})()
        self.refreshed = 0

    def _refresh_agents(self):
        self.refreshed += 1

    def control(self, action):
        return f"did {action}"

    def set_mode(self, mode):
        self.mode = mode
        return f"mode {mode}"


def test_apply_settings_swaps_in_one_update(tmp_path, monkeypatch):
    from friday.main import Assistant

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("ABACUS_API_KEY=k\nASSISTANT_NAME=Old\nDEFAULT_CITY=Toronto\n")
    s = config.load_settings(override=True)
    a = FakeAssistant(s)
    before_id = id(a.s)

    (tmp_path / ".env").write_text("ABACUS_API_KEY=k\nASSISTANT_NAME=New\nDEFAULT_CITY=Oslo\n")
    out = Assistant.apply_settings(a, [])

    assert a.s.assistant_name == "New" and a.s.default_city == "Oslo"
    assert id(a.s) == before_id, "identity must be preserved: ctx/brain/tts hold a reference to this object"
    assert a.ui.name == "New"
    assert out["restart_needed"] is False


def test_apply_settings_does_not_setattr_field_by_field(tmp_path, monkeypatch):
    """Deterministic check for the torn read.

    A reader thread cannot reproduce the tear reliably under the GIL (measured: 112k reads, 0 torn), so assert the
    mechanism instead: the new values must arrive through one __dict__ update, never through per-field setattr calls
    that a concurrent reader could observe halfway through."""
    from friday.main import Assistant

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("ABACUS_API_KEY=k\nASSISTANT_NAME=Old\nDEFAULT_CITY=Toronto\n")
    s = config.load_settings(override=True)

    writes: list[str] = []

    class Counting(config.Settings):
        def __setattr__(self, name, value):
            writes.append(name)
            super().__setattr__(name, value)

    counting = Counting(**{f: getattr(s, f) for f in s.__dataclass_fields__})
    writes.clear()  # the constructor sets every field; only apply_settings matters here
    a = FakeAssistant(counting)
    (tmp_path / ".env").write_text("ABACUS_API_KEY=k\nASSISTANT_NAME=New\nDEFAULT_CITY=Oslo\n")
    Assistant.apply_settings(a, [])

    assert counting.assistant_name == "New" and counting.default_city == "Oslo"
    assert writes == [], f"apply_settings used setattr for {writes}; the swap must be one __dict__ update"
