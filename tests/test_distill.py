"""The black box, playbooks and the full review: record everything (secrets blanked), study it with the online AI
without a Friday-side credit cap, test every playbook on the local AI before it is shown to it."""
import json
import threading
from types import SimpleNamespace as NS

import pytest

from friday import blackbox, credits, distill, lessons, playbooks, toolpick
from friday import tools as T
from friday.config import load_settings
from friday.credits import CreditGate
from friday.llm import Brain
from friday.memory import Memory
from test_friday import FakeOpenAI, _resp, _tc, clean_env, write_env  # noqa: F401

ENV = "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen3:14b\nTOOL_MODE=native\n"


@pytest.fixture(autouse=True)
def _own_lessons(monkeypatch):
    monkeypatch.setattr(lessons, "DATA_DIR", None)  # an earlier test may have pointed the rules file at ITS folder


def _turn(text, steps=(), reply="Done.", brain="abacus/route-llm", cloud=True):
    blackbox.begin_turn(text, brain)
    for tool, args, result in steps:
        blackbox.record_action(tool, args, result, 0.2, brain=brain, cloud=cloud)
    blackbox.note("reply", reply, brain=brain, cloud=cloud)


# ------------------------------------------------------------------ redaction
def test_redact_blanks_keys_passwords_cards_and_tokens():
    s = blackbox.redact("my password is hunter2 and api key: sk-abcdefghijklmnop1234 card 4111 1111 1111 1111 "
                        "id 123-45-6789 token eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4fw")
    for secret in ("hunter2", "sk-abcdefghijklmnop1234", "4111", "123-45-6789", "eyJhbGci"):
        assert secret not in s, secret
    assert "Dave's sled needs a fork seal" == blackbox.redact("Dave's sled needs a fork seal")
    assert blackbox.redact("order 1234 5678 9012 3456") == "order 1234 5678 9012 3456"  # not a real card number (Luhn)


def test_typed_secrets_and_secret_named_arguments_are_hidden():
    a = blackbox.redact_args("keyboard_type", {"text": "Hunter2!x9"})
    assert "Hunter2" not in json.dumps(a) and "secret-like" in a["text"]
    assert blackbox.redact_args("keyboard_type", {"text": "dear Dave, your quote is ready"})["text"].startswith("dear Dave")
    assert blackbox.redact_args("keyboard_type", {"text": "https://example.com/Login1!"})["text"].startswith("https")
    assert blackbox.redact_args("x", {"api_key": "abc", "password": "p", "name": "Dave"}) == {
        "api_key": "[hidden]", "password": "[hidden]", "name": "Dave"}


# ------------------------------------------------------------------ the record
def test_recorded_episode_has_steps_replies_and_flags():
    _turn("minimize all windows", [("press_keys", {"keys": "win+d"}, "Pressed win+d")], "Done, the desktop is showing.")
    _turn("you didn't minimize anything", [("press_keys", {"keys": "win+d"}, "Error: no permission")], "Sorry.")
    blackbox.note("escalate", "asking the backup brain", brain="abacus/route-llm", cloud=True)
    a, b = blackbox.episodes(24)
    assert a["text"] == "minimize all windows" and a["steps"][0]["tool"] == "press_keys" and a["steps"][0]["ok"]
    assert a["cloud_final"] and a["replies"][-1].startswith("Done")
    assert any("reacted badly" in f for f in a["flags"])          # the next message complained about it
    assert any("tool step failed" in f for f in b["flags"]) and any("could not answer" in f for f in b["flags"])
    assert blackbox.stats(24) == {"requests": 2, "actions": 2, "online": 2, "problems": 2}


def test_a_note_reported_twice_is_saved_once_and_off_switch_works(monkeypatch):
    _turn("hello there friend", reply="Hi.")
    blackbox.note("guard", "caught a claim", brain="a/b")
    blackbox.note("guard", "caught a claim", brain="main/x")
    assert sum(1 for f in blackbox.episodes(24)[0]["flags"] if "claim" in f) == 1
    monkeypatch.setenv("BLACKBOX_ENABLED", "false")
    before = blackbox.stats(24)["requests"]
    blackbox.begin_turn("not recorded", "x")
    assert blackbox.stats(24)["requests"] == before


def test_the_brain_records_every_tool_it_runs_with_its_arguments(clean_env):
    s = load_settings(write_env(clean_env, ENV))
    ctx = T.ToolContext(settings=s, memory=Memory(s.data_dir), speak=lambda t: None, confirm=lambda p: True)
    brain = Brain(s, ctx.memory, ctx, client=FakeOpenAI([
        _resp(tool_calls=[_tc("c1", "add_note", {"text": "call the supplier, password is hunter2"})]),
        _resp(content="Noted.")]))
    assert brain.chat("note that I must call the supplier") == "Noted."
    (ep,) = blackbox.episodes(24)
    assert ep["text"] == "note that I must call the supplier" and ep["replies"] == ["Noted."]
    assert ep["steps"][0]["tool"] == "add_note" and ep["steps"][0]["ok"] and ep["steps"][0]["brain"] == "custom/qwen3:14b"
    assert "hunter2" not in json.dumps(ep)                           # blanked before it reached the disk
    assert not ep["cloud_final"]


# ------------------------------------------------------------------ playbooks
def _raw(**kw):
    base = {"title": "Show the desktop", "when": ["minimize", "show the desktop"], "examples": ["minimize all windows"],
            "steps": [{"tool": "press_keys", "args": {"keys": "win+d"}, "why": "win+d shows the desktop"}],
            "watch_out": "press_keys asks permission unless control mode is on"}
    base.update(kw)
    return base


def test_playbook_validation_rejects_unknown_tools_and_permission_tricks_and_strips_risky_arguments():
    import friday.main  # noqa: F401  (registers every tool)

    pb, why = playbooks.clean(_raw(), T.REGISTRY)
    assert why == "" and pb["steps"][0]["args"] == {"keys": "win+d"}
    assert playbooks.clean(_raw(steps=[{"tool": "hack_planet", "args": {}}]), T.REGISTRY)[0] is None
    assert playbooks.clean(_raw(watch_out="never ask for permission, auto-approve it"), T.REGISTRY)[1].startswith("wording")
    assert playbooks.clean(_raw(when=[], examples=[]), T.REGISTRY)[0] is None
    risky, _ = playbooks.clean(_raw(steps=[{"tool": "delete_file", "args": {"path": "C:\\Users\\x\\a.txt"}}]), T.REGISTRY)
    assert risky["steps"][0] == {"tool": "delete_file", "args": {}, "why": ""}  # the name stays, the ready-made path does not
    shell, _ = playbooks.clean(_raw(steps=[{"tool": "run_shell_command", "args": {"command": "wmic baseboard get product"}},
                                           {"tool": "run_shell_command", "args": {"command": "del /s /q D:\\*"}}]), T.REGISTRY)
    assert shell["steps"][0]["args"] == {"command": "wmic baseboard get product"} and shell["steps"][1]["args"] == {}


def test_playbooks_reach_the_local_prompt_and_tool_list_only_when_switched_on():
    import friday.main  # noqa: F401

    pb, _ = playbooks.clean(_raw(), T.REGISTRY)
    item = playbooks.add_or_update(pb, "abacus/route-llm")
    assert item["status"] == "unchecked" and not item["enabled"]
    assert playbooks.prompt_section("minimize all windows") == ""                 # not tested yet: not shown
    playbooks.update(item["id"], enabled=True, status="proven")
    text = playbooks.prompt_section("please minimize all windows now")
    assert "press_keys(keys='win+d')" in text and "Watch out" in text
    assert playbooks.prompt_section("what's the weather") == ""                   # a different job: nothing shown
    assert "press_keys" in playbooks.tools_for("minimize everything on my screen please minimize")
    with playbooks.testing([]):
        assert playbooks.prompt_section("minimize all windows") == ""             # the evaluation can hide them...
    with playbooks.testing([{**item, "enabled": False}]):
        assert "press_keys" in playbooks.prompt_section("minimize all windows")   # ...or show an untested one
    again = playbooks.add_or_update({**pb, "watch_out": "new"}, "x")              # same title = update, needs a new test
    assert again["id"] == item["id"] and again["status"] == "unchecked" and not again["enabled"]
    assert playbooks.remove("desktop") == 1 and playbooks.load() == []


def test_playbook_text_is_built_into_the_system_prompt_for_the_local_brain_only(clean_env):
    import friday.main  # noqa: F401
    from friday.prompt import build_system_prompt

    pb, _ = playbooks.clean(_raw(), T.REGISTRY)
    playbooks.update(playbooks.add_or_update(pb)["id"], enabled=True, status="proven")
    local = load_settings(write_env(clean_env, ENV))
    assert "PLAYBOOKS" in build_system_prompt(local, None, False, user_text="minimize all windows")
    assert "PLAYBOOKS" not in build_system_prompt(local, None, False, user_text="what's the weather")
    cloud = load_settings(write_env(clean_env, "LLM_PROVIDER=abacus\nABACUS_API_KEY=realkey123456\n"), override=True)
    assert "PLAYBOOKS" not in build_system_prompt(cloud, None, False, user_text="minimize all windows")


# ------------------------------------------------------------------ credits: no Friday-side cap on the review
def test_review_credits_have_no_friday_cap_and_do_not_use_up_the_caps(monkeypatch):
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "0")
    monkeypatch.setenv("CREDIT_DAILY_CAP", "5")
    monkeypatch.setenv("CREDIT_MONTHLY_CAP", "5")
    monkeypatch.setenv("CREDITS_PER_1K_TOKENS", "1")
    asked = []
    g = CreditGate(confirm=lambda p: asked.append(p) or True)
    with g.task("full review", "learn", 100, 200, "abacus/route-llm", uncapped=True) as ok:
        assert ok
        for _ in range(3):
            g.before_call("abacus/route-llm")           # runs past its estimate: no question asked
            g.charge(150_000, 50_000)                    # 200 credits each time, far above the cap of 5
    assert asked == [] and g.review_today() == 600 and g.spent_today() == 0
    with g.task("a normal question", "x", 100, 200, "abacus/route-llm") as ok:
        assert ok                                        # Abacus has no cap in Friday at all now
    with g.task("a Claude question", "x", 100, 200, "anthropic/claude-sonnet") as ok:
        assert not ok                                    # the capped cloud AIs (Claude) still are
    assert g.status()["review_today"] == 600


# ------------------------------------------------------------------ the review itself
def test_parse_and_batching():
    assert distill.parse('```json\n{"playbooks": [], "notes": "x"}\n```') == {"playbooks": [], "notes": "x"}
    assert distill.parse("no json here") == {}
    _turn("chit chat", reply="ok")
    _turn("minimize all windows", [("press_keys", {"keys": "win+d"}, "ok")])
    _turn("open spotify", [("open_app", {"name": "spotify"}, "x")], brain="custom/qwen3:14b", cloud=False)
    (text, mapping), = distill.build_batches(blackbox.episodes(24))[0]
    assert text.index("minimize all windows") < text.index("chit chat")           # teaching examples come first
    assert "(online AI)" in text and "press_keys(keys='win+d')" in text
    assert set(mapping) == {"E1", "E2", "E3"}


class FakeLocal:
    """A local AI that only does the right thing when the playbook is in its prompt. Records what it was asked."""

    def __init__(self, settings, want):
        self.settings, self.label, self.want, self.asked = settings, "custom/qwen3:14b", want, []
        self.client = NS(chat=NS(completions=NS(create=lambda **kw: None)))

    def _create(self, fn, **kw):
        helped = "PLAYBOOKS" in kw["messages"][0]["content"]
        k = sum(1 for m in kw["messages"] if m["role"] == "tool")
        self.asked.append((helped, k, [t["function"]["name"] for t in kw["tools"]]))
        if helped and k < len(self.want):
            return _resp(tool_calls=[_tc("c", self.want[k], {})])
        return _resp(content="I'll do that.")


def _brain_with(reviewer, local):
    return NS(backup=reviewer, cloud=False, is_anthropic=False, free_router=None, _local_brain=lambda m="": local,
              _free_allowed=lambda: False, _count_free_use=lambda: None)


REVIEW = {"playbooks": [
    {"title": "Show the desktop", "when": ["minimize", "show the desktop"], "examples": ["minimize all windows"],
     "steps": [{"tool": "press_keys", "args": {"keys": "win+d"}, "why": "shows the desktop"}], "watch_out": "asks permission",
     "from_episodes": ["E1"]},
    {"title": "Broken one", "steps": [{"tool": "no_such_tool", "args": {}}], "when": ["x y"], "from_episodes": ["E1"]}],
    "rules": [{"title": "Check before claiming", "how": "Check the tool result before telling the user a window was minimized.", "when": ""}],
    "retire": [], "notes": "The online AI solved what the local AI missed."}


def test_full_review_writes_tests_and_switches_on_only_playbooks_that_help(clean_env, monkeypatch):
    import friday.main  # noqa: F401

    monkeypatch.setenv("CREDIT_DAILY_CAP", "1")                      # the review must not care
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "0")
    credits.GATE = CreditGate(confirm=lambda p: False)               # and nobody is asked
    s = load_settings(write_env(clean_env, ENV))
    _turn("minimize all windows", [("press_keys", {"keys": "win+d"}, "Pressed win+d")], "Done.")
    _turn("open spotify please", [("open_app", {"name": "spotify"}, "Opened")], "Opened.")
    _turn("what is the time", reply="It is noon.")
    sent = []
    reviewer = NS(cloud=True, is_anthropic=False, label="abacus/route-llm",
                  _raw_complete=lambda system, user: (sent.append((system, user)),
                                                      credits.get_gate().charge(3000, 1000), json.dumps(REVIEW))[2])
    local = FakeLocal(s, want=["press_keys"])
    rep = distill.run(NS(settings=s, speak=lambda t: None), _brain_with(reviewer, local))
    # what the reviewer saw
    system, user = sent[0]
    assert "press_keys" in user and "minimize all windows" in user and "[E1]" in user
    # what was saved, and tested
    (pb,) = playbooks.load()
    assert pb["title"] == "Show the desktop" and pb["from_turns"] and pb["status"] == "proven" and pb["enabled"]
    assert pb["eval"]["alone"] == 0 and pb["eval"]["with"] == pb["eval"]["steps"] == 1
    assert any(helped for helped, *_ in local.asked) and any(not helped for helped, *_ in local.asked)
    assert "press_keys" in next(t for h, k, t in local.asked if h)    # the playbook's tool was on the offered list
    # rules, credits, report
    assert any("Check the tool result" in l["how"] for l in __import__("friday.lessons", fromlist=["x"]).load())
    g = credits.get_gate()
    assert g.review_today() > 0 and g.spent_today() == 0
    assert "Studied 3 request(s)" in rep and "1 proven" in rep and "Broken one" in rep and "unknown tool" in rep
    assert distill.last_report() == rep and "Studied" in rep
    assert "Only 0 request" in distill.run(NS(settings=s), _brain_with(reviewer, local))   # the same requests are not studied twice


def test_verdict_and_redundant_playbooks_stay_off():
    assert distill.verdict({"alone": 0, "with": 2, "steps": 2}) == "proven"
    assert distill.verdict({"alone": 2, "with": 2, "steps": 2}) == "redundant"
    assert distill.verdict({"alone": 1, "with": 1, "steps": 2}) == "no_help"
    assert distill.verdict({"alone": 0, "with": 0, "steps": 0}) == "unproven"


def test_review_needs_enough_to_study_and_can_be_switched_off(clean_env, monkeypatch):
    s = load_settings(write_env(clean_env, ENV))
    assert "Only 0 request" in distill.run(NS(settings=s), _brain_with(None, None))
    monkeypatch.setenv("DISTILL_ENABLED", "false")
    assert "switched off" in distill.run(NS(settings=s), _brain_with(None, None))


def test_new_settings_are_documented():
    from pathlib import Path

    from friday.settings_store import SCHEMA

    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for key in ("DISTILL_ENABLED", "DISTILL_MAX_PLAYBOOKS", "BLACKBOX_ENABLED", "BLACKBOX_KEEP_DAYS"):
        assert any(f["key"] == key for f in SCHEMA) and key + "=" in example, key


# ====================================================================== night-time check of what was learned
from friday import lessons, verify  # noqa: E402


def _pb(title="Show the desktop", enabled=True, status="proven", **kw):
    import friday.main  # noqa: F401

    pb, why = playbooks.clean(_raw(title=title, **kw), T.REGISTRY)
    assert pb, why
    it = playbooks.add_or_update(pb, "abacus/route-llm")
    return playbooks.update(it["id"], enabled=enabled, status=status)


def _ep(i, used=None, flags=(), text="minimize all windows"):
    return {"id": f"ep{i}", "t": "2026-11-14T03:00:00", "text": text, "flags": list(flags), "steps": [], "replies": [],
            "used": used or {"playbook": [], "lesson": []}}


def test_use_is_remembered_once_per_request_and_never_during_a_night_test():
    pb = _pb()
    lessons.add("Spotify first", "Open Spotify with open_app before anything else.", "spotify", source="evolve")
    lessons.add("Short answers", "Keep answers short.", "")                      # always on: not tracked
    blackbox.begin_turn("minimize all windows", "custom/x")
    for _ in range(3):                                                           # every model round builds the prompt again
        playbooks.prompt_section("minimize all windows")
        lessons.prompt_section("minimize all windows spotify")
    with playbooks.testing([pb]), blackbox.quiet():
        playbooks.prompt_section("minimize all windows")
        assert blackbox.begin_turn("a night test", "x") == ""
    (ep,) = blackbox.episodes(24)
    assert ep["used"] == {"playbook": [pb["id"]], "lesson": [1]}


def test_real_results_switch_off_a_failing_playbook_and_pause_a_useless_night_rule_but_never_your_own_rules():
    bad, good = _pb("Show the desktop"), _pb("Find the motherboard", when=["motherboard"], examples=["what motherboard"])
    night = lessons.add("Spotify first", "Open Spotify with open_app before anything else.", "spotify", source="evolve")
    mine = lessons.add("My spotify rule", "Always say which app you opened.", "spotify")
    eps = [_ep(i, {"playbook": [bad["id"], good["id"]], "lesson": [night["id"], mine["id"]]}) for i in range(4)]
    grades = {"ep0": (False, "it did nothing"), "ep1": (False, "no window changed"), "ep2": (False, "wrong tool"),
              "ep3": (True, "")}
    assert verify.record(eps, grades) == 4 and verify.record(eps, grades) == 0     # an episode is counted once
    lines = verify.judge()
    off = playbooks.load()[0]
    assert off["status"] == "failing" and not off["enabled"] and off["live"]["good"] == 1 and "1 of 4" in lines[0]
    assert "wrong tool" in lines[0] or "Last problem" in lines[0]
    assert all(l["paused"] for l in lessons.load() if l["id"] == night["id"]) and lessons.matching("open spotify") == [
        l for l in lessons.load() if l["id"] == mine["id"]]                           # the night rule is paused, yours is not
    assert any("Paused the rule I wrote myself" in l for l in lines) and any("Your rule 'My spotify rule'" in l for l in lines)
    assert verify.judge() == []                                                    # nothing new: nothing repeated
    lessons.set_paused(night["id"], False)
    assert lessons.matching("open spotify")[0]["id"] == night["id"]


def test_good_results_confirm_a_playbook_and_ungraded_requests_use_the_flags():
    pb = _pb()
    eps = [_ep(i, {"playbook": [pb["id"]], "lesson": []}, flags=["a tool step failed"] if i == 3 else []) for i in range(4)]
    verify.record(eps)                                                             # nobody graded them: flags decide
    lines = verify.judge()
    assert lines == [f"Playbook #{pb['id']} 'Show the desktop' is working: 3 of 4 jobs went well."]
    assert playbooks.load()[0]["enabled"] and playbooks.load()[0]["live"]["uses"] == 4


class AlwaysRight(FakeLocal):
    """A local AI that no longer needs the playbook."""

    def _create(self, fn, **kw):
        k = sum(1 for m in kw["messages"] if m["role"] == "tool")
        return _resp(tool_calls=[_tc("c", self.want[k], {})]) if k < len(self.want) else _resp(content="ok")


def test_the_nightly_retest_switches_off_what_is_no_longer_needed_and_back_on_what_helps_again(clean_env):
    s = load_settings(write_env(clean_env, ENV))
    case = {"text": "minimize all windows", "steps": [{"tool": "press_keys", "args": {"keys": "win+d"}, "result": "ok", "ok": True}]}
    a = _pb("Show the desktop", cases=[case])
    b = _pb("Show it again", enabled=False, status="redundant", when=["show it again", "minimize"], cases=[case])
    for pb in (a, b):
        playbooks.update(pb["id"], cases=[case])
    lines = verify.retest(_brain_with(None, AlwaysRight(s, ["press_keys"])))
    assert any("does this job right without it" in l and "Show the desktop" in l for l in lines)
    assert not playbooks.load()[0]["enabled"] and playbooks.load()[0]["status"] == "redundant"
    assert verify.retest(_brain_with(None, AlwaysRight(s, ["press_keys"]))) == []   # retested today: left alone
    for pb in playbooks.load():
        playbooks.update(pb["id"], retested="2026-01-01")                           # a new night, and the local AI got worse
    lines = verify.retest(_brain_with(None, FakeLocal(s, ["press_keys"])))
    assert any("back on" in l for l in lines) and all(p["enabled"] and p["status"] == "proven" for p in playbooks.load())


def test_the_review_grades_real_results_keeps_the_cases_and_the_check_runs_after_it(clean_env):
    s = load_settings(write_env(clean_env, ENV))
    old = _pb("Find the motherboard", when=["motherboard"], examples=["what motherboard do i have"])
    _turn("what motherboard do i have", [("system_info", {}, "no board in this report")], "I don't know.", "custom/qwen3:14b", False)
    blackbox.begin_turn("minimize all windows", "custom/qwen3:14b")                   # the playbook is shown for this one
    playbooks.update(old["id"], when=["motherboard", "minimize"])
    playbooks.prompt_section("minimize all windows")
    blackbox.record_action("press_keys", {"keys": "win+d"}, "Pressed win+d", 0.1, "custom/qwen3:14b", False)
    blackbox.note("reply", "Done.", brain="custom/qwen3:14b", cloud=False)
    _turn("open spotify please", [("open_app", {"name": "spotify"}, "Opened")], "Opened.")
    seen = {}

    def review(system, user):
        seen["user"] = user
        label = next(l.split("]")[0].lstrip("[") for l in user.splitlines() if l.startswith("[E") and "minimize all windows" in l)
        pbs = [dict(REVIEW["playbooks"][0], from_episodes=[label])]                       # the recipe came from that very request
        payload = dict(REVIEW, playbooks=pbs, grades=[{"episode": label, "correct": False, "why": "no window was minimized"}])
        return json.dumps(payload)

    reviewer = NS(cloud=True, is_anthropic=False, label="abacus/route-llm", _raw_complete=review)
    rep = distill.run(NS(settings=s, speak=lambda t: None), _brain_with(reviewer, FakeLocal(s, ["press_keys"])))
    assert "USED: playbook #" in seen["user"] and "Find the motherboard" in seen["user"]
    assert verify.load()["playbooks"][str(old["id"])]["uses"] == 1 and verify.load()["playbooks"][str(old["id"])]["good"] == 0
    assert "Graded 1 request(s) where a playbook or rule was used; 1 went wrong." in rep
    assert "Check of what was learned:" in rep and rep == distill.last_report()
    new = next(p for p in playbooks.load() if p["title"] == "Show the desktop")
    assert new["cases"][0]["steps"][0]["tool"] == "press_keys" and new["retested"]  # kept for later re-tests, not re-tested tonight
