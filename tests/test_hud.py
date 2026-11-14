from types import SimpleNamespace

from friday.dashboard import DashboardServer


def test_vague_brain_switch_question_is_answered_not_web_searched(monkeypatch):
    from friday import quick, webcontext
    from friday import tools as T

    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"<{name}>")
    q = "Can you switch the AI models that you're using as a brain right now?"
    out = quick.handle(brain, q)
    assert out and "<list_local_models>" in out and "switch your model to" in out
    assert not webcontext.wants_search(q) if hasattr(webcontext, "wants_search") else True


def test_screen_questions_use_real_reading_and_control_phrases_route(monkeypatch):
    from friday import quick
    from friday import tools as T

    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    import friday.computer  # noqa: F401  (registers the screen/control tools)
    assert quick.handle(brain, "can you see my screen?") == "read_screen_text {}"
    assert quick.handle(brain, "what's on my screen") == "read_screen_text {}"
    assert quick.handle(brain, "what are my open shop jobs") == "list_jobs {}"
    assert quick.handle(brain, "which models do you have") == "list_local_models {}"
    assert "calendar" in quick.handle(brain, "what can you do")
    assert quick.handle(brain, "turn on computer control for 15 minutes") == "allow_computer_control {'minutes': 15}"
    assert quick.handle(brain, "stop computer control") == "stop_computer_control {}"
    import friday.routines  # noqa: F401
    assert quick.handle(brain, "give me the daily digest") == "daily_digest {}"


def test_own_data_questions_go_to_real_tools_and_unbacked_answers_are_caught(monkeypatch):
    from friday import llm, quick
    from friday import tools as T

    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: name)
    for q, tool in [("what are my notes", "read_notes"), ("what are my reminders", "list_reminders"),
                    ("how many timers do I have running", "list_reminders"), ("what are my parts reminders", "list_parts_reminders"),
                    ("what do you remember about me", "list_facts"), ("do I have any new emails", "check_email")]:
        assert quick.handle(brain, q) == tool, q
    for q in ("what texts did I get today", "who is my customer for the ZX6R", "what's my phone number", "what's in my downloads folder"):
        assert llm.data_unbacked(q, []), q
        assert not llm.data_unbacked(q, ["search_memory"]), q
    assert not llm.data_unbacked("what's the capital of Peru", []) and not llm.data_unbacked("what's the weather", [])


def test_read_texts_and_list_folder(monkeypatch, tmp_path):
    from friday import quick, sms
    from friday import tools as T

    # folder listing: newest first, sizes, refuses paths outside the home folder
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "a.txt").write_text("hi")
    out = T.dispatch("list_folder", {"folder": "Downloads"}, T.ToolContext())
    assert "a.txt" in out and "1 item" in out
    assert "only list folders inside" in T.dispatch("list_folder", {"folder": "C:\\Windows"}, T.ToolContext())
    # texts: not configured -> honest message; configured -> formatted rows
    monkeypatch.setattr(sms.SmsLink, "configured", staticmethod(lambda: False))
    assert "isn't set up" in sms.read_texts(T.ToolContext())
    monkeypatch.setattr(sms.SmsLink, "configured", staticmethod(lambda: True))
    link = sms.get_link()
    monkeypatch.setattr(type(link), "owner", property(lambda self: "+16475550198"))
    monkeypatch.setattr(link, "number_id", lambda: "PN1")
    monkeypatch.setattr(link, "_api", lambda *a, **k: {"data": [
        {"createdAt": "2026-11-13T10:00:00Z", "direction": "incoming", "text": "Is my bike ready?"}]})
    assert "Is my bike ready?" in sms.read_texts(T.ToolContext()) and "from them" in sms.read_texts(T.ToolContext())
    # routing
    calls = []
    brain = type("B", (), {"ctx": T.ToolContext(), "on_tool": lambda s, *a: calls.append(a[0]), "on_trace": lambda *a: None})()
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    assert quick.handle(brain, "what's in my downloads folder") == "list_folder {'folder': 'Downloads'}"
    assert quick.handle(brain, "what texts did I get today") == "read_texts {}"
    assert quick.handle(brain, "check my texts") == "read_texts {}"


def test_run_ledger_explains_how_the_last_run_ended(tmp_path):
    import json

    from friday import runlog

    assert runlog.describe_previous({}) == ""                                    # first ever run
    assert runlog.describe_previous({"clean": True, "reason": "restart"}) == ""  # clean exit: nothing to report
    killed = runlog.describe_previous({"clean": False, "heartbeat": 1_700_000_000})
    assert "killed from outside" in killed
    assert "console window was closed" in runlog.describe_previous(
        {"clean": False, "heartbeat": 1_700_000_000, "reason": "the console window was closed"})
    note = runlog.start(tmp_path)                                                # nothing before it
    assert note == "" and json.loads((tmp_path / "run_state.json").read_text())["clean"] is False
    runlog.mark_clean("test")
    assert runlog.start(tmp_path) == ""                                          # previous run was clean


def test_ocr_and_window_titles_fail_soft(monkeypatch):
    from friday import screen

    assert screen.ocr_text(b"not a picture") == ""          # bad input must never raise
    fg, others = screen.window_titles()
    assert isinstance(fg, str) and isinstance(others, list)


def test_monthly_claude_cap_is_a_hard_stop_and_digest_reads_the_log(monkeypatch, tmp_path):
    from friday import activity, claude_tiers, routines

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-1234567890")
    monkeypatch.setenv("CLAUDE_TIERS", "true")
    monkeypatch.setenv("CLAUDE_MAX_CALLS_PER_MONTH", "2")
    s = SimpleNamespace(data_dir=tmp_path)
    assert claude_tiers.allowed(s, "haiku")
    claude_tiers.count_use(s, "haiku")
    claude_tiers.count_use(s, "sonnet")
    assert claude_tiers.used_month(s) == 2 and not claude_tiers.allowed(s, "haiku")  # month limit beats the daily cap
    activity.log("error", "something broke")
    text = routines.build_digest(SimpleNamespace(settings=s))
    assert "Jarvis digest" in text and "something broke" in text


def test_lessons_review_flags_overflow_duplicates_and_stale(tmp_path):
    from datetime import datetime, timedelta

    from friday import lessons

    ctx = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    lessons.DATA_DIR = None
    assert "No lessons yet" in lessons.review(ctx)
    lessons.add("Check manuals first", "When asked about a part number always check the manuals folder first", ctx=ctx)
    lessons.add("Manuals first", "When asked about a part number always check the manuals folder first please", ctx=ctx)
    old = lessons.add("Old night lesson", "Prefer short answers on weekends", ctx=ctx, source="evolve")
    items = lessons.load(ctx)
    for i in items:
        if i["id"] == old["id"]:
            i["created"] = (datetime.now() - timedelta(days=60)).isoformat()
    lessons._save(items, ctx)
    for n in range(40):  # far more than the prompt budget allows
        lessons.add(f"Filler lesson {n}", "x" * 150 + f" unique{n}", ctx=ctx)
    out = lessons.review(ctx)
    assert "DON'T FIT" in out and "duplicates" in out and "older than 30 days" in out


def test_sentence_finished_detector_does_not_cut_people_off():
    from friday.voice import sentence_looks_finished as f

    assert f("What's on my calendar today?") and f("Set a timer for ten minutes.")
    assert not f("Set a timer for") and not f("Remind me to call John and") and not f("What is the.")
    assert not f("Well, I was thinking...") and not f("hello") and not f("")
    assert not f("Turn on the lights and the")


def test_hud_numbers_fail_soft_and_have_the_expected_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_TIERS", "false")
    a = SimpleNamespace(brain=SimpleNamespace(last_route="main"), s=SimpleNamespace(model="qwen3", data_dir=tmp_path),
                        ctx=SimpleNamespace(timers=[1, 2], data_dir=tmp_path))
    h = DashboardServer.hud(SimpleNamespace(a=a))
    assert h["brain_mode"] in ("auto", "local", "online") and h["timers"] == 2 and h["model"] == "qwen3"
    assert h["claude"]["on"] is False
    assert h["cpu"] is None or 0 <= h["cpu"] <= 100   # no sensor must never raise
    assert isinstance(h["next_event"], str)


def test_fake_clicks_and_confirmations_are_caught():
    from friday import llm

    fake1 = "KeyPressed: 'y'  \nPressed: 'enter'  \nApproval sent.  \nAction: change_myself\nStatus: pending"
    fake2 = "MouseClick: (x=1020, y=780) - confirmed via screen reader.\nStatus: YES button clicked.\nResult: Change confirmed."
    assert llm.unbacked_claim(fake1, []) and llm.unbacked_claim(fake2, [])
    assert not llm.unbacked_claim(fake2, ["mouse_click", "change_myself"])
    assert not llm.unbacked_claim("I can't click on Claude's screen directly.", [])


def test_brain_split_sends_general_requests_to_free_and_keeps_private_local(monkeypatch):
    from friday import split

    monkeypatch.setenv("BRAIN_SPLIT", "true")
    assert split.enabled()
    for q in ("how tall is Mount Everest", "set a timer for ten minutes", "open calculator", "tell me a joke"):
        assert split.wants_free(q), q
    for q in ("what's in my notes about the ZX6R", "read my texts", "what's on my screen", "what is my password",
              "remember that Mike prefers texts", "check my email inbox"):
        assert not split.wants_free(q), q
    monkeypatch.setenv("BRAIN_SPLIT", "false")
    assert not split.enabled()


def test_limits_meter_rows_and_desktop_route_and_fake_minimize(monkeypatch, tmp_path):
    from friday import llm, quick
    from friday import tools as T
    from friday.dashboard import limits_data

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    brain = SimpleNamespace(free_router=object(), free_used_today=lambda: 7, backup=SimpleNamespace(label="abacus/route-llm"), backup_used_today=lambda: 2)
    s = SimpleNamespace(free_router_max_per_day=300, backup_max_per_day=20, data_dir=tmp_path)
    rows = limits_data(SimpleNamespace(brain=brain, s=s))["rows"]
    names = [r["name"] for r in rows]
    assert "Free cloud (freellmapi)" in names and "Abacus" in names and "Claude" in names
    assert next(r for r in rows if r["name"] == "Abacus")["cap"] == 0 and next(r for r in rows if r["name"] == "Abacus")["used"] == 2
    assert next(r for r in rows if r["name"].startswith("Free"))["used"] == 7
    assert limits_data(SimpleNamespace(brain=None, s=s))["rows"]            # missing parts must not raise
    assert llm.unbacked_claim("Minimizing all windows. All windows minimized. Desktop clean.", []) == "minimized or closed"
    calls = []
    b = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    import friday.computer  # noqa: F401
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    assert quick.handle(b, "minimize everything on my desktop") == "press_keys {'keys': 'win+d'}"
