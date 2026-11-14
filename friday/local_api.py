"""Local API: lets other programs (phone shortcuts, scripts, Home Assistant, other AI apps) talk to Friday.

Off until you set FRIDAY_API_KEY. Listens on API_PORT (default 8766), only on this PC unless REMOTE_ACCESS=true
(then use Tailscale - see the README - never open the port on your router).
  POST /v1/ask                {"text": "..."}             -> {"reply": "..."}
  POST /v1/chat/completions   OpenAI-style messages        -> OpenAI-style answer (model "friday")
  POST /v1/listen             raw audio body (phone recording) -> {"text": "..."}   (Whisper on this PC)
  POST /v1/speak              {"text": "..."}             -> audio bytes (audio/mpeg or audio/wav) in Friday's voice
  POST /v1/ask/stream         {"text": "..."}             -> one JSON object per line: {"delta"} / {"step"} / {"final", "reply"}
  POST /v1/control            {"action": "mute_mic"}      -> {"ok": true, "message": "..."}   (see phone.CONTROL_ACTIONS)
  GET  /v1/status             credits, limits, health, switches, timers, open jobs (no secrets)
  GET  /v1/events             stays open; one JSON object per line when the state changes or an approval is waiting
  GET  /v1/health
  GET  /                      phone chat page (open in Safari/Chrome, Add to Home Screen; asks for the key once)
Every request needs the header  Authorization: Bearer <FRIDAY_API_KEY>  (or X-API-Key)."""
from __future__ import annotations

import hmac
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import activity
from .env import env, env_bool, env_int

MAX_BODY = 200_000
MAX_AUDIO = 15_000_000
AUDIO_SUFFIX = {"mp4": ".mp4", "m4a": ".m4a", "aac": ".m4a", "webm": ".webm", "ogg": ".ogg", "wav": ".wav",
                "mpeg": ".mp3", "mp3": ".mp3"}


def audio_suffix(content_type: str) -> str:
    ct = (content_type or "").lower()
    for word, suffix in AUDIO_SUFFIX.items():
        if word in ct:
            return suffix
    return ".webm"


PHONE_PAGE = Path(__file__).parent / "static" / "phone.html"

SERVICE_WORKER = """self.addEventListener('push', e => {
  let d = {title: 'Jarvis', body: '', data: {}};
  try { d = e.data.json(); } catch (x) {}
  const ap = d.data && d.data.approval;
  const opts = {body: d.body || '', icon: '/icon.svg', tag: ap ? 'jarvis-approval' : 'jarvis', renotify: true,
                requireInteraction: true, data: d.data || {}};
  if (ap) opts.actions = [{action: 'approve', title: 'Approve'}, {action: 'deny', title: 'Deny'}];  // Android / desktop
  e.waitUntil(self.registration.showNotification(d.title || 'Jarvis', opts));
});
self.addEventListener('notificationclick', e => {
  const n = e.notification, ap = n.data && n.data.approval;
  n.close();
  if (ap && (e.action === 'approve' || e.action === 'deny')) {   // answered straight from the notification
    e.waitUntil(fetch('/v1/approve', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: ap.id, token: ap.token, approve: e.action === 'approve'})})
      .then(r => self.registration.showNotification(r.ok ? (e.action === 'approve' ? 'Approved' : 'Denied') : 'Too late',
        {body: r.ok ? 'Jarvis got your answer.' : 'That request is no longer waiting.', icon: '/icon.svg', tag: 'jarvis-answer'}))
      .catch(() => self.registration.showNotification("Couldn't reach Jarvis", {body: 'Open Jarvis and answer there.', icon: '/icon.svg'})));
    return;
  }
  // iPhone shows no buttons on web notifications: tapping opens Jarvis with Approve / Deny right on screen
  e.waitUntil(clients.matchAll({type: 'window'}).then(l => {
    const url = ap ? '/?approval=1' : '/';
    if (l.length) { l[0].postMessage({approval: !!ap}); return l[0].focus(); }
    return clients.openWindow(url);
  }));
});
"""
MANIFEST = {"name": "Jarvis", "short_name": "Jarvis", "start_url": "/", "display": "standalone",
            "background_color": "#0b1220", "theme_color": "#0b1220",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" '
            'fill="#0b1220"/><circle cx="50" cy="50" r="28" fill="none" stroke="#4fd1ff" stroke-width="6"/>'
            '<circle cx="50" cy="50" r="10" fill="#4fd1ff"/></svg>')


def make_server(ask, key: str, host: str = "127.0.0.1", port: int = 8766, speak=None,
                listen=None, dashboard_link=None, approval=None, approve=None, live=None,
                ask_stream=None, control=None, status=None, events=None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Friday/2.0"

        def log_message(self, *a):  # keep the console quiet
            pass

        def handle(self):  # the phone pressed Stop / locked its screen mid-reply: not an error
            try:
                super().handle()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, code: int, data: bytes, mime: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _open_stream(self):
            """Start a line-by-line reply (one JSON object per line). Returns send(obj), safe to call from any thread."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            lock = threading.Lock()

            def send(obj: dict) -> None:
                with lock:
                    self.wfile.write(json.dumps(obj).encode() + b"\n")
                    self.wfile.flush()

            return send

        def _ask_stream(self, text: str):
            send = self._open_stream()
            try:
                send({"final": True, **ask_stream(text, send)})
            except (ConnectionError, OSError):
                pass  # the phone pressed Stop or lost its connection
            except Exception as e:
                try:
                    send({"error": f"{type(e).__name__}: {e}"})
                except OSError:
                    pass

        def _events(self):
            next_event, close = events()
            try:
                send = self._open_stream()
                send({"type": "approval", "pending": approval() if approval else None})
                while True:
                    send(next_event(15.0) or {"type": "ping"})
            except (ConnectionError, OSError):
                pass  # the phone closed the page
            finally:
                close()

        def _listen(self, n: int):
            if listen is None:
                self.rfile.read(min(max(n, 0), 65536))  # read the upload first so the sender sees our answer, not a reset
                return self._send(501, {"error": "voice input is not available"})
            if n > MAX_AUDIO:
                self.rfile.read(min(n, 65536))  # take a little of the upload so the sender sees our answer, not a reset
                return self._send(413, {"error": "recording too long"})
            if n <= 0:
                return self._send(400, {"error": "no audio"})
            data = self.rfile.read(n)
            fd, path = tempfile.mkstemp(suffix=audio_suffix(self.headers.get("Content-Type", "")))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                text = (listen(path) or "").strip()
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            activity.log("api", f"Phone voice from {self.client_address[0]}: {text[:120]}")
            self._send(200, {"text": text})

        def _authed(self) -> bool:
            given = self.headers.get("X-API-Key") or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if key and given and hmac.compare_digest(given.encode(), key.encode()):
                return True
            activity.log("api", f"Rejected API request from {self.client_address[0]} (bad key)")
            self._send(401, {"error": "missing or wrong API key"})
            return False

        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/sw.js":  # the service worker shows push notifications even when the page is closed
                return self._send_bytes(200, SERVICE_WORKER.encode(), "application/javascript")
            if route == "/manifest.webmanifest":  # lets the iPhone "Add to Home Screen" as an app (needed for push)
                return self._send_bytes(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
            if route == "/icon.svg":
                return self._send_bytes(200, ICON_SVG.encode(), "image/svg+xml")
            if self.path.split("?")[0] in ("/", "/phone"):  # the phone chat page itself holds no secrets
                data = PHONE_PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            if not self._authed():
                return
            if self.path.rstrip("/") == "/v1/dashboard-link":  # the phone page's "Dashboard" button
                link = dashboard_link() if dashboard_link else ""
                if not link:
                    return self._send(404, {"error": "dashboard link isn't set up (see the Dashboard on phone steps)"})
                return self._send(200, {"url": link})
            if route.rstrip("/") == "/v1/push/key":
                from . import notify

                return self._send(200, {"key": notify.public_key()})
            if route.rstrip("/") == "/v1/brain":  # which AI is thinking + what it is doing right now (phone page)
                try:
                    return self._send(200, live() if live else {})
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            if route.rstrip("/") == "/v1/status":  # credits, limits, health, switches, timers, jobs (phone Status tab)
                if status is None:
                    return self._send(501, {"error": "status is not available"})
                try:
                    return self._send(200, status())
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            if route.rstrip("/") == "/v1/events":  # stays open: state changes and approvals reach the phone at once
                if events is None:
                    return self._send(501, {"error": "live events are not available"})
                return self._events()
            if route.rstrip("/") == "/v1/approval":  # what is waiting for a yes/no right now (the page shows buttons)
                return self._send(200, {"pending": approval() if approval else None})
            if route.rstrip("/") == "/v1/notifications":
                from . import notify

                try:
                    since = int(self.path.split("since=")[1].split("&")[0]) if "since=" in self.path else 0
                except ValueError:
                    since = 0
                return self._send(200, {"items": notify.recent(since)})
            if self.path.rstrip("/") == "/v1/health":
                return self._send(200, {"ok": True, "name": "Friday"})
            if self.path.rstrip("/") == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": "friday", "object": "model"}]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            path = self.path.split("?")[0].rstrip("/")
            if path == "/v1/approve":  # the phone notification's Approve/Deny buttons: no API key, but a one-time token
                try:
                    body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "body must be JSON"})
                if approve is None or not approve(str(body.get("id", "")), str(body.get("token", "")),
                                                  bool(body.get("approve"))):
                    activity.log("api", f"Rejected an approval answer from {self.client_address[0]} (no such waiting request)")
                    return self._send(409, {"error": "that request isn't waiting any more"})
                activity.log("api", f"Approval answered from the phone: {'approved' if body.get('approve') else 'denied'}")
                return self._send(200, {"ok": True})
            if not self._authed():
                return
            if path == "/v1/listen":
                return self._listen(n)
            if n > MAX_BODY:
                self.rfile.read(min(n, 65536))
                return self._send(413, {"error": "request too big"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "body must be JSON"})
            if path == "/v1/push/subscribe":
                from . import notify

                if not notify.subscribe(body):
                    return self._send(400, {"error": "not a valid push subscription"})
                return self._send(200, {"ok": True})
            if path == "/v1/push/test":
                from . import notify

                notify.push("Jarvis", "Notifications are working.", text_too=False)
                return self._send(200, {"ok": True})
            if path == "/v1/speak":
                if speak is None:
                    return self._send(501, {"error": "voice output is not available"})
                text = str(body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "no text"})
                try:
                    audio, mime = speak(text)
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
                if not audio:
                    return self._send(500, {"error": "no voice available (TTS is off or failed)"})
                return self._send_bytes(200, audio, mime or "application/octet-stream")
            if path == "/v1/control":  # the phone's remote: mute, stop talking, brain mode, restart
                if control is None:
                    return self._send(501, {"error": "controls are not available"})
                try:
                    msg = control(str(body.get("action") or ""))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
                activity.log("api", f"Phone control from {self.client_address[0]}: {str(body.get('action'))[:40]}")
                return self._send(200, {"ok": True, "message": msg})
            if path in ("/v1/ask", "/v1/ask/stream"):
                text = str(body.get("text") or "").strip()
            elif path == "/v1/chat/completions":
                msgs = [m for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") == "user"]
                content = msgs[-1].get("content", "") if msgs else ""
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = str(content).strip()
            else:
                return self._send(404, {"error": "not found"})
            if not text:
                return self._send(400, {"error": "no text"})
            activity.log("api", f"API request from {self.client_address[0]}: {text[:120]}")
            if path == "/v1/ask/stream":
                if ask_stream is None:
                    return self._send(501, {"error": "streaming is not available"})
                return self._ask_stream(text)
            try:
                reply = ask(text)
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            if path == "/v1/ask":
                return self._send(200, {"reply": reply})
            self._send(200, {"id": f"friday-{int(time.time() * 1000)}", "object": "chat.completion",
                             "created": int(time.time()), "model": "friday",
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": reply}}]})

    return ThreadingHTTPServer((host, port), Handler)


def start(ask, speak=None, listen=None, dashboard_link=None, approval=None, approve=None,
          live=None, **phone) -> ThreadingHTTPServer | None:
    key = env("FRIDAY_API_KEY")
    if not key or not env_bool("API_ENABLED", True):
        return None
    if len(key) < 16:
        print("[api] FRIDAY_API_KEY is too short (use 16+ characters) - local API not started.")
        return None
    host = "0.0.0.0" if env_bool("REMOTE_ACCESS", False) else "127.0.0.1"
    port = env_int("API_PORT", 8766)
    try:
        srv = make_server(ask, key, host, port, speak=speak, listen=listen, dashboard_link=dashboard_link,
                          approval=approval, approve=approve, live=live, **phone)
    except OSError as e:
        print(f"[api] could not start on port {port}: {e}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="friday-api").start()
    print(f"[api] Friday API on http://{'this-pc' if host == '0.0.0.0' else '127.0.0.1'}:{port}/v1")
    return srv
