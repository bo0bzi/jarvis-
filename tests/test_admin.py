import time
from types import SimpleNamespace

from friday import admin, livefacts


def test_watch_notifies_once_when_the_admin_box_appears(monkeypatch):
    monkeypatch.setattr(livefacts, "anydesk_id", lambda: "1 234 567 890")
    got = []
    with admin.watch_uac("install Foo", got.append, poll=0.02, active=lambda: True):
        time.sleep(0.25)
    assert len(got) == 1  # not once per poll
    assert "install Foo" in got[0] and "1 234 567 890" in got[0] and "administrator" in got[0]


def test_no_notice_when_no_box(monkeypatch):
    got = []
    with admin.watch_uac("x", got.append, poll=0.02, active=lambda: False):
        time.sleep(0.1)
    assert got == []


def test_notify_owner_speaks_and_texts(monkeypatch):
    said, pushed = [], []
    monkeypatch.setattr("friday.notify.push", lambda title, body="", **k: pushed.append((title, body)))
    admin.notify_owner(SimpleNamespace(speak=said.append), "hello")
    assert said == ["hello"] and pushed == [("Jarvis needs you", "hello")]
