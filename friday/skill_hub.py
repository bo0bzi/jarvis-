"""Skill Hub: find ready-made skills on GitHub and install them safely.

Two kinds are supported:
* Skill packs (folders with a SKILL.md, the format Claude uses): instructions only. Friday sees each skill's name and
  description and reads the full instructions with use_skill when needed. No code from a pack is run.
* MCP servers (Model Context Protocol tool servers): a small program that gives Friday new tools. It runs as its own
  program OUTSIDE the safety cage, so installing one always shows a permission card and always asks you.

Everything is pinned to an exact commit (SHA), so what you approved can't change later without asking again.
GITHUB_TOKEN (optional) raises GitHub's search limits. Summaries of READMEs use the local AI only (free)."""
from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from . import activity
from . import tools as T
from .env import env
from .tools import tool

API = "https://api.github.com"
OPENER = urllib.request.urlopen  # tests replace this
MAX_ZIP = 25 * 1024 * 1024
MAX_JSON = 120 * 1024 * 1024  # the file list of a 9,000-skill pack is far bigger than a normal reply
MAX_PACK_SKILLS = 12000  # big "awesome" packs have 1,000-9,000+ skills
FULL_ZIP_LIMIT = 60      # packs with more skills than this: download only the SKILL.md files
PROMPT_LIST_LIMIT = 30   # more installed skills than this: Friday searches them instead of listing them all
SERVERS: dict[str, "McpServer"] = {}


def hub_dir() -> Path:
    from .skills import skills_dir

    d = skills_dir() / "_hub"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path() -> Path:
    return hub_dir() / "hub.json"


def manifest() -> dict:
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"packs": {}, "mcp": {}, "last_search": []}


def _save(m: dict) -> None:
    _manifest_path().write_text(json.dumps(m, indent=1), encoding="utf-8")


class TooBig(ValueError):
    pass


# ------------------------------------------------------------------ GitHub
def _gh(path: str, params: dict | None = None, raw: bool = False):
    url = path if path.startswith("http") else API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "Friday-assistant", "Accept": "application/vnd.github+json"}
    if env("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {env('GITHUB_TOKEN')}"
    limit = MAX_ZIP if raw else MAX_JSON
    with OPENER(urllib.request.Request(url, headers=headers), timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise TooBig(f"the download is over {limit // (1024 * 1024)} MB")
    return data if raw else json.loads(data)


def _repo_name(repo: str) -> str:
    repo = repo.strip().strip("<>\"'")
    if re.search(r"please|provide|placeholder|repo[-_ ]?name|your[-_ ]|owner/name|<|\.\.\.", repo, re.I) \
            and "github.com" not in repo.lower():
        raise ValueError(f"'{repo}' is a placeholder, not a real repo. Ask the user for the GitHub link "
                         "(or use the number from the last find_skills list).")
    if repo.isdigit():  # "number 2" from the last search
        last = manifest().get("last_search", [])
        i = int(repo) - 1
        if 0 <= i < len(last):
            return last[i]
    m = re.search(r"(?:github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$", repo)
    if not m:
        raise ValueError(f"'{repo}' isn't a GitHub repo like owner/name.")
    return m.group(1)


def _local_summary(ctx, text: str, question: str) -> str:
    """Summarize with the free local AI only; fall back to a plain excerpt."""
    brain = getattr(ctx, "brain", None)
    try:
        from .routines import _local_ask

        reply, _ = _local_ask(brain)("You summarize GitHub projects for a non-programmer in 3 short sentences.",
                                     f"{question}\n\n{text[:6000]}")
        return reply.strip()
    except Exception:
        clean = re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|[#*`>|]", " ", text)
        return re.sub(r"\s+", " ", clean).strip()[:500]


@tool("find_skills", "Search GitHub for ready-made skills Friday could install: MCP servers (new tools) and SKILL.md "
      "skill packs (instructions). Use when no current tool can do what the user wants.",
      {"type": "object", "properties": {"query": {"type": "string", "description": "what the skill should do"},
                                        "kind": {"type": "string", "description": "any (default), mcp or pack"}},
       "required": ["query"]})
def find_skills(ctx, query: str, kind: str = "any"):
    searches = []
    if kind in ("any", "mcp", ""):
        searches.append(f"{query} mcp server in:name,description,readme")
    if kind in ("any", "pack", ""):
        searches.append(f"{query} SKILL.md in:readme,description")
    seen: dict[str, dict] = {}
    for q in searches:
        try:
            for r in _gh("/search/repositories", {"q": q, "sort": "stars", "per_page": 8}).get("items", []):
                if not r.get("archived") and r["full_name"] not in seen:
                    seen[r["full_name"]] = r
        except Exception as e:
            return f"Error: GitHub search failed ({type(e).__name__}: {str(e)[:120]})."
    items = sorted(seen.values(), key=lambda r: -r.get("stargazers_count", 0))[:6]
    if not items:
        return f"I found no skills on GitHub for '{query}'. I could try to learn it myself (learn_skill)."
    m = manifest()
    m["last_search"] = [r["full_name"] for r in items]
    _save(m)
    return "Found on GitHub (say 'inspect number 1' to check one before installing):\n" + "\n".join(
        f"{i}. {r['full_name']} - {r.get('stargazers_count', 0)} stars, updated {str(r.get('pushed_at', ''))[:10]}"
        f", license {((r.get('license') or {}).get('spdx_id') or 'none')}: {(r.get('description') or '')[:140]}"
        for i, r in enumerate(items, 1))


def _repo_facts(repo: str) -> dict:
    info = _gh(f"/repos/{repo}")
    branch = info.get("default_branch", "main")
    sha = _gh(f"/repos/{repo}/commits/{branch}")["sha"]
    tree_json = _gh(f"/repos/{repo}/git/trees/{sha}", {"recursive": "1"})
    tree = tree_json.get("tree", [])
    paths = [t["path"] for t in tree if t.get("type") == "blob"]
    all_mds = [p for p in paths if p.split("/")[-1] == "SKILL.md"]
    # Big packs ship the same skill several times (e.g. a copy per AI app). Keep one copy per skill folder:
    # the shortest path, which is normally the main "skills/<name>/SKILL.md".
    best: dict[str, str] = {}
    for p in sorted(all_mds, key=lambda x: (x.count("/"), x)):
        best.setdefault(p.rsplit("/", 2)[-2] if "/" in p else "", p)
    skill_mds = sorted(best.values())
    kind = "pack" if skill_mds else ("mcp" if any(p in paths for p in ("package.json", "pyproject.toml")) else "unknown")
    return {"repo": repo, "sha": sha, "info": info, "paths": paths, "skill_mds": skill_mds, "kind": kind,
            "truncated": bool(tree_json.get("truncated")), "copies": len(all_mds) - len(skill_mds)}


def _suggest_command(repo: str, sha: str, paths: list[str]) -> list[str]:
    """A command that runs the MCP server at exactly this commit."""
    if "pyproject.toml" in paths:
        try:
            text = _gh(f"https://raw.githubusercontent.com/{repo}/{sha}/pyproject.toml", raw=True).decode()
            scripts = re.search(r"\[project\.scripts\]\s*\n\s*([\w.-]+)\s*=", text)
            name = scripts.group(1) if scripts else re.search(r'name\s*=\s*"([^"]+)"', text).group(1)
            return ["uvx", "--from", f"git+https://github.com/{repo}@{sha}", name]
        except Exception:
            return []
    if "package.json" in paths:
        return ["npx", "-y", f"github:{repo}#{sha}"]
    return []


@tool("inspect_skill", "Check a GitHub skill before installing: what it does, what kind it is, stars, license, last "
      "update. 'repo' is owner/name, a GitHub link, or the number from the last find_skills list.",
      {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]})
def inspect_skill(ctx, repo: str):
    repo = _repo_name(repo)
    f = _repo_facts(repo)
    info = f["info"]
    try:
        readme = _gh(f"https://raw.githubusercontent.com/{repo}/{f['sha']}/README.md", raw=True).decode("utf-8", "replace")
    except Exception:
        readme = info.get("description") or ""
    summary = _local_summary(ctx, readme, "What does this project do, and what would it let an assistant do?")
    kind = {"pack": f"a skill pack with {len(f['skill_mds'])} SKILL.md file(s) - instructions only, no code is run",
            "mcp": "an MCP tool server - it runs as its own program outside the safety cage",
            "unknown": "not a format Friday can install"}[f["kind"]]
    warn = []
    if info.get("stargazers_count", 0) < 20:
        warn.append("few stars (not widely used)")
    if not info.get("license"):
        warn.append("no license")
    if str(info.get("pushed_at", "")) < f"{datetime.now().year - 1}":
        warn.append("not updated in over a year")
    return (f"{repo}: {kind}. {info.get('stargazers_count', 0)} stars, license "
            f"{(info.get('license') or {}).get('spdx_id', 'none')}, last update {str(info.get('pushed_at', ''))[:10]}, "
            f"commit {f['sha'][:7]}. {summary}" + (f" Warnings: {', '.join(warn)}." if warn else ""))


def _extract(repo: str, sha: str, dest: Path) -> None:
    data = _gh(f"/repos/{repo}/zipball/{sha}", raw=True)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts[1:]  # drop GitHub's top folder
            if info.is_dir() or not parts or ".." in parts or Path(info.filename).is_absolute():
                continue
            out = dest.joinpath(*parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as s, open(out, "wb") as d:
                shutil.copyfileobj(s, d)


def _fetch_skill_files(repo: str, sha: str, paths: list[str], dest: Path, progress=None) -> list[str]:
    """Download only the SKILL.md files (for packs too big for one zip). Returns the paths that failed."""
    from concurrent.futures import ThreadPoolExecutor

    done = [0]
    lock = threading.Lock()

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    def one(p: str):
        if ".." in Path(p).parts:
            return p
        try:
            data = _gh(f"https://raw.githubusercontent.com/{repo}/{sha}/{urllib.parse.quote(p)}", raw=True)
        except Exception:
            return p
        out = dest.joinpath(*Path(p).parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        if progress:
            with lock:
                done[0] += 1
                if done[0] % 50 == 0 or done[0] == len(paths):
                    progress(f"downloaded {done[0]:,} of {len(paths):,} skill files")
        return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        return [p for p in pool.map(one, paths) if p]


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip().strip('"\'')
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")[:40] or "skill"


@tool("install_github_skill", "Install a skill from GitHub, pinned to its current commit. Always shows a permission "
      "card and asks first. For MCP servers you may give the start command (otherwise Friday works it out).",
      {"type": "object", "properties": {"repo": {"type": "string"},
                                        "command": {"type": "string", "description": "optional MCP start command"}},
       "required": ["repo"]})
def install_github_skill(ctx, repo: str, command: str = ""):
    repo = _repo_name(repo)
    f = _repo_facts(repo)
    sha, info = f["sha"], f["info"]
    facts = (f"{repo} at commit {sha[:7]} ({info.get('stargazers_count', 0)} stars, license "
             f"{(info.get('license') or {}).get('spdx_id', 'none')})")
    m = manifest()
    if f["kind"] == "pack":
        dest = hub_dir() / f"{_slug(repo.replace('/', '__'))}@{sha[:7]}"
        mds = f["skill_mds"][:MAX_PACK_SKILLS]
        total = len(f["skill_mds"])
        names = [p.rsplit("/", 2)[-2] if "/" in p else repo.split("/")[1] for p in mds]
        shown = ", ".join(names[:15]) + (f", and {total - 15} more" if total > 15 else "")
        big = total > FULL_ZIP_LIMIT
        how = (f"Because it is big, I will download only the {len(mds)} SKILL.md instruction files "
               "(about 1 minute per 1,000 skills), not the whole project. " if big else "")
        explicit = str(getattr(ctx, "explicit_repo", "") or "").lower() == repo.lower()
        if explicit:  # the user typed this exact repo: instructions only, so no second "are you sure?"
            activity.log("skill", f"Install of {repo} approved by the user's direct command ({total} skills)")
        elif not ctx.confirm(f"Permission card for {facts}: a skill pack of {total} instruction skills ({shown}). "
                           f"{how}No code from it is run - Friday only reads the instructions. Should I install it?"):
            return "Not installed (the user said no)."
        work = lambda progress=None: _install_pack(repo, f, dest, mds, total, big, progress)  # noqa: E731
        if getattr(ctx, "brain", None) is not None and big:
            # Big pack inside the running Friday: download in the background so the chat stays responsive.
            from . import jobs

            job = jobs.start(f"install skill pack {repo}", lambda p: work(p), notify=getattr(ctx, "speak", None))
            return (f"STARTED (job {job.id}): installing {total:,} skills from {repo} in the background, about "
                    f"{max(1, total // 1000)}-{max(2, total // 600)} minutes. It is really running now. I'll announce "
                    "when it's done; ask 'status' any time for the real progress.")
        return work()
    cmd = command.split() if command else _suggest_command(repo, sha, f["paths"])
    if not cmd:
        return (f"{repo} isn't a skill pack, and I can't tell how to start it as an MCP server. If its README "
                "gives a start command, tell me and I'll use it.")
    name = _slug(repo.split("/")[1].replace("mcp", "").replace("server", "")) or "mcp"
    if not ctx.confirm(f"Permission card for {facts}: an MCP tool server. It runs as its own program OUTSIDE the "
                       f"safety cage with your Windows user's access (internet and files). Start command: "
                       f"{' '.join(cmd)}. Its tools will ask you before each use. Only install it if you trust it. "
                       "Install?"):
        return "Not installed (the user said no)."
    m["mcp"][name] = {"repo": repo, "sha": sha, "command": cmd, "enabled": True, "installed": datetime.now().isoformat()}
    _save(m)
    activity.log("skill", f"Installed MCP server {name} from {repo}@{sha[:7]}: {' '.join(cmd)}")
    try:
        tools_added = start_server(name, cmd)
    except Exception as e:
        return f"Installed {name}, but it didn't start yet ({type(e).__name__}: {str(e)[:150]}). " \
               "It may need Node.js (npx) or uv (uvx) installed. I'll retry next time Friday starts."
    return f"Installed MCP server '{name}' with tools: {', '.join(tools_added) or 'none'}."


def _install_pack(repo: str, f: dict, dest: Path, mds: list[str], total: int, big: bool, progress=None) -> str:
    """Download and register a skill pack (after the user approved it)."""
    sha = f["sha"]
    m = manifest()
    if progress:
        progress(f"downloading {len(mds):,} skill files" if big else "downloading the pack")
    failed: list[str] = []
    method = "SKILL.md files only"
    if not big:
        try:
            _extract(repo, sha, dest)
            method = "full download"
        except TooBig:
            big = True
    if big:
        failed = _fetch_skill_files(repo, sha, mds, dest, progress)
    skills, seen = [], set()
    for p in mds:
        file = dest / p
        if not file.exists():
            continue
        fm = _frontmatter(file.read_text(encoding="utf-8", errors="replace"))
        name = _slug(fm.get("name") or file.parent.name)
        if name in seen:
            name = _slug(f"{name}_{file.parent.parent.name}")
        seen.add(name)
        skills.append({"name": name, "description": fm.get("description", "")[:300],
                       "file": str(file.relative_to(hub_dir()))})
    if not skills:
        shutil.rmtree(dest, ignore_errors=True)
        return (f"Error: installing {repo} failed - none of its {total} SKILL.md files could be downloaded "
                f"({len(failed)} failed). GitHub may be blocking or rate-limiting this PC right now.")
    m["packs"][repo] = {"sha": sha, "dir": dest.name, "skills": skills, "installed": datetime.now().isoformat()}
    _save(m)
    activity.log("skill", f"Installed skill pack {repo}@{sha[:7]} ({method}): {len(skills)} skills")
    notes = []
    if failed:
        notes.append(f"{len(failed)} of {len(mds)} files failed to download (say 'install it again' to retry)")
    if total > len(mds):
        notes.append(f"only the first {len(mds)} of {total} skills were kept")
    if f.get("copies"):
        notes.append(f"skipped {f['copies']} duplicate copies of the same skills")
    if f.get("truncated"):
        notes.append("GitHub sent a shortened file list, so a few skills may be missing")
    if big:
        notes.append("only the instruction files were downloaded, so a skill that points to extra example "
                     "files in its folder won't have them")
    listed = ", ".join(s["name"] for s in skills[:10]) + (f" and {len(skills) - 10} more" if len(skills) > 10 else "")
    return (f"Installed {len(skills)} skill(s) from {repo} at commit {sha[:7]} ({method}): {listed}. "
            + (f"Notes: {'; '.join(notes)}. " if notes else "")
            + "I find the right one with search_installed_skills and read it with use_skill.")


@tool("use_skill", "Read the full instructions of an installed skill pack skill (see 'Installed skill packs' in "
      "your instructions), then follow them with your other tools.",
      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def use_skill(ctx, name: str):
    for pack in manifest()["packs"].values():
        for s in pack["skills"]:
            if s["name"] == _slug(name) or s["name"] == name:
                text = (hub_dir() / s["file"]).read_text(encoding="utf-8", errors="replace")
                return text[:8000] + ("\n[...cut...]" if len(text) > 8000 else "")
    return f"No installed skill called '{name}'."


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


@tool("search_installed_skills", "Search the installed skill-pack skills by keywords and get the best matches "
      "(name and description). Use this before a specialised task when many skills are installed, then use_skill.",
      {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
def search_installed_skills(ctx, query: str):
    q = _words(query)
    scored = []
    for repo, pack in manifest()["packs"].items():
        for s in pack["skills"]:
            name_w = _words(s["name"].replace("_", " "))
            score = 3 * len(q & name_w) + len(q & _words(s["description"]))
            if score:
                scored.append((score, s["name"], s["description"][:200]))
    if not scored:
        total = sum(len(p["skills"]) for p in manifest()["packs"].values())
        return f"No installed skill matches '{query}' (out of {total} installed). Try other words or find_skills."
    scored.sort(key=lambda x: -x[0])
    return "Best matches (call use_skill with the name):\n" + "\n".join(f"- {n}: {d}" for _, n, d in scored[:8])


@tool("list_github_skills", "List skills installed from GitHub (skill packs and MCP servers).")
def list_github_skills(ctx):
    m = manifest()
    out = [f"Pack {r}@{p['sha'][:7]} ({len(p['skills'])} skills): {', '.join(s['name'] for s in p['skills'][:20])}"
           + (" ..." if len(p["skills"]) > 20 else "") for r, p in m["packs"].items()]
    out += [f"MCP {n} ({c['repo']}@{c['sha'][:7]}) - {'running' if n in SERVERS and SERVERS[n].alive() else 'stopped'}"
            for n, c in m["mcp"].items()]
    return "\n".join(out) or "No GitHub skills installed yet."


@tool("remove_github_skill", "Remove a skill pack (owner/name) or MCP server (its name) installed from GitHub.",
      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, risk="delete")
def remove_github_skill(ctx, name: str):
    m = manifest()
    if name in m["mcp"]:
        stop_server(name)
        del m["mcp"][name]
    elif name in m["packs"]:
        shutil.rmtree(hub_dir() / m["packs"][name]["dir"], ignore_errors=True)
        del m["packs"][name]
    else:
        return f"Nothing installed called '{name}'."
    _save(m)
    activity.log("skill", f"Removed GitHub skill {name}")
    return f"Removed {name}."


def prompt_section() -> str:
    """Injected into Friday's instructions: the names and descriptions of installed skill-pack skills."""
    rows = [f"- {s['name']}: {s['description'][:160]}" for p in manifest()["packs"].values() for s in p["skills"]]
    if not rows:
        return ""
    if len(rows) > PROMPT_LIST_LIMIT:
        return (f"\nYou have {len(rows)} installed skill-pack skills (expert instructions for specific jobs). Before a "
                "specialised task, call search_installed_skills with a few keywords, then use_skill on the best match "
                "and follow it.")
    return "\nInstalled skill packs (call use_skill with the name to read full instructions before doing that task):\n" \
        + "\n".join(rows[:30])


# ------------------------------------------------------------------ minimal MCP client (stdio, JSON-RPC 2.0)
class McpServer:
    def __init__(self, name: str, command: list[str], cwd: str | None = None):
        self.name, self.command, self.cwd = name, command, cwd
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._waiting: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self.tools: list[dict] = []

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 60) -> list[dict]:
        cmd = list(self.command)
        exe = shutil.which(cmd[0])  # Windows: npx -> npx.cmd
        if exe:
            cmd[0] = exe
        safe_env = {k: v for k, v in os.environ.items()
                    if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|PIN", k, re.IGNORECASE)}  # don't leak Friday's keys
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=self.cwd, env=safe_env, text=True, encoding="utf-8", bufsize=1,
                                     creationflags=flags)
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "friday", "version": "2.0"}}, timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = self.request("tools/list", {}, timeout).get("tools", [])
        return self.tools

    def _send(self, msg: dict) -> None:
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _reader(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            q = self._waiting.pop(msg.get("id"), None) if "id" in msg else None
            if q is not None:
                q.put(msg)

    def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        if not self.alive():
            raise RuntimeError(f"MCP server {self.name} isn't running")
        with self._lock:
            self._id += 1
            rid = self._id
        q: queue.Queue = queue.Queue()
        self._waiting[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            self._waiting.pop(rid, None)
            raise TimeoutError(f"MCP server {self.name} didn't answer {method}")
        if "error" in msg:
            raise RuntimeError(str(msg["error"].get("message", msg["error"]))[:300])
        return msg.get("result") or {}

    def call(self, tool_name: str, args: dict) -> str:
        res = self.request("tools/call", {"name": tool_name, "arguments": args}, timeout=120)
        text = "\n".join(c.get("text", f"[{c.get('type')}]") for c in res.get("content", []))
        return ("Error: " if res.get("isError") else "") + (text or "(no output)")

    def stop(self) -> None:
        if self.proc is not None and self.alive():
            self.proc.terminate()


def _tool_name(server: str, tool_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", f"{server}__{tool_name}")[:64]


def bundled_servers() -> dict[str, dict]:
    """Vetted, read-only MCP servers from the official modelcontextprotocol project, pinned to a version.
    Fetch and Time are Python packages installed in Jarvis's own environment; Sequential Thinking comes from npm."""
    return {
        "fetch": {"command": [sys.executable, "-m", "mcp_server_fetch"], "readonly": True,
                  "about": "reads a web page as clean text (official MCP Fetch server)"},
        "time": {"command": [sys.executable, "-m", "mcp_server_time"], "readonly": True,
                 "about": "time and timezone conversion (official MCP Time server)"},
        "thinking": {"command": ["npx", "-y", "@modelcontextprotocol/server-sequential-thinking@2.1.1"],
                     "readonly": True, "about": "step-by-step reasoning for hard problems (official MCP server)"},
    }


def ensure_bundled() -> list[str]:
    """Offer each bundled server once. If the owner removes one later it is NOT put back."""
    m = manifest()
    done = m.setdefault("bundled_done", [])
    added = []
    for name, cfg in bundled_servers().items():
        if name in done:
            continue
        done.append(name)
        if name not in m.setdefault("mcp", {}):
            m["mcp"][name] = {"repo": "modelcontextprotocol/servers", "sha": "bundled", "command": cfg["command"],
                              "enabled": True, "readonly": True, "about": cfg["about"],
                              "installed": datetime.now().isoformat()}
            added.append(name)
    if added or done != manifest().get("bundled_done"):
        _save(m)
    return added


def start_server(name: str, command: list[str], readonly: bool = False) -> list[str]:
    stop_server(name)
    srv = McpServer(name, command, cwd=str(hub_dir()))
    srv.start()
    SERVERS[name] = srv
    added = []
    for t in srv.tools:
        tname = _tool_name(name, t["name"])
        if tname in T.REGISTRY and T.REGISTRY[tname].source != f"mcp:{name}":
            continue

        def run(ctx, _t=t["name"], **kwargs):
            return srv.call(_t, kwargs)
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        T.REGISTRY[tname] = T.Tool(name=tname, description=f"[{name} MCP] {t.get('description', '')}"[:900],
                                   params=schema, func=run, dangerous=not readonly,
                                   risk="" if readonly else "system", source=f"mcp:{name}",
                                   perms=["internet"] if readonly else ["internet", "files", "run"])
        added.append(tname)
    activity.log("skill", f"MCP server {name} started: {len(added)} tool(s)")
    return added


def stop_server(name: str) -> None:
    srv = SERVERS.pop(name, None)
    if srv:
        srv.stop()
    for k in [k for k, t in T.REGISTRY.items() if getattr(t, "source", "") == f"mcp:{name}"]:
        T.REGISTRY.pop(k, None)


def start_all() -> dict:
    """At Friday's start: run every enabled MCP server (in the background; failures are logged, never fatal)."""
    result = {}
    try:
        ensure_bundled()
    except Exception as e:
        activity.log("error", f"Could not add the bundled MCP servers: {e}")
    for name, cfg in manifest().get("mcp", {}).items():
        if cfg.get("enabled", True):
            try:
                result[name] = start_server(name, cfg["command"], readonly=bool(cfg.get("readonly")))
            except Exception as e:
                result[name] = f"failed: {e}"
                activity.log("error", f"MCP server {name} didn't start: {e}")
    return result
