"""Background projects: several at once, one question at a time, the screen taken in turns, nothing mixed up between them."""
import threading
import time
from types import SimpleNamespace as NS

import pytest

from friday import blackbox, credits, projects, toolpick, updates
from friday import tools as T
from friday.config import load_settings
from friday.credits import CreditGate
from friday.memory import Memory
from test_friday import FakeOpenAI, _resp, clean_env, write_env  # noqa: F401


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def mgr(monkeypatch):
    """A fresh manager whose workers are fake brains the test controls: gates[title].set() lets that project finish."""
    m = projects.Manager()
    monkeypatch.setattr(projects, "MANAGER", m)
    sent, gates, seen = [], {}, {"conflicts": []}
    monkeypatch.setattr(updates, "send", lambda kind, text, **k: sent.append((kind, text, k)))

    class Brain:
        label, last_error = "abacus/route-llm", ""

        def __init__(self, proj, on_tool):
            self.proj, self.on_tool = proj, on_tool

        def _chat_inner(self, text):
            self.on_tool("web_search", {"query": self.proj.title})
            seen["conflicts"].append((self.proj.title, projects.screen_conflict("press_keys")))   # asked from the worker thread
            gates.setdefault(self.proj.title, threading.Event()).wait(5)
            self.on_tool("browse_web", {"url": "https://example.com"})                          # a cancelled project stops here
            return f"Report for {self.proj.title}."

    monkeypatch.setattr(projects, "make_brain", lambda ctx, proj, on_tool, on_trace: Brain(proj, on_tool))
    yield NS(m=m, sent=sent, gates=gates, seen=seen, ctx=NS(memory=None))
    for proj in m.projects:                     # no worker thread may outlive its test and start working in the next one
        proj.cancel.set()
    for ev in gates.values():
        ev.set()
    with m._cv:
        m._cv.notify_all()
    wait_for(lambda: not any(pr.active for pr in m.projects), 3)


def gate(mgr, title):
    return mgr.gates.setdefault(title, threading.Event())


# ------------------------------------------------------------------ parallel, queued, cancelled
def test_projects_run_at_the_same_time_up_to_the_limit_and_the_rest_wait_in_line(mgr, monkeypatch):
    monkeypatch.setenv("PROJECTS_MAX_PARALLEL", "2")
    ps = [mgr.m.start(mgr.ctx, t, f"please do the whole job called {t}") for t in ("Alpha", "Beta", "Gamma")]
    assert wait_for(lambda: [p.status for p in ps] == ["running", "running", "queued"])
    assert wait_for(lambda: "web_search" in ps[0].progress and "web_search" in ps[1].progress)
    assert ps[2].progress == "waiting for a free slot"
    gate(mgr, "Alpha").set()
    assert wait_for(lambda: ps[0].status == "done" and ps[2].status == "running")      # a free slot goes to the next in line
    gate(mgr, "Beta").set()
    gate(mgr, "Gamma").set()
    assert wait_for(lambda: all(p.status == "done" for p in ps))
    assert ps[1].result == "Report for Beta." and ps[1].brain_label == "abacus/route-llm"
    assert wait_for(lambda: len(mgr.sent) == 3)                      # the announcement follows the status a moment later
    assert [k for k, *_ in mgr.sent] == ["project"] * 3 and all("is finished" in t for _, t, _ in mgr.sent)


def test_a_queued_project_is_cancelled_at_once_and_a_running_one_stops_at_its_next_step(mgr, monkeypatch):
    monkeypatch.setenv("PROJECTS_MAX_PARALLEL", "1")
    a = mgr.m.start(mgr.ctx, "Runner", "a long running research job please")
    b = mgr.m.start(mgr.ctx, "Waiter", "another job that has to wait in line")
    assert wait_for(lambda: a.status == "running" and b.status == "queued")
    assert "Cancelling project" in mgr.m.cancel("waiter") and b.status == "cancelled"
    assert "stops at its next step" in mgr.m.cancel(str(a.id))
    gate(mgr, "Runner").set()
    assert wait_for(lambda: a.status == "cancelled")
    assert mgr.sent == [] and "already cancelled" in mgr.m.cancel("Waiter") and "No project matches" in mgr.m.cancel("nothing")


def test_a_failing_project_is_reported_and_never_takes_friday_down(mgr, monkeypatch):
    def boom(ctx, proj, on_tool, on_trace):
        raise RuntimeError("there is no online AI (Abacus) set up to work on projects")

    monkeypatch.setattr(projects, "make_brain", boom)
    p = mgr.m.start(mgr.ctx, "Doomed", "this one cannot even start properly")
    assert wait_for(lambda: p.status == "failed") and "no online AI" in p.error
    assert mgr.sent and mgr.sent[0][2].get("important") and "could not finish" in mgr.sent[0][1]
    with pytest.raises(ValueError):
        mgr.m.start(mgr.ctx, "x", "short")


# ------------------------------------------------------------------ the screen is taken in turns
def test_a_project_that_needs_the_screen_has_it_alone_and_others_keys_are_refused(mgr, monkeypatch):
    monkeypatch.setenv("PROJECTS_MAX_PARALLEL", "3")
    one = mgr.m.start(mgr.ctx, "Screen one", "use the browser to check a supplier page", needs_screen=True)
    two = mgr.m.start(mgr.ctx, "Screen two", "also needs the screen to do something", needs_screen=True)
    side = mgr.m.start(mgr.ctx, "Reading", "just read some pages and write a summary")
    assert wait_for(lambda: one.status == "running" and side.status == "running" and two.status == "waiting for the screen")
    assert wait_for(lambda: {"Screen one", "Reading"} <= {title for title, _ in mgr.seen["conflicts"]})
    assert "the project 'Screen one' is using the screen" in projects.screen_conflict("press_keys")   # the chat is refused
    assert projects.screen_conflict("browser_open") and projects.screen_conflict("web_search") == ""  # but not reading tools
    assert dict(mgr.seen["conflicts"])["Screen one"] == "" and dict(mgr.seen["conflicts"])["Reading"]  # the owner of it may; others may not
    assert "the project 'Screen one'" in T.dispatch("press_keys", {"keys": "win+d"}, T.ToolContext())
    for t in ("Screen one", "Reading", "Screen two"):
        gate(mgr, t).set()
    assert wait_for(lambda: all(p.status == "done" for p in (one, two, side)))
    assert wait_for(lambda: mgr.m.screen_owner is None)               # the screen is handed back a moment after "done"
    assert projects.screen_conflict("press_keys") == ""


def test_background_projects_only_get_the_tools_that_are_safe_for_them():
    import friday.main  # noqa: F401

    plain = projects.allowed_tools(False)
    for ok in ("web_search", "browse_web", "get_time_date", "list_jobs", "remember_fact"):
        assert ok in plain, ok
    for no in ("press_keys", "keyboard_type", "mouse_click", "browser_open", "look_at_screen", "start_project", "change_myself",
               "change_setting", "power_action", "set_brain_mode"):
        assert no not in plain, no
    screen = projects.allowed_tools(True)
    assert "press_keys" in screen and "browser_open" in screen and "start_project" not in screen and "change_myself" not in screen \
        and "power_action" not in screen


def test_the_project_brain_refuses_tools_it_may_not_use_and_has_its_own_conversation(clean_env):
    import friday.main  # noqa: F401

    s = load_settings(write_env(clean_env, "LLM_PROVIDER=abacus\nABACUS_API_KEY=realkey123456\nTOOL_MODE=native\n"))
    main_mem = Memory(s.data_dir)
    main_mem.remember("the shop closes at six")
    main_mem.add("user", "the owner's own chat")
    ctx = T.ToolContext(settings=s, memory=main_mem)
    proj = projects.Project(1, "X", "do something useful for me", False)
    mem = projects.ProjectMemory(main_mem)
    b = projects._brain_class()(s, mem, ctx, client=FakeOpenAI([_resp(content="Done.")]))
    b.allowed = projects.allowed_tools(False)
    b.blocked = frozenset(t.name for t in T.REGISTRY.values()) - frozenset(b.allowed)
    assert b._tool_names("anything") == b.allowed
    assert "not available in this background project" in b._run_tool("press_keys", {"keys": "win+d"})
    assert "needs the screen" in b._run_tool("browser_open", {"url": "https://example.com"})
    assert "not available" in b._run_tool("start_project", {"title": "nested", "request": "a project inside a project"})
    assert "Error: the" not in b._run_tool("get_time_date", {})
    assert mem.history() == [] and mem.facts == ["the shop closes at six"]         # its own history, the owner's facts
    mem.add("user", "project chat")
    mem.remember("supplier is All Balls")
    assert [m["content"] for m in main_mem.messages] == ["the owner's own chat"]      # nothing leaked into the owner's chat
    assert "supplier is All Balls" in main_mem.facts                                  # what it learns is kept


# ------------------------------------------------------------------ things that must not get mixed up between projects
def test_each_project_has_its_own_open_credit_task_and_balance_calibration_is_skipped_when_they_overlap(monkeypatch):
    monkeypatch.setenv("CREDITS_PER_1K_TOKENS", "1")
    g = CreditGate(confirm=lambda p: False, balance_fn=lambda: 1000.0)
    a_open, b_done, out = threading.Event(), threading.Event(), {}

    def a():
        with g.task("project A", "x", 1, 2, "abacus/route-llm") as ok:
            a_open.set()
            b_done.wait(5)                                  # B opened and closed its own task meanwhile
            g.before_call("abacus/route-llm")               # the old single shared ticket made this raise CreditsDenied
            g.charge(10_000, 0)
            out["a"] = (ok, g.ticket["spent"], g.ticket.get("overlap"))

    def b():
        a_open.wait(5)
        with g.task("project B", "x", 1, 2, "abacus/route-llm") as ok:
            g.charge(5_000, 0)
            out["b"] = (ok, g.ticket["spent"])
        b_done.set()

    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert out == {"a": (True, 10.0, True), "b": (True, 5.0)} and g.ticket is None and g.abacus_today() == 15.0
    assert "rate" not in g.data                              # overlapping tasks: nothing to calibrate the rate from


def test_two_projects_record_their_own_steps_in_the_black_box():
    barrier = threading.Barrier(2)

    def work(name):
        blackbox.begin_turn(f"[project] {name}", "abacus/route-llm")
        barrier.wait(5)                                      # both have begun before either acts
        blackbox.record_action("web_search", {"query": name}, f"results for {name}", 0.1, "abacus/route-llm", True)
        blackbox.note("reply", f"done {name}", brain="abacus/route-llm", cloud=True)

    ts = [threading.Thread(target=work, args=(n,)) for n in ("fork seals", "trailer quote")]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    eps = {e["text"]: e for e in blackbox.episodes(24)}
    assert len(eps) == 2
    for name in ("fork seals", "trailer quote"):
        e = eps[f"[project] {name}"]
        assert [s["args"]["query"] for s in e["steps"]] == [name] and e["replies"] == [f"done {name}"]


def test_only_one_permission_question_is_asked_at_a_time_whoever_asks():
    from friday.approvals import ApprovalsMixin

    class Fake(ApprovalsMixin):
        active = peak = 0

        def _confirm_one(self, prompt):
            Fake.active += 1
            Fake.peak = max(Fake.peak, Fake.active)
            time.sleep(0.04)
            Fake.active -= 1
            return True

    f = Fake()
    ts = [threading.Thread(target=f.confirm, args=(f"question {i}",)) for i in range(5)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert Fake.peak == 1


# ------------------------------------------------------------------ the tools, saved state, pages
def test_the_voice_tools_start_list_report_and_cancel_projects(mgr):
    import friday.main  # noqa: F401

    ctx = T.ToolContext()
    out = T.dispatch("start_project", {"title": "Fork seal prices", "request": "Find the three best prices for fork seals for a 2019 Ski-Doo"}, ctx)
    assert out.startswith("Started project #1 'Fork seal prices'") and "tell you when it's done" in out
    assert T.dispatch("start_project", {"title": "x", "request": "tiny"}, ctx).startswith("Error:")
    p = mgr.m.projects[0]
    assert wait_for(lambda: p.status == "running" and p.steps)
    assert "RUNNING" in T.dispatch("list_projects", {}, ctx) and "web_search" in T.dispatch("project_status", {"which": "fork"}, ctx)
    assert "Background projects (the TRUE status" in projects.prompt_section() and "Fork seal prices" in projects.prompt_section()
    gate(mgr, "Fork seal prices").set()
    assert wait_for(lambda: p.status == "done")
    assert "Report: Report for Fork seal prices." in T.dispatch("project_status", {"which": "#1"}, ctx)
    assert T.dispatch("project_status", {"which": "nope"}, ctx) == "No project matches 'nope'."
    assert "already done" in T.dispatch("cancel_project", {"which": "1"}, ctx) and projects.prompt_section() == ""


def test_projects_survive_a_restart_and_one_that_was_running_shows_as_interrupted(mgr):
    done = mgr.m.start(mgr.ctx, "Finished one", "this one finishes before the restart happens")
    gate(mgr, "Finished one").set()
    assert wait_for(lambda: done.status == "done")
    live = mgr.m.start(mgr.ctx, "Still going", "this one is still running when Friday restarts")
    assert wait_for(lambda: live.status == "running")
    fresh = projects.Manager()                               # Friday restarted: a new manager reads data/projects.json
    rows = {r["title"]: r for r in fresh.snapshot()}
    assert rows["Finished one"]["status"] == "done" and rows["Finished one"]["result"] == "Report for Finished one."
    assert rows["Still going"]["status"] == "interrupted" and "restarted" in rows["Still going"]["error"]
    gate(mgr, "Still going").set()


def test_dashboard_and_phone_show_projects_and_the_prompt_and_voice_know_about_them(mgr):
    import friday.main  # noqa: F401
    from friday import phone
    from friday.prompt import build_system_prompt

    p = mgr.m.start(mgr.ctx, "Quote check", "check the quote for the trailer against last month")
    assert wait_for(lambda: p.status == "running")
    st = phone.status(NS(bus=NS(status={}, flags={}), tts=NS(muted=False), a=None, s=NS(conversation_mode=False), mode="always",
                         brain=NS(last_route=""), ctx=NS(timers=[]), last_checks={}))
    assert st["projects"][0]["title"] == "Quote check" and st["projects"][0]["status"] == "running"
    gate(mgr, "Quote check").set()
    assert "BACKGROUND PROJECTS" in build_system_prompt(NS(assistant_name="Jarvis", user_name="Ibbi", default_city="x", provider="abacus",
                                                           base_url="", model="m", backup_provider="none"), None, False)
    for text in ("research fork seal prices in the background", "what are you working on", "cancel that project",
                 "run these two jobs at the same time"):
        assert "start_project" in toolpick.pick(text), text
    html = open(projects.__file__.replace("projects.py", "static/dashboard.html"), encoding="utf-8").read()
    assert 'id="proj-form"' in html and "/api/projects" in html
    page = open(projects.__file__.replace("projects.py", "static/phone.html"), encoding="utf-8").read()
    assert 'id="s-projects"' in page


def test_dashboard_api_starts_lists_and_cancels_projects(mgr, monkeypatch, tmp_path):
    import json
    import urllib.request

    from friday import config
    from friday.main import Assistant

    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=abacus\nABACUS_API_KEY=realkey123456\nINPUT_MODE=text\nTTS_ENGINE=none\nTRAY_ICON=false\n"
                   "DASHBOARD_PORT=0\nDASHBOARD_OPEN_BROWSER=false\nAUTO_CHECK_MINUTES=0\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", env)
    a = Assistant(config.load_settings(override=True), mode="text")
    try:
        def call(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{a.dashboard.port}{path}", headers={"X-Friday-Token": a.dashboard.token,
                                         "Content-Type": "application/json"}, data=None if body is None else json.dumps(body).encode())
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        code, d = call("/api/projects", {"title": "Dashboard job", "request": "a job that was started from the dashboard form"})
        assert code == 200 and d["started"] == 1
        assert wait_for(lambda: call("/api/projects")[1]["projects"][0]["status"] == "running")
        assert call("/api/projects")[1]["max"] >= 1
        assert call("/api/projects", {"title": "x", "request": "tiny"})[0] == 400
        code, d = call("/api/projects", {"cancel": 1})
        assert code == 200 and "Cancelling project #1" in d["message"]
        gate(mgr, "Dashboard job").set()
        assert wait_for(lambda: call("/api/projects")[1]["projects"][0]["status"] == "cancelled")
    finally:
        a.running = False
        if a.dashboard:
            a.dashboard.stop()
