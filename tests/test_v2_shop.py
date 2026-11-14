"""v2.0: searchable memory and shop helpers (local SQLite only)."""
import pytest

from friday import memory_db, sms
from friday import tools as T
from tests.test_v2 import FakeQuo


def test_memory_search_finds_notes_and_chat():
    memory_db.index("chat", "user: the Ducati Streetfighter needs a valve check next month")
    memory_db.index("note", "order brake pads for the ZX6R")
    out = T.dispatch("search_memory", {"query": "ducati valve"}, T.ToolContext())
    assert "Streetfighter" in out and "brake" not in out
    assert "nothing" in T.dispatch("search_memory", {"query": "harley"}, T.ToolContext())
    assert memory_db.search("brake", kind="chat") == []


def test_add_note_is_searchable():
    T.dispatch("add_note", {"text": "customer Mike prefers texts"}, T.ToolContext())
    assert "Mike" in T.dispatch("search_memory", {"query": "mike"}, T.ToolContext())


def test_jobs_board(monkeypatch):
    ctx = T.ToolContext()
    assert "Added job 1" in T.dispatch("add_job", {"customer": "John Smith", "vehicle": "2017 Kawasaki ZX6R",
                                                   "work": "chain and sprockets", "phone": "905-555-7777"}, ctx)
    T.dispatch("add_job", {"customer": "Ann Lee", "vehicle": "Sea-Doo Spark", "work": "winterize"}, ctx)
    assert "waiting for parts" in T.dispatch("update_job", {"job": "smith", "status": "parts",
                                                            "note": "sprocket on order"}, ctx)
    assert "Unknown status" in T.dispatch("update_job", {"job": "1", "status": "flying"}, ctx)
    assert "couldn't find" in T.dispatch("update_job", {"job": "nobody"}, ctx)
    board = T.dispatch("list_jobs", {}, ctx)
    assert "John Smith" in board and "Ann Lee" in board
    from friday.shop import open_jobs_summary
    assert "1 booked" in open_jobs_summary() and "1 waiting for parts" in open_jobs_summary()
    assert "sprocket" in T.dispatch("search_memory", {"query": "sprocket", "kind": "job"}, ctx)


def test_parts_reminder_and_quote(monkeypatch):
    from friday.env import data_dir
    from friday.shop import due_parts_reminders

    ctx = T.ToolContext()
    T.dispatch("add_job", {"customer": "John", "vehicle": "GSX-R750", "work": "carb clean"}, ctx)
    T.dispatch("add_parts_reminder", {"part": "carb kit", "job": "john"}, ctx)
    T.dispatch("add_parts_reminder", {"part": "tires", "days": 5}, ctx)
    assert due_parts_reminders(mark_done=True) == ["carb kit (job 1: John's GSX-R750)"]
    assert due_parts_reminders() == []
    assert "tires" in T.dispatch("list_parts_reminders", {}, ctx)

    monkeypatch.setenv("LABOUR_RATE", "100")
    out = T.dispatch("draft_quote", {"customer": "John", "vehicle": "GSX-R750", "work": "carb clean",
                                     "labour_hours": 2, "parts": [{"name": "carb kit", "price": 50, "qty": 1}]}, ctx)
    assert "$282.50" in out  # (200 + 50) * 1.13
    assert list((data_dir() / "quotes").glob("*.txt"))


def test_bike_ready_text_asks_first_and_sends(monkeypatch):
    for k, v in {"SMS_ENABLED": "true", "QUO_API_KEY": "test", "OWNER_PHONE": "9055551234",
                 "FRIDAY_SMS_NUMBER": "9055550000", "AUTONOMY_LEVEL": "2"}.items():
        monkeypatch.setenv(k, v)
    fake = FakeQuo()
    monkeypatch.setattr(sms, "LINK", sms.SmsLink(opener=fake))
    T.dispatch("add_job", {"customer": "John Smith", "vehicle": "ZX6R", "work": "tires", "phone": "905-555-7777"},
               T.ToolContext())
    asked = []
    no = T.ToolContext(confirm=lambda p: asked.append(p) or False)
    assert "Cancelled" in T.dispatch("text_customer_bike_ready", {"job": "john"}, no)
    assert asked and not fake.sent  # "send" always asks, even at autonomy level 2
    yes = T.ToolContext(confirm=lambda p: True)
    out = T.dispatch("text_customer_bike_ready", {"job": "john", "extra": "Total $120."}, yes)
    assert "marked ready" in out
    assert fake.sent[-1]["to"] == ["+19055557777"]
    assert "Hi John, your ZX6R is ready" in fake.sent[-1]["content"]
