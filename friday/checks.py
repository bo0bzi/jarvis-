"""Status checks shown on the dashboard. Every check returns a dict:
{"id", "name", "status": ok|warn|error|info, "summary", "details": {...}, "latency_ms", "checked_at"}.
Nothing here ever invents a number: if a value can't be fetched, the check says so."""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime
from importlib import metadata

ABACUS_CREDITS_URL = "https://api.abacus.ai/api/v0/_getOrganizationComputePoints"
ABACUS_BILLING_PAGE = "https://apps.abacus.ai/chatllm/"  # then: profile menu > Billing / Usage
ABACUS_KEYS_PAGE = "https://abacus.ai/app/route-llm-apis"


def _result(cid, name, status, summary, details=None, latency=None):
    return {"id": cid, "name": name, "status": status, "summary": summary, "details": details or {},
            "latency_ms": None if latency is None else round(latency), "checked_at": datetime.now().isoformat(timespec="seconds")}


def _http_json(url, headers=None, timeout=10, data=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Friday-assistant", **(headers or {})}, data=data)
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else {}, (time.perf_counter() - t) * 1000


# --------------------------------------------------------------------------- individual checks
def check_internet() -> dict:
    t = time.perf_counter()
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=4).close()
        return _result("internet", "Internet", "ok", "Connected", latency=(time.perf_counter() - t) * 1000)
    except OSError as e:
        return _result("internet", "Internet", "error", f"No connection ({e})")


def check_backup(settings, brain=None) -> dict:
    """The backup ('big brain') AI used only for hard cases."""
    if not getattr(settings, "has_backup", False):
        return _result("backup", "Backup AI", "info", "Not set up (optional). Settings > Backup brain.")
    r = check_api(settings.backup_settings(), brain, cid="backup", label="Backup AI")
    used = brain.backup_used_today() if brain is not None and hasattr(brain, "backup_used_today") else None
    r["details"]["used_today"] = used if used is not None else "unknown"
    r["details"]["daily_limit"] = settings.backup_max_per_day
    if used is not None:
        r["summary"] += f" · used {used}/{settings.backup_max_per_day} today"
    return r


def check_free_router(settings, brain=None) -> dict:
    """The free cloud tier (OmniRoute). Only lists its models, so the check costs no free-tier quota."""
    name = "Free cloud AI (freellmapi)"
    if not getattr(settings, "has_free_router", False):
        return _result("free_router", name, "info", "Not set up (optional). Settings > Free cloud (freellmapi).")
    url = settings.free_router_url.rstrip("/")
    details = {"address": url, "model": settings.free_router_model, "daily_limit": settings.free_router_max_per_day}
    used = brain.free_used_today() if brain is not None and hasattr(brain, "free_used_today") else None
    suffix = f" · used {used}/{settings.free_router_max_per_day} today" if used is not None else ""
    if used is not None:
        details["used_today"] = used
    t = time.perf_counter()
    try:
        data, latency = _http_json(f"{url}/models", {"Authorization": f"Bearer {settings.free_router_key}"}, timeout=10)
    except urllib.error.HTTPError as e:
        details["error"] = f"HTTP {e.code}"
        why = ("freellmapi key rejected (copy the unified key again from freellmapi)" if e.code in (401, 403)
               else f"freellmapi answered with an error ({e.code})")
        return _result("free_router", name, "error", why, details, (time.perf_counter() - t) * 1000)
    except Exception as e:
        details["error"] = str(e)[:200]
        return _result("free_router", name, "error", "Can't reach the free cloud router (freellmapi) - is it running on port 31415?", details,
                       (time.perf_counter() - t) * 1000)
    ids = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    details["models_available"] = len(ids)
    if settings.free_router_model not in ids and not settings.free_router_model.startswith("auto"):
        return _result("free_router", name, "warn", f"Reachable, but the model '{settings.free_router_model}' isn't in "
                       "its list - pick another in Settings" + suffix, details, latency)
    return _result("free_router", name, "ok", f"freellmapi reachable · {len(ids)} models · using "
                   f"{settings.free_router_model}" + suffix, details, latency)


def check_api(settings, brain=None, cid: str = "api", label: str = "AI API") -> dict:
    """Validate the key and reach the configured AI provider with a tiny request."""
    from .config import is_placeholder

    name = f"{label} ({settings.provider})"
    if settings.provider != "custom" and is_placeholder(settings.api_key):
        return _result(cid, name, "error", "No API key set. Add it in Settings.", {"get_key": ABACUS_KEYS_PAGE})
    details = {"provider": settings.provider, "model": settings.model, "base_url": settings.base_url or "(default)"}
    t = time.perf_counter()
    try:
        if settings.provider == "anthropic":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.api_key, timeout=20)
            resp = client.messages.create(model=settings.model, max_tokens=5,
                                          messages=[{"role": "user", "content": "Reply with: ok"}])
            text = " ".join(getattr(b, "text", "") for b in resp.content)
        else:
            from openai import OpenAI

            client = OpenAI(api_key=settings.api_key or "not-needed", base_url=settings.base_url or None, timeout=20)
            resp = client.chat.completions.create(model=settings.model, max_tokens=5,
                                                  messages=[{"role": "user", "content": "Reply with: ok"}])
            text = resp.choices[0].message.content or ""
            details["answered_by"] = getattr(resp, "model", "") or settings.model
        latency = (time.perf_counter() - t) * 1000
        if brain is not None and getattr(brain, "usage", None) is not None:
            brain.usage.record(resp, latency)
        details["reply"] = text.strip()[:60]
        return _result(cid, name, "ok", "Key valid, AI responding" if settings.provider != "custom" else "AI responding",
                       details, latency)
    except Exception as e:
        latency = (time.perf_counter() - t) * 1000
        msg = str(e)
        low = msg.lower()
        if "credits" in low or "quota" in low:
            summary = "Out of credits / quota"
        elif ("401" in msg or "403" in msg or "unauthorized" in low or "forbidden" in low
              or "invalid api key" in low or "authentication" in low):
            summary = "API key rejected (or no access to this model)"
        elif "429" in msg or "rate" in low:
            summary = "Rate limited (try again shortly)"
        elif "connect" in low or "timeout" in low or "timed out" in low:
            summary = ("Can't reach the local AI - is Ollama / the server running?"
                       if settings.provider == "custom" else "Can't reach the service")
        elif "model" in low:
            summary = "Model name not accepted"
        else:
            summary = "Request failed"
        details["error"] = msg[:300]
        return _result(cid, name, "error", summary, details, latency)


def check_abacus_credits(settings) -> dict:
    """Abacus credits. Uses Abacus' own (undocumented) organization-credits endpoint with your API key."""
    from .config import is_placeholder
    from .settings_store import EnvStore

    name = "Abacus.AI credits"
    key = settings.api_key if settings.provider == "abacus" else os.environ.get("ABACUS_API_KEY", "")
    if not key or is_placeholder(key):
        key = EnvStore().read().get("ABACUS_API_KEY", "")
    if not key or is_placeholder(key):
        return _result("credits", name, "info", "No Abacus key configured", {"billing_page": ABACUS_BILLING_PAGE})
    try:
        data, latency = _http_json(ABACUS_CREDITS_URL, headers={"apiKey": key}, timeout=12)
    except urllib.error.HTTPError as e:
        return _result("credits", name, "warn", f"Credits not available via API (HTTP {e.code}). Check the billing page.",
                       {"billing_page": ABACUS_BILLING_PAGE})
    except Exception as e:
        return _result("credits", name, "warn", f"Couldn't fetch credits ({type(e).__name__}). Check the billing page.",
                       {"billing_page": ABACUS_BILLING_PAGE})
    r = data.get("result") if isinstance(data, dict) else None
    if not data.get("success") or not isinstance(r, dict) or "computePointsLeft" not in r:
        return _result("credits", name, "warn", "Credits not available via API. Check the billing page.",
                       {"billing_page": ABACUS_BILLING_PAGE})
    left, total = r.get("computePointsLeft"), r.get("totalComputePoints")
    details = {"credits_left": left, "credits_total": total, "billing_page": ABACUS_BILLING_PAGE,
               "note": "From Abacus.AI's organization credit endpoint (undocumented; may change)."}
    pct = None
    if isinstance(left, (int, float)) and isinstance(total, (int, float)) and total > 0:
        pct = round(100 * left / total, 1)
        details["percent_left"] = pct
    status = "ok" if pct is None or pct > 25 else "warn" if pct > 5 else "error"
    summary = f"{left:,.0f} of {total:,.0f} credits left" if pct is not None else f"{left} credits left"
    return _result("credits", name, status, summary, details, latency)


def check_email(settings) -> dict:
    from . import email_tools

    t = time.perf_counter()
    r = email_tools.check_connection()
    cfg = email_tools.email_config()
    return _result("email", "Email", r["status"], r["summary"],
                   {"provider": cfg["provider"], "server": cfg["imap"] or "(not set)", "address": cfg["address"] or "(not set)"},
                   (time.perf_counter() - t) * 1000 if r["status"] == "ok" else None)


def check_skills(settings) -> dict:
    """Everything Jarvis has learned: code skills, taught/auto-learned rules, installed skill packs."""
    from . import lessons, skills

    try:
        rules = lessons.load()
    except Exception:
        rules = []
    taught = [r for r in rules if r.get("source") != "evolve"]
    auto = [r for r in rules if r.get("source") == "evolve"]
    packs = mcp = pack_skills = 0
    try:
        from . import skill_hub

        m = skill_hub.manifest()
        packs, mcp = len(m["packs"]), len(m["mcp"])
        pack_skills = sum(len(p["skills"]) for p in m["packs"].values())
    except Exception:
        pass
    last_learn = ""
    try:
        from . import evolve

        p = evolve._report_path()
        if p.exists():
            last_learn = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")
    except Exception:
        pass
    parts = []
    if skills.LOADED or skills.ERRORS:
        parts.append(f"{len(skills.LOADED)} code skill(s)" + (f" ({len(skills.ERRORS)} broken)" if skills.ERRORS else ""))
    if rules:
        parts.append(f"{len(taught)} taught + {len(auto)} self-learned rule(s)")
    if packs or mcp:
        parts.append(f"{pack_skills} skill-pack skill(s) in {packs} pack(s)" + (f", {mcp} MCP server(s)" if mcp else ""))
    details = {"code_skills": {k: ", ".join(v) for k, v in skills.LOADED.items()}, "broken": dict(skills.ERRORS),
               "taught_rules": [r["how"][:120] for r in taught][-10:], "self_learned_rules": [r["how"][:120] for r in auto][-10:],
               "skill_packs": packs, "mcp_servers": mcp, "last_learning_session": last_learn or "never",
               "folder": str(skills.skills_dir())}
    if not parts:
        return _result("skills", "Learned skills", "info",
                       "Nothing learned yet. Say 'teach: ...' or 'evolve' (or just let it learn tonight).", details)
    summary = " · ".join(parts) + (f" · last learning session {last_learn}" if last_learn else " · no learning session yet")
    return _result("skills", "Learned skills", "warn" if skills.ERRORS else "ok", summary, details)


def check_microphone(settings, assistant=None) -> dict:
    try:
        import sounddevice as sd

        devices = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    except Exception as e:
        return _result("microphone", "Microphone", "error", f"Audio system unavailable: {e}")
    if not devices:
        return _result("microphone", "Microphone", "error", "No microphone found")
    details = {"devices": len(devices), "selected": settings.mic_device or "(Windows default)"}
    if assistant is not None:
        details["listening"] = bool(assistant.recorder)
        details["muted"] = assistant.bus.flags.get("mic_muted", False)
        if assistant.recorder:
            details["trigger_level"] = round(assistant.recorder.threshold, 4)
    status = "ok" if assistant is None or assistant.recorder or assistant.mode in ("text",) else "warn"
    return _result("microphone", "Microphone", status, f"{len(devices)} input device(s)", details)


def check_stt(settings, assistant=None) -> dict:
    if assistant is not None and assistant.stt is not None:
        eng = assistant.stt.engine
        model = settings.whisper_model if eng == "whisper" else "Google (online)"
        return _result("stt", "Speech recognition", "ok", f"{eng} ready", {"engine": eng, "model": model})
    try:
        metadata.version("faster-whisper")
        ok = True
    except metadata.PackageNotFoundError:
        ok = False
    st = "info" if ok else "warn"
    return _result("stt", "Speech recognition", st, "Not loaded (text mode)" if ok else "faster-whisper not installed",
                   {"engine": settings.stt_engine})


def check_tts(settings, assistant=None) -> dict:
    eng = settings.tts_engine
    if eng == "none":
        return _result("tts", "Voice output", "info", "Voice disabled (TTS_ENGINE=none)")
    pkgs = {"edge": ["edge-tts", "miniaudio"], "pyttsx3": ["pyttsx3"]}.get(eng, [])
    missing = []
    for p in pkgs:
        try:
            metadata.version(p)
        except metadata.PackageNotFoundError:
            missing.append(p)
    try:
        import sounddevice as sd

        outs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]
    except Exception:
        outs = []
    details = {"engine": eng, "voice": settings.edge_voice if eng == "edge" else "Windows voice",
               "speakers": len(outs)}
    if assistant is not None and assistant.tts.muted:
        details["muted"] = True
    if missing:
        return _result("tts", "Voice output", "warn", f"Missing: {', '.join(missing)}", details)
    if not outs:
        return _result("tts", "Voice output", "warn", "No speaker/output device found", details)
    return _result("tts", "Voice output", "ok", f"{eng} ready", details)


def check_system() -> dict:
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        disk = shutil.disk_usage(os.path.abspath(os.sep))
        proc = psutil.Process()
        details = {"cpu_percent": cpu, "ram_percent": mem.percent, "ram_total_gb": round(mem.total / 1e9, 1),
                   "disk_free_gb": round(disk.free / 1e9, 1), "disk_percent": round(100 * disk.used / disk.total, 1),
                   "friday_ram_mb": round(proc.memory_info().rss / 1e6)}
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if bat:
            details["battery_percent"] = round(bat.percent)
        status = "warn" if cpu > 90 or mem.percent > 92 or details["disk_free_gb"] < 2 else "ok"
        return _result("system", "PC resources", status,
                       f"CPU {cpu:.0f}% · RAM {mem.percent:.0f}% · {details['disk_free_gb']} GB free", details)
    except Exception as e:
        return _result("system", "PC resources", "warn", f"Unavailable: {e}")


PACKAGES = ["openai", "anthropic", "python-dotenv", "psutil", "numpy", "sounddevice", "edge-tts", "miniaudio",
            "pyttsx3", "SpeechRecognition", "faster-whisper", "pystray", "keyboard", "Pillow"]


def check_packages() -> dict:
    from . import __version__

    versions, missing = {}, []
    for p in PACKAGES:
        try:
            versions[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            missing.append(p)
    details = {"friday": __version__, "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
               "packages": versions, "missing": missing}
    status = "ok" if not missing else "warn"
    return _result("software", "Software", status,
                   f"Python {platform.python_version()} · {len(versions)}/{len(PACKAGES)} packages" +
                   (f" (missing: {', '.join(missing)})" if missing else ""), details)


def run_all(settings, assistant=None) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    brain = getattr(assistant, "brain", None)
    jobs = {
        "internet": lambda: check_internet(),
        "api": lambda: check_api(settings, brain),
        "free_router": lambda: check_free_router(settings, brain),
        "backup": lambda: check_backup(settings, brain),
        "credits": lambda: check_abacus_credits(settings),
        "email": lambda: check_email(settings),
        "skills": lambda: check_skills(settings),
        "microphone": lambda: check_microphone(settings, assistant),
        "stt": lambda: check_stt(settings, assistant),
        "tts": lambda: check_tts(settings, assistant),
        "system": lambda: check_system(),
        "software": lambda: check_packages(),
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {k: ex.submit(f) for k, f in jobs.items()}
        out = []
        for k, fut in futures.items():
            try:
                out.append(fut.result(timeout=40))
            except Exception as e:
                out.append(_result(k, k, "error", f"Check crashed: {type(e).__name__}: {e}"))
    return out


CHECKS = {"internet": lambda s, a: check_internet(), "api": lambda s, a: check_api(s, getattr(a, "brain", None)),
          "free_router": lambda s, a: check_free_router(s, getattr(a, "brain", None)),
          "backup": lambda s, a: check_backup(s, getattr(a, "brain", None)),
          "credits": lambda s, a: check_abacus_credits(s), "email": lambda s, a: check_email(s),
          "skills": lambda s, a: check_skills(s), "microphone": check_microphone, "stt": check_stt,
          "tts": check_tts, "system": lambda s, a: check_system(), "software": lambda s, a: check_packages()}
