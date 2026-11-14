"""Real software tools: find, download and install programs.

Before this, Jarvis had no download/install tool, so a small AI would pretend: it "downloaded" a file it never fetched,
then ran an installer on an invented path. Now it has proper tools, and every result comes from the real action:
  winget_search(query)   - look up the exact package ID (read-only)
  install_app(app_id)    - install it with Windows' own package manager (winget). Asks you first.
  download_file(url)     - save a file to your Downloads folder and report the real path. Asks you first.
Nothing is ever run from a download automatically."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from . import tools as T
from .env import env_int

_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{1,100}$")
_RISKY_EXT = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".scr", ".dll", ".jar", ".msix", ".appx"}
# winget "already installed / nothing to do" results are not failures
_WINGET_OK = {0x8A15002B, 0x8A150061}  # no applicable update / already installed (unsigned Windows exit codes)


def downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _winget() -> str:
    return shutil.which("winget") or ""


@T.tool("winget_search", "Find a Windows program by name and get its exact winget ID (read-only). Use this before "
        "install_app. Example query: 'cpu-z'.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
def winget_search(ctx, query: str):
    exe = _winget()
    if not exe:
        return "Error: winget isn't available on this PC (it comes with Windows 10/11 'App Installer')."
    q = str(query).strip()[:80]
    if not q:
        return "Error: say what to search for."
    try:
        p = subprocess.run([exe, "search", q, "--accept-source-agreements"], capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "Error: winget search took too long."
    lines = [l.rstrip() for l in (p.stdout or "").splitlines() if l.strip() and not set(l.strip()) <= {"-", "\\", "|", "/"}]
    out = "\n".join(lines[:18])
    return out or f"No packages found for '{q}'."


@T.tool("install_app", "Download and install a Windows program with winget, by its exact ID (get it from "
        "winget_search, e.g. CPUID.CPU-Z). Asks the user for permission first. Only say it is installed if this "
        "tool's result says so.",
        {"type": "object", "properties": {"app_id": {"type": "string"}}, "required": ["app_id"]},
        dangerous=True, risk="system")
def install_app(ctx, app_id: str):
    exe = _winget()
    if not exe:
        return "Error: winget isn't available on this PC (it comes with Windows 10/11 'App Installer')."
    app_id = str(app_id).strip()
    if not _ID_OK.match(app_id):
        return f"Error: '{app_id}' isn't a valid winget ID. Call winget_search first and use the exact ID it shows."
    from . import admin

    try:  # if Windows pops up its admin box while installing, tell the owner (it can't be clicked by any program)
        with admin.watch_uac(f"install {app_id}", lambda m: admin.notify_owner(ctx, m)):
            p = subprocess.run([exe, "install", "--id", app_id, "-e", "--accept-package-agreements",
                                "--accept-source-agreements", "--silent"], capture_output=True, text=True,
                               timeout=env_int("INSTALL_TIMEOUT_SECONDS", 900), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return ("Error: the install is taking very long (15+ minutes). It may be waiting for an administrator "
                "approval box on the PC, or still running.")
    text = " ".join((p.stdout or p.stderr or "").split())
    tail, low, rc = text[-500:], text.lower(), p.returncode & 0xFFFFFFFF  # Windows codes come back unsigned
    if any(s in low for s in ("already installed", "no available upgrade", "no newer package")) or rc in _WINGET_OK:
        return f"{app_id} is already installed and up to date. Nothing needed doing. winget says: {tail}"
    if rc == 0:
        return f"Installed {app_id}. winget says: {tail}"
    return (f"Error: winget could not install {app_id} (exit code {rc:#x}). {tail} "
            "If it needed administrator rights, the owner must approve the Windows box (on the PC or through "
            "AnyDesk) and then ask again.")


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def winget_lookup(query: str) -> list[tuple[str, str]]:
    """[(name, id), ...] from `winget search`, best match first. [] if winget is missing or nothing found."""
    exe = _winget()
    q = str(query).strip()[:80]
    if not exe or not q:
        return []
    try:
        p = subprocess.run([exe, "search", q, "--accept-source-agreements"], capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace")
    except Exception:
        return []
    rows = []
    for line in (p.stdout or "").splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 3 and _ID_OK.match(parts[1]) and "." in parts[1] and parts[0].lower() != "name":
            rows.append((parts[0], parts[1]))
    return rows


def best_match(query: str) -> tuple[str, str] | None:
    """The package whose NAME contains what was asked for ('cpu z' finds CPU-Z), or None when unsure."""
    want = _squash(query)
    if len(want) < 3:
        return None
    tried = []
    for q in (query, query.replace(" ", "-"), want):  # winget search is literal: 'cpu z' finds nothing, 'cpu-z' does
        if q in tried:
            continue
        tried.append(q)
        for name, app_id in winget_lookup(q):
            if want in _squash(name):
                return name, app_id
    return None


@T.tool("download_file", "Download a file from an https link into the user's Downloads folder and return its REAL "
        "path. Asks the user for permission first. It never runs what it downloads. To install a program prefer "
        "install_app.",
        {"type": "object", "properties": {"url": {"type": "string"}, "filename": {"type": "string"}},
         "required": ["url"]}, dangerous=True, risk="system")
def download_file(ctx, url: str, filename: str = ""):
    url = str(url).strip()
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.netloc:
        return "Error: only https links can be downloaded."
    host = (u.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith(("192.168.", "10.", "169.254.")):
        return "Error: downloads from this PC or the local network aren't allowed."
    name = Path(urllib.parse.unquote(filename or Path(u.path).name or "download.bin")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "download.bin"
    dest = downloads_dir() / name
    n = 1
    while dest.exists():  # never overwrite
        dest = dest.with_name(f"{Path(name).stem} ({n}){Path(name).suffix}")
        n += 1
    cap = env_int("DOWNLOAD_MAX_MB", 800) * 1024 * 1024
    h, size = hashlib.sha256(), 0
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Jarvis assistant)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise ValueError(f"file is bigger than the {cap // (1024 * 1024)} MB limit")
                h.update(chunk)
                f.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        return f"Error: download failed: {type(e).__name__}: {str(e)[:200]}"
    if size == 0:
        dest.unlink(missing_ok=True)
        return "Error: the server sent an empty file."
    risky = " This is a program file. It was NOT run." if dest.suffix.lower() in _RISKY_EXT else ""
    return f"Downloaded {size:,} bytes to {dest} (SHA-256 {h.hexdigest()[:16]}...).{risky}"
