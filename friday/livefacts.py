"""Read-only tools for live facts about this PC, so Jarvis reads them instead of guessing:
remote-desktop ID (AnyDesk / RustDesk / TeamViewer), all drives, biggest running programs, network address,
installed programs. None of these change anything."""
from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

from .tools import tool


def _anydesk_conf_paths() -> list[Path]:
    out = []
    for base in (os.environ.get("ProgramData"), os.environ.get("APPDATA")):
        if base:
            out.append(Path(base) / "AnyDesk" / "system.conf")
    return out


def anydesk_id() -> str:
    for p in _anydesk_conf_paths():
        try:
            m = re.search(r"^ad\.anynet\.id=(\d{6,12})\s*$", p.read_text(encoding="utf-8", errors="ignore"), re.M)
        except OSError:
            continue
        if m:
            n = m.group(1)
            return f"{int(n):,}".replace(",", " ")  # AnyDesk shows it in groups of three: 1 560 983 303
    return ""


def rustdesk_id() -> str:
    base = os.environ.get("APPDATA")
    if not base:
        return ""
    try:
        m = re.search(r"^id\s*=\s*'?([\w-]{6,20})'?\s*$",
                      (Path(base) / "RustDesk" / "config" / "RustDesk.toml").read_text(encoding="utf-8", errors="ignore"),
                      re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


@tool("remote_desktop_id", "Read this PC's AnyDesk (or RustDesk) ID from its config file. Use it when the user asks for "
      "their AnyDesk ID / remote access number. Read-only.", {"type": "object", "properties": {}})
def remote_desktop_id(ctx):
    found = []
    if anydesk_id():
        found.append(f"AnyDesk ID: {anydesk_id()}")
    if rustdesk_id():
        found.append(f"RustDesk ID: {rustdesk_id()}")
    return "\n".join(found) or ("I couldn't find an AnyDesk or RustDesk ID on this PC. Open AnyDesk once so it creates "
                                "its config, or tell me which remote tool you use.")


@tool("disk_report", "Free and used space on every drive of this PC. Read-only.", {"type": "object", "properties": {}})
def disk_report(ctx):
    import psutil

    rows = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        rows.append(f"{part.device} {u.free / 1e9:.0f} GB free of {u.total / 1e9:.0f} GB ({u.percent:.0f}% used)")
    return "\n".join(rows) or "No drives found."


@tool("top_processes", "The programs using the most memory right now (and CPU). Read-only. Use for 'why is my PC slow'.",
      {"type": "object", "properties": {"count": {"type": "integer"}}})
def top_processes(ctx, count: int = 8):
    import psutil

    count = max(1, min(int(count or 8), 20))
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.info["memory_info"].rss, p.info["name"] or "?"))
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    merged: dict[str, int] = {}
    for rss, name in procs:  # chrome.exe has dozens of processes: add them up
        merged[name] = merged.get(name, 0) + rss
    top = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:count]
    return "Using the most memory:\n" + "\n".join(f"- {n}: {r / 1e6:.0f} MB" for n, r in top)


@tool("network_info", "This PC's local IP address, computer name and Wi-Fi/network name. Read-only.",
      {"type": "object", "properties": {}})
def network_info(ctx):
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the right network card
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    wifi = ""
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
        wifi = m.group(1).strip() if m else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return f"Computer name: {socket.gethostname()}\nLocal IP: {ip or 'unknown'}\nWi-Fi: {wifi or 'not on Wi-Fi'}"


@tool("installed_apps", "List installed programs, optionally only those whose name contains a word. Read-only. Use it to "
      "check whether something is installed.", {"type": "object", "properties": {"contains": {"type": "string"}}})
def installed_apps(ctx, contains: str = ""):
    import winreg  # noqa: PLC0415

    names: set[str] = set()
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            with winreg.OpenKey(hive, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                            names.add(str(winreg.QueryValueEx(sub, "DisplayName")[0]))
                    except OSError:
                        continue
        except OSError:
            continue
    want = (contains or "").lower().strip()
    hits = sorted(n for n in names if want in n.lower())
    if not hits:
        return f"No installed program matches '{contains}'." if want else "I couldn't read the installed programs list."
    shown = hits[:40]
    return f"{len(hits)} installed" + (f" matching '{contains}'" if want else "") + ":\n" + "\n".join(
        f"- {n}" for n in shown) + (f"\n...and {len(hits) - 40} more" if len(hits) > 40 else "")
