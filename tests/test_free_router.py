import pytest


@pytest.fixture(autouse=True)
def _escalation_only(monkeypatch):
    monkeypatch.setenv("BRAIN_SPLIT", "false")  # these tests cover the escalation ladder; the split has its own tests


"""Tier order: local AI -> free cloud (OmniRoute) -> Abacus (credits)."""
import pytest

from friday import tools as T
from friday.config import load_settings
from friday.llm import Brain
from friday.memory import Memory
from test_friday import FakeOpenAI, _resp, clean_env, write_env  # noqa: F401

DOWN = Exception("Connection error: connection refused")


def _brain(clean_env, env_extra="", main=(), free=(), backup=()):
    s = load_settings(write_env(clean_env, "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen2.5:14b\nABACUS_API_KEY=k\n"
                                           "BACKUP_PROVIDER=abacus\nBACKUP_MAX_PER_DAY=5\nTOOL_MODE=native\n" + env_extra))
    ctx = T.ToolContext(settings=s, memory=Memory(s.data_dir), speak=lambda t: None, confirm=lambda p: False)
    b = Brain(s, ctx.memory, ctx)
    m, bk = FakeOpenAI(list(main)), FakeOpenAI(list(backup))
    b.reload(client=m, backup_client=bk)
    fr = FakeOpenAI(list(free))
    if b.free_router is not None:
        b.free_router.client = fr
    return b, m, fr, bk


def test_off_without_key(clean_env):
    b, *_ = _brain(clean_env)
    assert b.free_router is None and not b.settings.has_free_router


def test_on_with_key_and_settings(clean_env):
    b, *_ = _brain(clean_env, "FREE_ROUTER_KEY=sk-omni-123456789\n")
    s = b.settings.free_router_settings()
    assert (s.provider, s.base_url, s.model, s.api_key) == ("custom", "http://localhost:20128/v1", "auto/cheap",
                                                            "sk-omni-123456789")
    assert b.free_router is not None and not b.free_router.cloud  # local gateway: no Credit Gate prompts


def test_local_failure_uses_free_tier_not_abacus(clean_env):
    b, main, free, backup = _brain(clean_env, "FREE_ROUTER_KEY=sk-omni-123456789\n", main=[DOWN, DOWN, DOWN],
                                   free=[_resp(content="Free answer.")], backup=[_resp(content="Abacus answer.")])
    assert b.chat("explain torque specs") == "Free answer."
    assert b.last_route == "free cloud (main AI failed)"
    assert backup.calls == [] and b.backup_used_today() == 0 and b.free_used_today() == 1


def test_free_tier_failure_falls_through_to_abacus(clean_env):
    b, main, free, backup = _brain(clean_env, "FREE_ROUTER_KEY=sk-omni-123456789\n", main=[DOWN, DOWN, DOWN],
                                   free=[Exception("429 rate limit")], backup=[_resp(content="Abacus answer.")])
    assert b.chat("q") == "Abacus answer."
    assert b.last_route.startswith("backup") and b.free_used_today() == 1 and b.backup_used_today() == 1


def test_naming_abacus_skips_free_tier(clean_env):
    b, main, free, backup = _brain(clean_env, "FREE_ROUTER_KEY=sk-omni-123456789\n", free=[_resp(content="nope")],
                                   backup=[_resp(content="Abacus answer.")])
    r = b.chat("use abacus and think hard about pricing")
    assert r == "Abacus answer." and free.calls == [], (r, b.last_route, b.last_error, backup.calls)


def test_think_hard_goes_free_first_and_daily_cap(clean_env):
    b, main, free, backup = _brain(clean_env, "FREE_ROUTER_KEY=sk-omni-123456789\nFREE_ROUTER_MAX_PER_DAY=1\n",
                                   free=[_resp(content="Free deep answer.")],
                                   backup=[_resp(content="Abacus 2.")])
    assert b.chat("think hard about my shop pricing") == "Free deep answer." and backup.calls == []
    # free cap (1/day) used up -> next hard question goes to Abacus
    assert b.chat("think hard about my parts margins") == "Abacus 2."

