"""v2.0.7: replays Ibbi's failed chat. Direct commands, background jobs, tool picker, fake-promise guard,
placeholder repos, big GitHub file lists. Fakes only - no network, no cloud AI."""
import io
import json
import time
from types import SimpleNamespace

import pytest

from friday import jobs, quick, skill_hub, toolpick
from friday import tools as T
from friday.config import load_settings
from friday.llm import HONEST_FAIL, Brain, is_fake_promise

from test_v2_hub import BigPackGitHub, FakeGitHub

URL = "https://github.com/sickn33/agentic-awesome-skills"


def _resp(content=None, tool=None, args=None):
    calls = None
    if tool:
        calls = [SimpleNamespace(id="c1", function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))],
                           usage=None)


class Fake:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        return self.responses.pop(0)


@pytest.fixture
def brain(tmp_path, monkeypatch):
    for k in ("LLM_PROVIDER", "BACKUP_PROVIDER", "CUSTOM_MODEL", "TOOL_MODE", "ABACUS_API_KEY", "TOOL_PICKER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    (tmp_path / "skills").mkdir()
    env = tmp_path / ".env"
    env.write_text(f"DATA_DIR={tmp_path / 'data'}\nLLM_PROVIDER=custom\nCUSTOM_BASE_URL=http://localhost:11434/v1\n"
                   "CUSTOM_MODEL=qwen3.5:9b\nTOOL_MODE=native\n")
    s = load_settings(env, override=True)
    jobs.clear()

    def make(responses=(), confirm=lambda p: True):
        fake = Fake(responses)
        b = Brain(s, None, T.ToolContext(settings=s, confirm=confirm), client=fake)
        return b, fake
    yield make
    jobs.clear()


class Pack(BigPackGitHub):
    def __call__(self, req, timeout=0):
        u = req.full_url
        if "sickn33/agentic-awesome-skills" in u:
            u = u.replace("sickn33/agentic-awesome-skills", "owner/pack")
            req = SimpleNamespace(full_url=u)
        return super().__call__(req, timeout)


def _wait_done():
    for _ in range(200):
        if not jobs.running():
            return
        time.sleep(0.05)


def test_install_link_runs_directly_no_ai_no_second_question(brain, monkeypatch):
    monkeypatch.setattr(skill_hub, "OPENER", Pack())
    asked = []
    b, fake = brain([], confirm=lambda p: asked.append(p) or True)
    reply = b.chat(f"just install {URL} as a regular pack ad yes")
    assert fake.calls == []  # the AI was never asked
    assert asked == []  # the user named the exact repo: no "are you sure?"
    assert reply.startswith("STARTED (job 1)") and "really running" in reply
    _wait_done()
    assert jobs.recent(1)[0].status == "done" and "Installed 119 skill(s)" in jobs.recent(1)[0].result
    assert "sickn33/agentic-awesome-skills" in skill_hub.manifest()["packs"]


def test_status_reports_the_real_job(brain, monkeypatch):
    b, fake = brain([])
    assert "No background jobs" in b.chat("update on the skill pack installation ?")
    jobs.start("install skill pack x/y", lambda p: (p("downloaded 50 of 9,080 skill files"), time.sleep(0.3), "ok")[2])
    out = b.chat("im waiting")
    assert "RUNNING" in out and "50 of 9,080" in out and fake.calls == []
    _wait_done()
    assert "DONE" in b.chat("status")


def test_list_skills_is_direct(brain):
    b, fake = brain([])
    assert "skill" in b.chat("list all skills").lower() and fake.calls == []


def test_normal_chat_still_goes_to_ai(brain):
    b, fake = brain([_resp("Hey Ibbi.")])
    assert b.chat("tell me about github.com/a/b") == "Hey Ibbi."
    assert quick.repo_in("add oil change to job 12") == ""


def test_jobs_wait_and_thread():
    jobs.clear()
    seen = []
    j = jobs.start("t", lambda p: "Error: boom", notify=seen.append, wait=True)
    assert j.status == "failed" and seen and "boom" in seen[0]
    j2 = jobs.start("t2", lambda p: 1 / 0)
    _wait_done()
    assert j2.status == "failed" and "ZeroDivisionError" in j2.result
    jobs.clear()


def test_tool_picker_small_and_right(brain):
    b, fake = brain([_resp("ok")])
    names = toolpick.pick("can you install a skill for invoices")
    assert "install_github_skill" in names and "find_skills" in names and len(names) < 25
    assert "send_email" not in names
    b.chat("what's up")
    sent = [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert len(sent) < 15 and "load_tools" in sent and len(T.active_tools()) > 60


def test_load_tools_adds_a_group(brain):
    b, fake = brain([_resp(tool="load_tools", args={"group": "email"}), _resp("Loaded.")])
    b.chat("hmm do the thing")
    assert "send_email" not in [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert "send_email" in [t["function"]["name"] for t in fake.calls[1]["tools"]]


def test_cloud_brain_keeps_all_tools(brain):
    b, _ = brain([])
    b.cloud = True
    assert b._tool_names("hi") is None


def test_fake_promise_retried_then_honest(brain):
    b, fake = brain([_resp("Got it, pulling it down now. Stand by."), _resp("Installing it now, one moment.")])
    assert b.chat("please find a quo skill") == HONEST_FAIL
    assert len(fake.calls) == 2 and "did NOT call any tool" in fake.calls[1]["messages"][-1]["content"]


def test_fake_promise_fixed_by_nudge(brain):
    b, fake = brain([_resp("Let me fetch that."), _resp(tool="get_time_date"), _resp("It's 3 PM.")])
    assert b.chat("check the time") == "It's 3 PM."
    assert is_fake_promise("install it", "I'll install it now") and not is_fake_promise("hi", "I'll be here")


def test_placeholder_repo_rejected():
    for bad in ("please-provide-repo-name-or-link", "<owner/name>", "your-repo/here", "repo_name"):
        with pytest.raises(ValueError, match="placeholder|isn't a GitHub repo"):
            skill_hub._repo_name(bad)
    assert skill_hub._repo_name(URL) == "sickn33/agentic-awesome-skills"


def test_big_file_list_is_allowed(monkeypatch):
    big = json.dumps({"tree": [{"path": f"skills/s{i}/SKILL.md" + " " * 3000, "type": "blob"} for i in range(9100)]})
    assert len(big) > skill_hub.MAX_ZIP
    monkeypatch.setattr(skill_hub, "OPENER", lambda req, timeout=0: io.BytesIO(big.encode()))
    assert len(skill_hub._gh("/repos/x/y/git/trees/abc")["tree"]) == 9100
    monkeypatch.setattr(skill_hub, "OPENER", lambda req, timeout=0: io.BytesIO(b"x" * (skill_hub.MAX_ZIP + 1)))
    with pytest.raises(skill_hub.TooBig):
        skill_hub._gh("/repos/x/y/zipball/abc", raw=True)


def test_mcp_install_still_asks(brain, monkeypatch):
    monkeypatch.setattr(skill_hub, "OPENER", FakeGitHub(kind="mcp"))
    asked = []
    b, _ = brain([], confirm=lambda p: asked.append(p) or False)
    assert "Not installed" in b.chat("install https://github.com/owner/pack")
    assert asked and "OUTSIDE the safety cage" in asked[0]
