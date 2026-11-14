"""v2.0 Skill Hub: GitHub search/inspect/install (fake GitHub) and a real MCP stdio server subprocess."""
import io
import json
import sys
import textwrap
import zipfile

import pytest

from friday import skill_hub
from friday import tools as T
from friday.llm import build_system_prompt

SHA = "a" * 40


def test_bundled_official_mcp_servers_are_offered_once_and_stay_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path))
    from friday import toolpick
    assert skill_hub.ensure_bundled() == ["fetch", "time", "thinking"]
    m = skill_hub.manifest()
    assert m["mcp"]["thinking"]["command"][-1].endswith("@2.1.1")  # pinned, never "latest"
    assert all(c["readonly"] for c in m["mcp"].values())
    assert skill_hub.ensure_bundled() == []  # second call changes nothing
    del m["mcp"]["time"]
    skill_hub._save(m)  # the owner removes one...
    assert skill_hub.ensure_bundled() == [] and "time" not in skill_hub.manifest()["mcp"]  # ...it is not put back
    assert "worldtime" in toolpick.groups_for("what time is it in Tokyo")
    assert "deepthink" in toolpick.groups_for("think this through step by step")
    assert "worldtime" not in toolpick.groups_for("open notepad")


def _zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("owner-pack-aaaaaaa/README.md", "# Pack")
        z.writestr("owner-pack-aaaaaaa/invoices/SKILL.md",
                   "---\nname: invoice-helper\ndescription: Make clean invoices for a repair shop\n---\nStep 1: ...")
        z.writestr("owner-pack-aaaaaaa/../evil.txt", "nope")
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, kind="pack"):
        self.kind, self.urls = kind, []

    def __call__(self, req, timeout=0):
        u = req.full_url
        self.urls.append(u)
        if "/search/repositories" in u:
            body = {"items": [{"full_name": "owner/pack", "stargazers_count": 120, "pushed_at": "2026-10-01",
                               "license": {"spdx_id": "MIT"}, "description": "Shop skills", "archived": False},
                              {"full_name": "old/dead", "archived": True, "stargazers_count": 999}]}
        elif u.endswith("/repos/owner/pack"):
            body = {"default_branch": "main", "stargazers_count": 120, "license": {"spdx_id": "MIT"},
                    "pushed_at": "2026-10-01"}
        elif "/commits/main" in u:
            body = {"sha": SHA}
        elif "/git/trees/" in u:
            files = ["README.md", "invoices/SKILL.md"] if self.kind == "pack" else ["README.md", "package.json"]
            body = {"tree": [{"path": p, "type": "blob"} for p in files]}
        elif "/zipball/" in u:
            assert SHA in u  # pinned to the commit
            return io.BytesIO(_zip())
        elif "README.md" in u:
            return io.BytesIO(b"# Pack\nSkills for repair shops.")
        else:
            raise AssertionError(u)
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    (tmp_path / "skills").mkdir()
    fake = FakeGitHub()
    monkeypatch.setattr(skill_hub, "OPENER", fake)
    return fake


def test_find_inspect_install_pack(hub, tmp_path):
    out = T.dispatch("find_skills", {"query": "invoice"}, T.ToolContext())
    assert "1. owner/pack" in out and "old/dead" not in out
    info = T.dispatch("inspect_skill", {"repo": "1"}, T.ToolContext())
    assert "skill pack" in info and "aaaaaaa" in info and "repair shops" in info
    asked = []
    no = T.ToolContext(confirm=lambda p: asked.append(p) or False)
    assert "Not installed" in T.dispatch("install_github_skill", {"repo": "owner/pack"}, no)
    assert "Permission card" in asked[0]
    out = T.dispatch("install_github_skill", {"repo": "https://github.com/owner/pack"}, T.ToolContext(confirm=lambda p: True))
    assert "invoice_helper" in out
    assert not list(tmp_path.rglob("evil.txt"))
    assert "Step 1" in T.dispatch("use_skill", {"name": "invoice-helper"}, T.ToolContext())
    s = type("S", (), {"assistant_name": "Friday", "user_name": "Ibbi", "default_city": "St. Catharines", "base_url": "", "provider": "custom",
                       "model": "m", "backup_provider": "none"})()
    assert "invoice_helper: Make clean invoices" in build_system_prompt(s, None, False)
    assert "Removed" in T.dispatch("remove_github_skill", {"name": "owner/pack"}, T.ToolContext(confirm=lambda p: True))
    assert "No GitHub skills" in T.dispatch("list_github_skills", {}, T.ToolContext())


def test_mcp_repo_suggests_pinned_command_and_asks(hub):
    hub.kind = "mcp"
    asked = []
    out = T.dispatch("install_github_skill", {"repo": "owner/pack"}, T.ToolContext(confirm=lambda p: asked.append(p) or False))
    assert "Not installed" in out
    assert f"npx -y github:owner/pack#{SHA}" in asked[0] and "OUTSIDE the safety cage" in asked[0]


FAKE_SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            r = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}}
        elif m["method"] == "tools/list":
            r = {"tools": [{"name": "add", "description": "Add two numbers",
                            "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}}]}
        elif m["method"] == "tools/call":
            a = m["params"]["arguments"]
            r = {"content": [{"type": "text", "text": str(a["a"] + a["b"])}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
''')


def test_real_mcp_server_subprocess(hub, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_LEVEL", "2")
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER)
    added = skill_hub.start_server("calc", [sys.executable, str(script)])
    try:
        assert added == ["calc__add"]
        assert "Cancelled" in T.dispatch("calc__add", {"a": 2, "b": 3}, T.ToolContext())  # MCP always asks
        assert T.dispatch("calc__add", {"a": 2, "b": 3}, T.ToolContext(confirm=lambda p: True)) == "5"
    finally:
        skill_hub.stop_server("calc")
    assert "calc__add" not in T.REGISTRY


class BigPackGitHub(FakeGitHub):
    """A pack with 120 skills whose zip is too big: Friday must fetch only the SKILL.md files."""
    N = 120

    def __call__(self, req, timeout=0):
        u = req.full_url
        self.urls.append(u)
        if "/git/trees/" in u:
            files = ["README.md"] + [f"skills/s{i}/SKILL.md" for i in range(self.N)] + ["skills/s0/big.bin"]
            return io.BytesIO(json.dumps({"tree": [{"path": p, "type": "blob"} for p in files]}).encode())
        if "/zipball/" in u:
            raise AssertionError("big packs must not download the whole zip")
        if "raw.githubusercontent.com" in u and u.endswith("SKILL.md"):
            i = int(u.split("/s")[-1].split("/")[0])
            if i == 7:
                raise OSError("network blip")
            desc = "Plan carburetor rebuild jobs" if i == 42 else f"Generic skill number {i}"
            return io.BytesIO(f"---\nname: skill-{i}\ndescription: {desc}\n---\nDo step {i}.".encode())
        return super().__call__(req, timeout)


def test_big_pack_installs_skill_files_only_and_is_searchable(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(skill_hub, "OPENER", BigPackGitHub())
    asked = []
    out = T.dispatch("install_github_skill", {"repo": "owner/pack"},
                     T.ToolContext(confirm=lambda p: asked.append(p) or True))
    assert "120 instruction skills" in asked[0] and "and 105 more" in asked[0] and "only the 120 SKILL.md" in asked[0]
    assert "Installed 119 skill(s)" in out and "1 of 120 files failed" in out
    hits = T.dispatch("search_installed_skills", {"query": "carburetor rebuild"}, T.ToolContext())
    assert hits.splitlines()[1].startswith("- skill_42")
    assert "Do step 42" in T.dispatch("use_skill", {"name": "skill_42"}, T.ToolContext())
    s = type("S", (), {"assistant_name": "Friday", "user_name": "Ibbi", "default_city": "x", "base_url": "",
                       "provider": "custom", "model": "m", "backup_provider": "none"})()
    p = build_system_prompt(s, None, False)
    assert "119 installed skill-pack skills" in p and "skill_3:" not in p
    assert "How can I assist you today" in p  # listed as a banned phrase
    assert "(119 skills)" in T.dispatch("list_github_skills", {}, T.ToolContext())


def test_too_big_zip_falls_back_to_skill_files(hub, monkeypatch):
    def call(req, timeout=0):
        u = req.full_url
        if "/zipball/" in u:
            return io.BytesIO(b"x" * (skill_hub.MAX_ZIP + 1))
        if "raw.githubusercontent.com" in u and u.endswith("SKILL.md"):
            return io.BytesIO(b"---\nname: invoice-helper\ndescription: Shop invoices\n---\nok")
        return hub(req, timeout)
    monkeypatch.setattr(skill_hub, "OPENER", call)
    out = T.dispatch("install_github_skill", {"repo": "owner/pack"}, T.ToolContext(confirm=lambda p: True))
    assert "Installed 1 skill(s)" in out and "SKILL.md files only" in out
