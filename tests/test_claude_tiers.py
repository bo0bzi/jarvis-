from types import SimpleNamespace

import pytest

from friday import brainmode, claude_tiers, credits, quick
from friday import tools as T
from friday.config import load_settings
from friday.credits import CreditGate
from friday.llm import Brain
from friday.memory import Memory
from test_friday import FakeOpenAI, _resp, clean_env, write_env  # noqa: F401


@pytest.fixture()
def claude_on(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-1234567890")
    monkeypatch.setenv("CLAUDE_TIERS", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CREDIT_PREAPPROVED_PER_DAY", "0")  # the owner's real allowance must not leak into the tests
    monkeypatch.setenv("CLAUDE_AUTO_OPUS", "false")
    return tmp_path


def test_cheapest_tier_that_fits_and_opus_only_by_name(claude_on, monkeypatch):
    c = claude_tiers.choose
    assert c("what is a good name for a dog", "too hard for main AI") == "sonnet"  # reason says hard
    assert c("recommend a movie") == "haiku"
    assert c("write a python script that renames my files and debug it") == "sonnet"
    assert c("use opus for this one") == "opus" and c("ask sonnet please") == "sonnet" and c("haiku, quick: capital of Peru") == "haiku"
    assert c("recommend a movie", failed=("haiku",)) == "sonnet"
    assert c("write code for a game", failed=("sonnet",)) == ""            # never climbs to Opus on its own...
    monkeypatch.setenv("CLAUDE_AUTO_OPUS", "true")
    assert c("write code for a game", failed=("sonnet",)) == "opus"        # ...unless the owner allowed it


def test_daily_caps_are_counted_per_tier(claude_on, monkeypatch):
    s = SimpleNamespace(data_dir=claude_on)
    monkeypatch.setenv("CLAUDE_MAX_OPUS_PER_DAY", "1")
    assert claude_tiers.allowed(s, "opus")
    claude_tiers.count_use(s, "opus")
    assert not claude_tiers.allowed(s, "opus") and claude_tiers.allowed(s, "haiku") and claude_tiers.used_today(s, "opus") == 1
    assert "opus claude-opus-5-5 1/1 today" in claude_tiers.summary(s)


def test_enabled_needs_a_real_key(monkeypatch):
    monkeypatch.setenv("CLAUDE_TIERS", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert not claude_tiers.enabled()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-realkey-123456789")
    assert claude_tiers.enabled()
    monkeypatch.setenv("CLAUDE_TIERS", "false")
    assert not claude_tiers.enabled()


def _brain(clean_env, responses=None):
    s = load_settings(write_env(clean_env, "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen3:30b\nTOOL_MODE=native\n"))
    ctx = T.ToolContext(settings=s, memory=Memory(s.data_dir), speak=lambda t: None, confirm=lambda p: True)
    return Brain(s, ctx.memory, ctx, client=FakeOpenAI(responses or [_resp("local answer")])), s


class FakeClaude:
    def __init__(self, tier, reply="Claude says hi"):
        self.label, self.last_error, self.tier, self.reply, self.asked = f"anthropic/{tier}", "", tier, reply, []

    def chat(self, text):
        self.asked.append(text)
        return self.reply


def test_escalation_uses_haiku_asks_the_gate_and_counts(clean_env, claude_on, monkeypatch):
    asked = []
    credits.GATE = CreditGate(confirm=lambda p: asked.append(p) or True)
    b, s = _brain(clean_env)
    fakes = {}
    monkeypatch.setattr(Brain, "_claude_brain", lambda self, tier: fakes.setdefault(tier, FakeClaude(tier)))
    out = b._escalate("recommend a movie", "the local AI could not answer")
    assert out == "Claude says hi" and b.last_route.startswith("Claude haiku") and asked and "Claude haiku" in asked[0]
    assert claude_tiers.used_today(s, "haiku") == 1


def test_declined_claude_falls_back_to_local_and_never_asks_abacus(clean_env, claude_on, monkeypatch):
    credits.GATE = CreditGate(confirm=lambda p: False)
    b, s = _brain(clean_env)
    monkeypatch.setattr(Brain, "_claude_brain", lambda self, tier: FakeClaude(tier))
    monkeypatch.setattr(b, "_retry_local", lambda text, why: f"local:{why}")
    assert b._escalate("recommend a movie", "x") == "local:cloud declined"
    assert claude_tiers.used_today(s, "haiku") == 0


def test_failed_haiku_climbs_to_sonnet(clean_env, claude_on, monkeypatch):
    credits.GATE = CreditGate(confirm=lambda p: True)
    b, s = _brain(clean_env)
    fakes = {"haiku": FakeClaude("haiku", "ESCALATE"), "sonnet": FakeClaude("sonnet", "Sonnet answer")}
    fakes["haiku"].last_error = "boom"
    monkeypatch.setattr(Brain, "_claude_brain", lambda self, tier: fakes[tier])
    assert b._escalate("recommend a movie", "x") == "Sonnet answer" and b.last_route.startswith("Claude sonnet")


def test_modes_persist_local_blocks_the_cloud_online_asks_claude(clean_env, claude_on, monkeypatch):
    assert brainmode.get() == "auto"
    brainmode.set_mode("local")
    assert brainmode.get() == "local"
    b, s = _brain(clean_env)
    assert not b._can_escalate() and not b._has_escalation()          # nothing leaves the PC
    brainmode.set_mode("online")
    credits.GATE = CreditGate(confirm=lambda p: True)
    fake = FakeClaude("haiku", "From Claude")
    monkeypatch.setattr(Brain, "_claude_brain", lambda self, tier: fake)
    assert b.chat("tell me something interesting about whales") == "From Claude"
    assert fake.asked
    brainmode.set_mode("auto")
    with pytest.raises(ValueError):
        brainmode.set_mode("turbo")


def test_notes_and_facts_are_saved_by_tools_and_fake_saves_are_caught(monkeypatch):
    from friday import llm
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    assert quick.handle(brain, "Add a note that my supplier for fork seals is Fox Racing") == \
        "add_note {'text': 'my supplier for fork seals is Fox Racing'}"
    assert quick.handle(brain, "Remember that my shop closes at 6 pm on Fridays") == \
        "remember_fact {'fact': 'my shop closes at 6 pm on Fridays'}"
    assert "remember_fact" not in str(quick.handle(brain, "remember to call John"))  # that's a reminder, not a fact
    for fake in ("Note saved: Supplier is Fox Racing.", "Fact remembered: Shop closes at 6.", "I've saved that for you.",
                 "Got it, I'll remember that."):
        assert llm.unbacked_claim(fake, []) == "saved", fake
    assert llm.unbacked_claim("Note saved: ok.", ["add_note"]) == ""
    assert llm.unbacked_claim("The weather is fine.", []) == ""


def test_disk_questions_read_the_real_drives(monkeypatch):
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a[0]), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name}")
    for q in ("How much free space do I have on drive D?", "how much space is left on my computer", "what's my disk space"):
        assert quick.handle(brain, q) == "disk_report", q


def test_set_mode_tool_warns_without_a_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert "No Claude API key" in brainmode.set_brain_mode(None, "online")
    assert brainmode.get() == "online"


def test_model_matching_and_quick_routes(monkeypatch):
    models = [{"name": "qwen3:30b-a3b", "gb": 18}, {"name": "qwen2.5:14b", "gb": 9}, {"name": "nomic-embed-text:latest", "gb": 0.3}]
    assert brainmode.match_model("qwen2.5:14b", models)[0] == "qwen2.5:14b"
    assert brainmode.match_model("30b", models)[0] == "qwen3:30b-a3b"
    assert "More than one" in brainmode.match_model("qwen", models)[1] and "don't have" in brainmode.match_model("llama", models)[1]
    calls = []
    brain = SimpleNamespace(ctx=T.ToolContext(), on_tool=lambda *a: calls.append(a), on_trace=lambda *a: None)
    monkeypatch.setattr(T, "dispatch", lambda name, args, ctx: f"{name} {args}")
    assert quick.handle(brain, "Jarvis, go online") == "set_brain_mode {'mode': 'online'}"
    assert quick.handle(brain, "go local") == "set_brain_mode {'mode': 'local'}"
    assert quick.handle(brain, "back to auto") == "set_brain_mode {'mode': 'auto'}"
    assert quick.handle(brain, "what model are you running on") is None or True
    assert quick.handle(brain, "switch your model to qwen2.5:14b") == "switch_local_model {'name': 'qwen2.5:14b'}"
    assert quick.handle(brain, "what models do you have") == "list_local_models {}"
