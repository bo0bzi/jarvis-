from types import SimpleNamespace

from friday import livefacts, quick
from friday import tools as T


def test_anydesk_id_is_read_from_config_and_spaced(tmp_path, monkeypatch):
    (tmp_path / "AnyDesk").mkdir()
    (tmp_path / "AnyDesk" / "system.conf").write_text("ad.anynet.id=123456789\nother=1\n", encoding="utf-8")
    monkeypatch.setenv("ProgramData", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "nothing"))
    assert livefacts.anydesk_id() == "123 456 789"
    assert "AnyDesk ID: 123 456 789" in livefacts.remote_desktop_id(None)


def test_no_remote_tool_gives_an_honest_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert "couldn't find" in livefacts.remote_desktop_id(None)


def test_quick_routes_anydesk_question_to_the_tool(monkeypatch):
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    monkeypatch.setattr(livefacts, "anydesk_id", lambda: "111 222 333")
    assert "111 222 333" in quick.handle(brain, "what is my anydesk id")
    assert calls == ["remote_desktop_id"]
    assert quick.handle(brain, "open anydesk") is None


def test_installed_and_memory_questions_use_tools_not_guesses_or_the_web(monkeypatch):
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append((a[0], a[1])), on_trace=lambda *a: None)
    quick.handle(brain, "do i have anydesk installed")
    quick.handle(brain, "what are the biggest programs using memory right now")
    assert calls == [("installed_apps", {"contains": "anydesk"}), ("top_processes", {})]
    from friday import webcontext
    assert not webcontext.wants_search("what are the biggest programs using memory right now")


def test_read_only_tools_run():
    assert "free of" in livefacts.disk_report(None)
    assert "MB" in livefacts.top_processes(None, 3)
    assert "Local IP" in livefacts.network_info(None)
    assert "installed" in livefacts.installed_apps(None, "") or "couldn't" in livefacts.installed_apps(None, "")
    assert "No installed program" in livefacts.installed_apps(None, "zzzz-not-a-program-zzzz")
