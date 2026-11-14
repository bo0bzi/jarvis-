"""Local web dashboard (standard library only - nothing extra to install).

Security: binds to 127.0.0.1 only, checks the Host header (blocks DNS-rebinding), and every API call needs a
secret token (sent as an HttpOnly SameSite=Strict cookie after opening the login link, or an X-Friday-Token header).
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import tools as T

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "friday_token"


def read_token(data_dir) -> str:
    p = Path(data_dir) / "dashboard_token.txt"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_or_create_token(data_dir) -> str:
    tok = read_token(data_dir)
    if len(tok) >= 20:
        return tok
    tok = secrets.token_urlsafe(24)
    p = Path(data_dir) / "dashboard_token.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok, encoding="utf-8")
    return tok


def _timers(ctx) -> list[dict]:
    return [{"id": t.get("id"), "message": t["message"], "due": t["due"].isoformat(timespec="seconds")}
            for t in ctx.timers]



def v2_overview(a) -> dict:
    """Shop board, credits, SMS, routines, knowledge and activity for the 'Shop & Friday' tab. Local data only."""
    from . import activity, autonomy, backup, credits, knowledge, routines, shop, sms

    con, _ = shop._db(getattr(a, "ctx", None))
    jobs = [dict(r) for r in con.execute(
        f"SELECT id, customer, vehicle, work, status, quote, due, updated FROM jobs WHERE status IN "
        f"({','.join('?' * len(shop.OPEN))}) ORDER BY id", shop.OPEN).fetchall()]
    parts = [dict(r) for r in con.execute("SELECT part, job_id, remind_on FROM parts WHERE done=0 ORDER BY remind_on")]
    con.close()
    rep = routines._report_path()
    man = knowledge._manifest()
    return {"jobs": jobs, "parts": parts, "credits": credits.get_gate().status(), "sms": sms.get_link().status(),
            "autonomy": {"level": autonomy.level(), "text": autonomy.describe()},
            "routines": routines._load(), "nightly_report": rep.read_text(encoding="utf-8")[-4000:] if rep.exists() else "",
            "knowledge": {"documents": len(man), "semantic": sum(1 for m in man.values() if m.get("semantic")),
                          "folder": str(knowledge.knowledge_dir())},
            "backups": [p.name for p in backup.list_all()[:10]],
            "activity": activity.recent(80)[::-1]}


def hud_data(a) -> dict:
    """Numbers for the HUD rings (dashboard + phone). Every part fails soft: a missing sensor shows as None."""
    import subprocess

    from . import brainmode, claude_tiers

    out: dict = {"cpu": None, "ram": None, "gpu": None, "vram": None, "brain_mode": brainmode.get(),
                 "route": getattr(a.brain, "last_route", "") or "main", "model": a.s.model,
                 "claude": {"on": claude_tiers.enabled(), "used": 0, "cap": 0},
                 "next_event": "", "timers": len(a.ctx.timers)}
    try:
        import psutil

        out["cpu"] = round(psutil.cpu_percent(interval=None))
        out["ram"] = round(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        util, used, total = [float(x) for x in r.stdout.strip().splitlines()[0].split(",")]
        out["gpu"], out["vram"] = round(util), round(100 * used / total) if total else None
    except Exception:
        pass
    try:
        out["claude"]["used"] = sum(claude_tiers.used_today(a.s, t) for t in claude_tiers.TIERS)
        out["claude"]["cap"] = sum(claude_tiers.cap_for(t) for t in claude_tiers.TIERS)
    except Exception:
        pass
    try:
        from datetime import datetime, timedelta

        from . import agenda

        now = datetime.now()
        evs = [e for e in agenda.events_between(now, now + timedelta(days=2), getattr(a.ctx, "data_dir", None))
               if not e["all_day"] and e["source"] != "error" and e["start"] >= now]
        if evs:
            out["next_event"] = f"{evs[0]['start']:%a %I:%M %p}".replace(" 0", " ") + " " + evs[0]["title"]
    except Exception:
        pass
    return out


def limits_data(a) -> dict:
    """Usage-vs-limit meters for every cloud brain: [{name, used, cap, unit, note}]. Fails soft (a bad source is skipped)."""
    from . import claude_tiers

    rows: list[dict] = []
    brain = getattr(a, "brain", None)
    s = getattr(a, "s", None)

    def add(name, used, cap, note=""):
        rows.append({"name": name, "used": int(used), "cap": int(cap), "note": note})

    try:
        if brain is not None and getattr(brain, "free_router", None) is not None:
            add("Free cloud (freellmapi)", brain.free_used_today(), s.free_router_max_per_day, "requests today")
    except Exception:
        pass
    try:
        if claude_tiers.enabled():
            for t in claude_tiers.TIERS:
                add(f"Claude {t.title()}", claude_tiers.used_today(s, t), claude_tiers.cap_for(t), "calls today")
            add("Claude this month", claude_tiers.used_month(s), claude_tiers.month_cap(), "calls this month (hard stop)")
        else:
            rows.append({"name": "Claude", "used": 0, "cap": 0, "note": "off: add ANTHROPIC_API_KEY in Settings"})
    except Exception:
        pass
    try:
        if brain is not None and getattr(brain, "backup", None) is not None:
            if "abacus" in str(getattr(brain.backup, "label", "")).lower():
                add("Abacus", brain.backup_used_today(), 0, "requests today. No limit; I warn every 1,000 credits")
            else:
                add("Backup AI", brain.backup_used_today(), s.backup_max_per_day, "requests today")
    except Exception:
        pass
    return {"rows": rows, "credits": credit_summary()}


def credit_summary(days: int = 14) -> dict:
    """Credits spent today / this month against the caps, the no-ask allowance, and the last `days` days. Fails soft."""
    try:
        from datetime import datetime, timedelta

        from . import credits
        from .env import env_int

        gate = credits.get_gate()
        day_cap, month_cap = gate.caps()
        today = datetime.now()
        spent = gate.data.get("days", {})
        return {"spent_today": gate.spent_today(), "review_today": gate.review_today(), "abacus_today": gate.abacus_today(),
                "abacus_total": round(gate.data.get("abacus_total", 0.0), 1),
                "abacus_warn_every": env_int("ABACUS_WARN_EVERY", 1000), "daily_cap": day_cap,
                "spent_month": gate.spent_month(),
                "monthly_cap": month_cap, "allowance": env_int("CREDIT_PREAPPROVED_PER_DAY", 0),
                "allowance_used": round(gate.data.get("preapproved_used", {}).get(today.strftime("%Y-%m-%d"), 0), 1),
                "days": [{"date": d, "spent": round(spent.get(d, 0.0), 1),
                          "review": round(gate.data.get("review_days", {}).get(d, 0.0), 1),
                          "abacus": round(gate.data.get("abacus_days", {}).get(d, 0.0), 1)} for d in
                         ((today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1))]}
    except Exception:
        return {}


class DashboardServer:
    def __init__(self, assistant, port: int = 8765, host: str = "127.0.0.1"):
        self.a = assistant
        self.host = host
        self.requested_port = port
        self.token = get_or_create_token(assistant.s.data_dir)
        self.httpd: ThreadingHTTPServer | None = None
        self.port = port

    @property
    def login_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        handler = _make_handler(self)
        last = None
        for attempt in range(10):  # the previous Friday may still be releasing the port after a restart
            try:
                self.httpd = ThreadingHTTPServer((self.host, self.requested_port), handler)
                break
            except OSError as e:
                last = e
                time.sleep(0.5)
        if self.httpd is None:
            raise OSError(f"port {self.requested_port} is busy ({last}). Change DASHBOARD_PORT in .env.")
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="dashboard").start()

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass

    # ------------------------------------------------------------------ API implementations
    def state(self) -> dict:
        a = self.a
        snap = a.bus.snapshot()
        snap.update({
            "assistant": {"name": a.s.assistant_name, "user": a.s.user_name, "mode": a.mode,
                          "conversation_mode": a.s.conversation_mode, "followup_seconds": a.s.followup_seconds,
                          "wake_words": a.s.wake_words, "provider": a.s.provider, "model": a.s.model,
                          "version": __import__("friday").__version__,
                          "mic_available": a.recorder is not None, "voice_muted": a.tts.muted,
                          "problems": a.s.validate()},
            "usage": a.usage.summary(),
            "checks": list(a.last_checks.values()),
            "timers": _timers(a.ctx),
            "server_time": time.time(),
        })
        return snap

    def hud(self) -> dict:
        return hud_data(self.a)

    def tools(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "dangerous": t.dangerous,
                 "source": getattr(t, "source", "") or "", "enabled": t.name not in T.DISABLED, **T.STATS.get(t.name, {"calls": 0, "errors": 0})}
                for t in T.REGISTRY.values()]

    def set_tool(self, name: str, enabled: bool) -> dict:
        if name not in T.REGISTRY:
            raise KeyError(name)
        disabled = set(T.DISABLED)
        (disabled.discard if enabled else disabled.add)(name)
        from .settings_store import EnvStore

        EnvStore().write({"DISABLED_TOOLS": ",".join(sorted(disabled))})
        T.DISABLED.clear()
        T.DISABLED.update(disabled)
        self.a.s.disabled_tools = sorted(disabled)
        self.a.bus.log_event("tools", f"{name} {'enabled' if enabled else 'disabled'}")
        self.a._refresh_agents()
        return {"ok": True}

    def mics(self) -> dict:
        try:
            from .voice import list_microphone_devices

            return {"devices": list_microphone_devices(), "selected": self.a.s.mic_device}
        except Exception as e:
            return {"devices": [], "selected": self.a.s.mic_device, "error": str(e)}

    def calibrate(self) -> dict:
        rec = self.a.recorder
        if rec is None:
            return {"ok": False, "error": "Microphone is not running."}
        # The listener holds an input stream; opening a second one works on Windows (WASAPI shared mode).
        try:
            ambient = rec.calibrate()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        from .voice import suggest_trigger

        suggested = suggest_trigger(ambient, getattr(rec, "ambient_p90", 0.0))
        self.a.bus.log_event("voice_input", f"Calibrated: noise {ambient:.4f}, suggested threshold {suggested}")
        return {"ok": True, "ambient": round(ambient, 5), "suggested_threshold": suggested,
                "current_trigger": round(rec._trigger(), 4)}

    def test_voice(self, voice: str = "", engine: str = "") -> dict:
        from types import SimpleNamespace

        from .voice import TextToSpeech

        s = self.a.s
        tmp = SimpleNamespace(**{f: getattr(s, f) for f in s.__dataclass_fields__})
        if voice:
            tmp.edge_voice = voice
        if engine:
            tmp.tts_engine = engine
        tts = TextToSpeech(tmp)
        threading.Thread(target=tts.speak, args=(f"Hello{', ' + s.user_name if s.user_name else ''}. "
                                                 f"This is how I sound.",), daemon=True).start()
        return {"ok": True}


# ---------------------------------------------------------------------- HTTP handler
def _make_handler(server: DashboardServer):
    a = server.a

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FridayDashboard"

        def log_message(self, fmt, *args):  # keep the console clean
            pass

        # ---------------------------------------------------------- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if host in ("127.0.0.1", "localhost", "[::1]"):
                return True
            # Phone access through Tailscale (https://<pc>.<tailnet>.ts.net:8443): only when REMOTE_ACCESS is on.
            # Tailscale Serve keeps it private to your own devices, and the secret token is still required.
            from .env import env, env_bool

            if env_bool("REMOTE_ACCESS", False) and host.endswith(".ts.net"):
                return True
            return host in [h.strip().lower() for h in env("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]

        def _token_ok(self) -> bool:
            if secrets.compare_digest(self.headers.get("X-Friday-Token", ""), server.token):
                return True
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return COOKIE in c and secrets.compare_digest(c[COOKIE].value, server.token)

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, default=str).encode("utf-8"))

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                self.rfile.read(min(n, 65536))  # take some of the upload so the sender sees our answer, not a reset
                raise ValueError("body too large")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        # ---------------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"Forbidden host", "text/plain")
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                t = parse_qs(url.query).get("t", [""])[0]
                if t:
                    if secrets.compare_digest(t, server.token):
                        return self._send(302, b"", "text/plain", {
                            "Location": "/",
                            "Set-Cookie": f"{COOKIE}={server.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
                    return self._send(403, b"Wrong dashboard link. Use the link printed in the Friday window.",
                                      "text/plain")
                if not self._token_ok():
                    return self._send(401, LOCKED_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return self._send(200, (STATIC_DIR / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/health":  # unauthenticated liveness probe, reveals nothing
                return self._json({"ok": True})
            if not self._token_ok():
                return self._json({"error": "unauthorized"}, 401)
            try:
                if path == "/api/state":
                    return self._json(server.state())
                if path == "/api/events":
                    return self._sse()
                if path == "/api/hud":
                    return self._json(server.hud())
                if path == "/api/limits":
                    return self._json(limits_data(server.a))
                if path == "/api/settings":
                    from .settings_store import EnvStore

                    return self._json({"fields": EnvStore().public_view()})
                if path == "/api/mics":
                    return self._json(server.mics())
                if path == "/api/tools":
                    return self._json({"tools": server.tools()})
                if path == "/api/memory":
                    return self._json({"messages": a.memory.messages[-100:], "facts": a.memory.facts})
                if path == "/api/lessons":
                    from . import lessons

                    return self._json({"lessons": lessons.load(a.ctx)})
                if path == "/api/evolve":
                    from . import evolve

                    return self._json({"report": evolve.last_report(), "running": bool(getattr(a, "_evolving", False))})
                if path == "/api/projects":  # background projects: what is running, waiting, finished
                    from . import projects
                    from .env import env_int

                    return self._json({"projects": projects.MANAGER.snapshot(), "max": max(1, env_int("PROJECTS_MAX_PARALLEL", 3))})
                if path == "/api/updates":  # what Jarvis told you without being asked
                    from . import updates

                    return self._json({"updates": updates.recent(15), "quiet_now": updates.quiet_now()})
                if path == "/api/playbooks":  # what the local AI learned from the online AI + the last full review
                    from . import blackbox, distill, playbooks

                    return self._json({"playbooks": playbooks.load(), "report": distill.last_report(),
                                       "running": distill.running(), "record": blackbox.stats(24)})
                if path == "/api/usage":
                    return self._json(a.usage.summary())
                if path == "/api/v2":
                    return self._json(v2_overview(a))
                if path == "/api/checks":
                    from . import checks

                    results = {k: v for k, v in a.last_checks.items()}
                    results["skills"] = checks.check_skills(a.s)  # cheap + local: always show what it knows RIGHT NOW
                    return self._json({"results": list(results.values())})
                if path == "/api/notes":
                    from .tools import _notes_path

                    p = _notes_path(a.ctx)
                    return self._json({"notes": p.read_text(encoding="utf-8") if p.exists() else ""})
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"error": "not found"}, 404)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = a.bus.subscribe()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while a.running:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode("utf-8"))
                    except Exception:  # queue.Empty -> heartbeat
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                a.bus.unsubscribe(q)
                self.close_connection = True

        # ---------------------------------------------------------- POST
        def do_POST(self):
            if not self._host_ok() or not self._token_ok():
                try:  # read the upload first, otherwise the sender sometimes sees a connection reset instead of our answer
                    self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
                except (ValueError, OSError):
                    pass
                if not self._host_ok():
                    return self._send(403, b"Forbidden host", "text/plain")
                return self._json({"error": "unauthorized"}, 401)
            path = urlparse(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": f"bad request: {e}"}, 400)
            try:
                return self._route_post(path, body)
            except KeyError as e:
                return self._json({"error": f"not found: {e}"}, 404)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _route_post(self, path: str, body: dict):
            if path == "/api/chat":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._json({"error": "empty message"}, 400)
                a.inputs.put("\x00DASH" + text[:4000])
                return self._json({"ok": True})
            if path == "/api/evolve":  # "Learn from today now": runs in the background, report appears when done
                from . import evolve

                if getattr(a, "_evolving", False):
                    return self._json({"started": False, "running": True})

                def job():
                    a._evolving = True
                    try:
                        a.bus.log_event("evolve", "Learning session started")
                        evolve.run(a.ctx, a.brain)
                        a.bus.log_event("evolve", "Learning session finished")
                    except Exception as e:
                        a.bus.log_event("evolve", f"Learning session failed: {e}", level="error")
                    finally:
                        a._evolving = False
                        a._refresh_agents()
                        a.run_checks("skills")

                threading.Thread(target=job, daemon=True, name="evolve").start()
                return self._json({"started": True})
            if path == "/api/projects":  # start a background project from the dashboard, or cancel one
                from . import projects

                if body.get("cancel") is not None:
                    return self._json({"message": projects.MANAGER.cancel(body["cancel"]), "projects": projects.MANAGER.snapshot()})
                try:
                    p = projects.MANAGER.start(a.ctx, str(body.get("title", "")), str(body.get("request", "")),
                                               bool(body.get("needs_screen")))
                except (ValueError, RuntimeError) as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"started": p.id, "projects": projects.MANAGER.snapshot()})
            if path == "/api/distill":  # "Review everything now": runs in the background, report appears when done
                from . import distill

                if distill.running():
                    return self._json({"started": False, "running": True})

                def review_job():
                    try:
                        a.bus.log_event("distill", "Full review started")
                        distill.run(a.ctx, a.brain)
                        a.bus.log_event("distill", "Full review finished")
                    except Exception as e:
                        a.bus.log_event("distill", f"Full review failed: {e}", level="error")

                threading.Thread(target=review_job, daemon=True, name="distill").start()
                return self._json({"started": True})
            if path == "/api/playbooks":
                from . import playbooks

                if body.get("delete"):
                    return self._json({"removed": playbooks.remove(str(body["delete"])), "playbooks": playbooks.load()})
                if "enabled" in body:
                    it = playbooks.update(int(body["id"]), enabled=bool(body["enabled"]))
                    return self._json({"ok": it is not None, "playbooks": playbooks.load()})
                return self._json({"error": "nothing to do"}, 400)
            if path == "/api/lessons":
                from . import lessons

                if body.get("resume"):  # a rule the night-time check paused because it was not helping
                    lessons.set_paused(str(body["resume"]), False, "", a.ctx)
                    return self._json({"lessons": lessons.load(a.ctx)})
                if body.get("delete"):
                    return self._json({"removed": lessons.remove(str(body["delete"]), a.ctx),
                                       "lessons": lessons.load(a.ctx)})
                it = lessons.add(str(body.get("title", "")), str(body.get("how", "")), str(body.get("when", "")), a.ctx)
                a.bus.log_event("lessons", f"Taught lesson #{it['id']}: {it['title']}")
                return self._json({"lesson": it, "lessons": lessons.load(a.ctx)})
            if path == "/api/confirm":
                ok = a.answer_confirm(str(body.get("id", "")), bool(body.get("approve")))
                return self._json({"ok": ok}, 200 if ok else 409)
            if path == "/api/control":
                action = str(body.get("action", ""))
                if action.startswith("brain:"):  # auto / local / online, same switch as "go local" by voice
                    from . import phone

                    try:
                        return self._json({"ok": True, "message": phone.control(a, action)})
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                return self._json({"ok": True, "message": a.control(action)})
            if path == "/api/settings":
                from .settings_store import EnvStore

                updates = body.get("updates") or {}
                if not isinstance(updates, dict):
                    return self._json({"error": "updates must be an object"}, 400)
                try:
                    result = EnvStore().write({str(k): ("" if v is None else str(v)) for k, v in updates.items()})
                except ValueError as e:
                    return self._json({"error": "invalid values", "fields": e.args[0]}, 400)
                applied = a.apply_settings(result["apply"]) if result["changed"] else {"restart_needed": False, "notes": []}
                a.bus.log_event("settings", f"Saved: {', '.join(result['changed']) or 'no changes'}")
                return self._json({"ok": True, **result, **applied})
            if path == "/api/checks/run":
                only = body.get("id")
                from .checks import CHECKS

                if only and only not in CHECKS:
                    return self._json({"error": "unknown check"}, 404)
                return self._json({"results": a.run_checks(only)})
            if path == "/api/tools":
                return self._json(server.set_tool(str(body.get("name")), bool(body.get("enabled"))))
            if path == "/api/memory/clear":
                a.memory.clear()
                a.bus.log_event("memory", "Conversation history cleared from the dashboard.")
                return self._json({"ok": True})
            if path == "/api/memory/fact":
                fact = str(body.get("fact", "")).strip()
                if body.get("delete"):
                    if fact in a.memory.facts:
                        a.memory.facts.remove(fact)
                        a.memory.save()
                elif fact:
                    a.memory.remember(fact)
                return self._json({"ok": True, "facts": a.memory.facts})
            if path == "/api/timers/cancel":
                return self._json({"ok": T.cancel_timer(a.ctx, str(body.get("id", "")))})
            if path == "/api/calibrate":
                return self._json(server.calibrate())
            if path == "/api/test-voice":
                return self._json(server.test_voice(str(body.get("voice", "")), str(body.get("engine", ""))))
            return self._json({"error": "not found"}, 404)

    return Handler


LOCKED_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Friday dashboard</title>
<style>body{background:#05080f;color:#9fe8ff;font-family:Segoe UI,Arial,sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}div{max-width:520px;text-align:center;line-height:1.6}</style></head>
<body><div><h2>Friday dashboard is locked</h2><p>Open it from the Friday tray icon (<b>Open dashboard</b>),
say <b>"open the dashboard"</b>, or double-click <b>open_dashboard.bat</b> in the Friday folder.</p></div></body></html>"""
