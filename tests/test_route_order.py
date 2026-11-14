"""Brain order: free cloud (freellmapi) -> Abacus (reasoning + memory) -> Claude (last)."""
import time

from friday import blackbox, brainmode, credits, playbooks, split
from friday import tools as T
from friday.config import load_settings
from friday.credits import CreditGate
from friday.llm import FREE_COOLDOWN_SECONDS, Brain
from friday.memory import Memory
from test_friday import FakeOpenAI, _resp, _tc, clean_env, write_env  # noqa: F401

DOWN = Exception("Connection error: connection refused")
ENV = ("LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen2.5:14b\nABACUS_API_KEY=k\nBACKUP_PROVIDER=abacus\nBACKUP_MAX_PER_DAY=5\n"
       "TOOL_MODE=native\nFREE_ROUTER_KEY=sk-free-123456789\n")


class FakeClaude:
    def __init__(self, reply="Claude answer."):
        self.label, self.last_error, self.reply, self.asked = "anthropic/haiku", "", reply, []

    def chat(self, text):
        self.asked.append(text)
        return self.reply


def _brain(clean_env, monkeypatch, main=(), free=(), backup=(), claude=True, split_on=False):
    monkeypatch.setenv("BRAIN_SPLIT", "true" if split_on else "false")
    monkeypatch.setenv("CLAUDE_TIERS", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-realkey-123456789" if claude else "")
    credits.GATE = CreditGate(confirm=lambda p: True)
    s = load_settings(write_env(clean_env, ENV))
    ctx = T.ToolContext(settings=s, memory=Memory(s.data_dir), speak=lambda t: None, confirm=lambda p: True)
    b = Brain(s, ctx.memory, ctx)
    m, bk, fr = FakeOpenAI(list(main)), FakeOpenAI(list(backup)), FakeOpenAI(list(free))
    b.reload(client=m, backup_client=bk)
    b.free_router.client = fr
    fake = FakeClaude()
    monkeypatch.setattr(Brain, "_claude_brain", lambda self, tier: fake)
    return b, m, fr, bk, fake


def test_free_first_then_abacus_and_claude_is_not_asked(clean_env, monkeypatch):
    b, main, free, backup, claude = _brain(clean_env, monkeypatch, main=[DOWN, DOWN, DOWN], free=[DOWN],
                                           backup=[_resp(content="Abacus answer.")])
    assert b.chat("q") == "Abacus answer."
    assert b.last_route.startswith("backup") and claude.asked == []


def test_claude_is_the_last_route_when_abacus_fails(clean_env, monkeypatch):
    b, main, free, backup, claude = _brain(clean_env, monkeypatch, main=[DOWN, DOWN, DOWN], free=[DOWN],
                                           backup=[DOWN, DOWN, DOWN])
    assert b.chat("q") == "Claude answer."
    assert b.last_route.startswith("Claude") and len(claude.asked) == 1


def test_helper_brains_never_hand_over_themselves(clean_env, monkeypatch):
    b, *_ = _brain(clean_env, monkeypatch)
    assert b.free_router.sub and b.backup.sub and not b.sub
    assert not b.free_router._can_escalate() and not b.backup._has_escalation()


def test_naming_claude_goes_straight_to_claude(clean_env, monkeypatch):
    b, main, free, backup, claude = _brain(clean_env, monkeypatch, free=[_resp(content="free")],
                                           backup=[_resp(content="abacus")])
    assert b.chat("ask claude what a carburetor does") == "Claude answer."
    assert free.calls == [] and backup.calls == []


def test_reasoning_and_memory_questions_go_to_abacus_not_the_free_brain(clean_env, monkeypatch):
    b, main, free, backup, claude = _brain(clean_env, monkeypatch, split_on=True, free=[_resp(content="free")],
                                           backup=[_resp(content="Abacus thought it through."),
                                                   _resp(content="We decided on Tuesday.")])
    assert b.chat("why would a two stroke bog down at full throttle") == "Abacus thought it through."
    assert b.chat("what did we decide about the shop hours last time") == "We decided on Tuesday."
    assert free.calls == [] and claude.asked == [] and b.last_route.startswith("backup")


def test_wants_reasoning_only_for_real_questions():
    for q in ["why does my sled bog down at full throttle", "compare the two quotes and recommend one for me",
              "do you remember what I said about the trailer", "what did we talk about yesterday afternoon"]:
        assert split.wants_reasoning(q), q
    for q in ["open chrome", "what time is it", "play some music on youtube please", "why", "explain"]:
        assert not split.wants_reasoning(q), q


def test_allowance_covers_abacus_but_claude_always_asks_and_a_no_goes_back_to_abacus(clean_env, monkeypatch):
    b, main, free, backup, claude = _brain(clean_env, monkeypatch, free=[_resp(content="free")],
                                           backup=[_resp(content="Abacus took it.")])
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "1000")
    monkeypatch.setenv("CREDITS_PER_1K_TOKENS", "1")
    asked = []
    credits.GATE = CreditGate(confirm=lambda p: asked.append(p) or False)  # the owner says no to everything asked
    assert b.chat("ask claude what a carburetor does") == "Abacus took it."
    assert len(asked) == 1 and "Claude" in asked[0] and "Estimate" in asked[0]  # Claude asked, with its estimate
    assert claude.asked == [] and b.last_route.startswith("backup")             # Abacus ran on the allowance, unasked


def test_allowance_counts_real_spend_not_estimates(monkeypatch):
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "10")
    monkeypatch.setenv("CREDITS_PER_1K_TOKENS", "1")
    asked = []
    g = CreditGate(confirm=lambda p: asked.append(p) or True)
    for _ in range(3):  # three tasks estimated at up to 8 credits each, really costing 2 each: all fit in 10
        with g.task("small", "y", 1, 8) as ok:
            assert ok
            g.charge(1500, 500)
    assert asked == [] and g.data["preapproved_used"][max(g.data["preapproved_used"])] == 6.0
    with g.task("claude job", "y", 1, 8, "anthropic/claude", always_ask=True) as ok:
        assert ok
    assert len(asked) == 1  # Claude is never covered by the allowance


def test_rate_limited_free_tier_is_skipped_for_a_few_minutes(clean_env, monkeypatch):
    b, main, free, backup, claude = _brain(clean_env, monkeypatch, main=[DOWN, DOWN, DOWN, DOWN, DOWN, DOWN],
                                           free=[Exception("Error code: 429 - All models exhausted")],
                                           backup=[_resp(content="A1."), _resp(content="A2.")])
    assert b.chat("q one") == "A1."
    assert not b._free_allowed() and b._free_cool_until > time.time() + FREE_COOLDOWN_SECONDS - 5
    calls = len(free.calls)
    assert b.chat("q two") == "A2." and len(free.calls) == calls  # not tried again while cooling down
    b._free_cool_until = 0.0
    assert b._free_allowed()


# ---- tool picker: the local AI can only use a tool it was shown ----
def test_picker_offers_the_right_tool_for_everyday_requests():
    import friday.main  # noqa: F401  (registers every tool)
    from friday import toolpick

    for text, tool in [("minimize all windows", "press_keys"), ("maximize spotify", "press_keys"),
                       ("what motherboard do i have in this machine?", "system_info"),
                       ("what motherboard do i have in this machine?", "run_shell_command"),
                       ("list the folders in my d:/ drive", "list_folder"), ("what's in my downloads folder", "list_folder"),
                       ("what do you remember about me", "list_facts"), ("what texts did I get today", "read_texts"),
                       ("give me the daily digest", "daily_digest"), ("give me a brief system check", "system_info")]:
        assert tool in toolpick.pick(text), (text, tool)


def test_picker_no_longer_matches_words_inside_other_words():
    from friday import toolpick

    assert "texts" not in toolpick.groups_for("the microphone is very sensitive")   # 'phone' inside 'microphone'
    assert "pc" not in toolpick.groups_for("what happened in grammar class")         # 'app' / 'ram' inside words
    assert "shop" not in toolpick.groups_for("we went to a party in the apartment")  # 'part' inside words
    assert "texts" in toolpick.groups_for("text my phone when it is done")


def test_picker_safety_net_needs_two_uncommon_words():
    import friday.main  # noqa: F401
    from friday import toolpick

    assert toolpick.by_description("hello") == [] and toolpick.by_description("what is the capital of Peru") == []
    assert len(toolpick.pick("tell me a short joke")) <= len(toolpick.CORE) + 3


# ---- Teach mode: Abacus does the work, the local AI is the apprentice
def _teach(clean_env, monkeypatch, **kw):
    out = _brain(clean_env, monkeypatch, **kw)
    brainmode.set_mode("teach")
    return out


def _proven(title, when, text, tool):
    import friday.main  # noqa: F401

    pb, why = playbooks.clean({"title": title, "when": when, "examples": [text],
                               "steps": [{"tool": tool, "args": {}, "why": "x"}]}, T.REGISTRY)
    assert pb, why
    return playbooks.update(playbooks.add_or_update(pb)["id"], enabled=True, status="proven")


def test_teach_mode_is_a_known_mode_you_can_switch_to_by_voice_or_phone():
    import friday.main  # noqa: F401
    from friday import phone

    assert "teach" in brainmode.MODES and "apprentice" not in brainmode.set_mode("teach")
    assert T.dispatch("set_brain_mode", {"mode": "teach"}, T.ToolContext()).startswith("Teach:")
    assert "teach" in T.REGISTRY["set_brain_mode"].params["properties"]["mode"]["enum"]
    brainmode.set_mode("auto")
    assert phone.control(None, "brain:teach").startswith("Teach:") and brainmode.get() == "teach"


def test_in_teach_mode_abacus_does_the_work_and_every_step_is_recorded(clean_env, monkeypatch):
    b, main, free, backup, claude = _teach(clean_env, monkeypatch, free=[_resp(content="free")], backup=[
        _resp(tool_calls=[_tc("c1", "add_note", {"text": "order fork seals"})]), _resp(content="Noted for the supplier.")])
    assert b.chat("jot down that we must order fork seals tomorrow") == "Noted for the supplier."
    assert b.last_route.startswith("backup") and main.calls == [] and free.calls == [] and claude.asked == []
    (ep,) = blackbox.episodes(24)
    assert ep["steps"][0]["tool"] == "add_note" and ep["steps"][0]["cloud"] and ep["steps"][0]["brain"] == "abacus/route-llm"
    assert ep["replies"] == ["Noted for the supplier."] and ep["cloud_final"]


def test_abacus_is_not_stopped_by_the_25_a_day_backup_limit_in_any_mode(clean_env, monkeypatch):
    b, main, free, backup, claude = _teach(clean_env, monkeypatch, backup=[_resp(content=f"Answer {i}.") for i in range(8)])
    assert b.backup_used_today() == 0 and b.settings.backup_max_per_day == 5
    for i in range(8):                                     # more than BACKUP_MAX_PER_DAY (5 in this test)
        assert b.chat(f"do job number {i} for the garage board") == f"Answer {i}."
    assert main.calls == [] and len(backup.calls) == 8
    brainmode.set_mode("auto")
    assert b._backup_allowed()                             # Abacus has no limit in Friday in any mode


def test_the_local_ai_takes_over_a_job_with_a_proven_playbook_and_asks_abacus_when_it_fails(clean_env, monkeypatch):
    _proven("Read the shop clock", ["shop clock reading"], "grab the shop clock reading", "get_time_date")
    b, main, free, backup, claude = _teach(
        clean_env, monkeypatch, main=[_resp(tool_calls=[_tc("c1", "get_time_date", {})]), _resp(content="It is noon.")])
    assert b.chat("grab the shop clock reading now") == "It is noon."
    assert backup.calls == [] and b.last_route == "main"   # graduated: local, free and fast
    (ep,) = blackbox.episodes(24)
    assert ep["steps"][0]["tool"] == "get_time_date" and not ep["cloud_final"] and ep["used"]["playbook"]
    # when the local AI fails on a graduated job, the teacher steps in
    b2, main2, _, backup2, _ = _teach(clean_env, monkeypatch, main=[DOWN, DOWN, DOWN], backup=[_resp(content="Abacus covered it.")])
    assert b2.chat("grab the shop clock reading again") == "Abacus covered it."


def test_requests_about_passwords_and_keys_stay_on_this_pc_even_in_teach_mode(clean_env, monkeypatch):
    b, main, free, backup, claude = _teach(clean_env, monkeypatch, main=[_resp(content="I can't look up passwords.")],
                                           backup=[_resp(content="should never be asked")])
    assert b.chat("what is the wifi password for the shop router") == "I can't look up passwords."
    assert backup.calls == [] and split.touches_secrets("show me my api key") and not split.touches_secrets("open spotify")


def test_teach_mode_never_asks_before_using_abacus(clean_env, monkeypatch):
    b, main, free, backup, claude = _teach(clean_env, monkeypatch, main=[_resp(content="local")],
                                           backup=[_resp(content="Abacus answered.")])
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "0")
    monkeypatch.setenv("CREDIT_DAILY_CAP", "1")
    asked = []
    credits.GATE = CreditGate(confirm=lambda p: asked.append(p) or False)   # nobody to ask, and caps of 1: it must not matter
    assert b.chat("work out the garage board layout for me") == "Abacus answered."
    assert asked == [] and main.calls == [] and credits.GATE.abacus_today() > 0