"""Text Friday from your phone - through Quo (formerly OpenPhone).

Setup (Dashboard > Settings > SMS (Quo)):
  QUO_API_KEY        - Quo app > Settings > API > create key (stays in .env, never shown).
  FRIDAY_SMS_NUMBER  - the Quo number Friday texts from, e.g. +19055551234 (a dedicated number is best;
                       your shop's existing number also works). Or QUO_PHONE_NUMBER_ID (PN...).
  OWNER_PHONE        - YOUR cell number. Friday only ever answers this number; everyone else is ignored.
  SMS_PIN            - 4+ digit PIN. Risky actions and credit approvals by text need "YES <PIN>".
  SMS_ENABLED=true
How it works: Friday checks Quo for new texts every SMS_POLL_SECONDS (default 20). No ports are opened on your
PC and nothing from the internet can connect in. Photos you text are looked at by the LOCAL vision model.
"""
from __future__ import annotations

import base64
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import activity
from .env import data_dir, env, env_bool, env_int, ollama_url
from .tools import tool

API = "https://api.openphone.com/v1"
YES_RE = re.compile(r"^\s*(yes|y|approve|ok)\b\W*(\d{3,8})?\s*$", re.I)
NO_RE = re.compile(r"^\s*(no|n|deny|cancel|stop)\b", re.I)


def norm_phone(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if digits else ""


class SmsLink:
    def __init__(self, on_message=None, opener=None):
        self.on_message = on_message  # callable(text, images:list[bytes]) -> reply str
        self.opener = opener or urllib.request.urlopen  # replaceable in tests
        self.pending: dict | None = None  # {"id", "prompt", "answer": None|bool, "event"}
        self.last_error = ""
        self.last_poll = 0.0
        self._number_id = ""
        self._lock = threading.Lock()  # one approval question at a time
        self._poll_lock = threading.RLock()  # the poll loop and approval waits never read the same text twice
        self.state = self._load()

    # ------------------------------------------------------------------ config
    @staticmethod
    def configured() -> bool:
        return bool(env_bool("SMS_ENABLED", False) and env("QUO_API_KEY") and env("OWNER_PHONE")
                    and (env("QUO_PHONE_NUMBER_ID") or env("FRIDAY_SMS_NUMBER")))

    @property
    def owner(self) -> str:
        return norm_phone(env("OWNER_PHONE"))

    def _path(self):
        return data_dir() / "sms_state.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state["seen"] = self.state.get("seen", [])[-300:]
        try:
            self._path().write_text(json.dumps(self.state), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ HTTP
    def _api(self, method: str, path: str, params: list | None = None, body: dict | None = None) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": env("QUO_API_KEY"), "Content-Type": "application/json", "User-Agent": "Friday"})
        try:
            with self.opener(req, timeout=20) as r:
                raw = r.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else ""
            raise RuntimeError(f"Quo said {e.code}: {detail}") from None
        return json.loads(raw)

    def number_id(self) -> str:
        if env("QUO_PHONE_NUMBER_ID"):
            return env("QUO_PHONE_NUMBER_ID")
        if not self._number_id:
            want = norm_phone(env("FRIDAY_SMS_NUMBER"))
            for n in self._api("GET", "/phone-numbers").get("data", []):
                if norm_phone(n.get("number") or n.get("phoneNumber") or "") == want:
                    self._number_id = n["id"]
                    break
            if not self._number_id:
                raise RuntimeError(f"Quo has no number {want} on this account (check FRIDAY_SMS_NUMBER).")
        return self._number_id

    # ------------------------------------------------------------------ send
    def send(self, to: str, text: str) -> str:
        to = norm_phone(to)
        if not to:
            return "Error: no phone number."
        text = (text or "").strip() or "(empty)"
        parts = [text[i:i + 1500] for i in range(0, min(len(text), 4500), 1500)]
        for part in parts:
            self._api("POST", "/messages", body={"content": part, "from": self.number_id(), "to": [to]})
        activity.log("sms", f"Texted {to[-4:].rjust(len(to), '*')}: {text[:120]}")
        return f"Text sent to {to}."

    def text_owner(self, text: str) -> str:
        return self.send(self.owner, text)

    # ------------------------------------------------------------------ receive
    def fetch_new(self) -> list[dict]:
        if "since" not in self.state:  # first run: don't answer old history
            self.state["since"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            self._save()
            return []
        params = [("phoneNumberId", self.number_id()), ("participants[]", self.owner), ("maxResults", 20),
                  ("createdAfter", self.state["since"])]
        items = self._api("GET", "/messages", params).get("data", [])
        seen = set(self.state.get("seen", []))
        new = [m for m in items if m.get("id") not in seen and m.get("direction") in ("incoming", "inbound")]
        new.sort(key=lambda m: m.get("createdAt", ""))
        for m in items:
            seen.add(m.get("id"))
        self.state["seen"] = list(seen)
        if items:
            newest = max(m.get("createdAt", "") for m in items)
            try:  # step back 1s-safe: keep 'since' a bit behind the newest message, ids de-duplicate
                t = datetime.fromisoformat(newest.replace("Z", "+00:00")) - timedelta(seconds=60)
                self.state["since"] = t.isoformat(timespec="seconds").replace("+00:00", "Z")
            except ValueError:
                pass
        self._save()
        return new

    def handle_incoming(self, m: dict) -> str | None:
        sender = norm_phone(m.get("from", ""))
        text = (m.get("text") or m.get("body") or m.get("content") or "").strip()
        if sender != self.owner:
            activity.log("sms", f"Ignored a text from an unknown number ending {sender[-4:]}")
            return None
        activity.log("sms", f"Text from you: {text[:120]}")
        if self.pending is not None:
            y, n = YES_RE.match(text), NO_RE.match(text)
            if y or n:
                if y:
                    pin = env("SMS_PIN")
                    if not pin:
                        return ("Approvals by text need a PIN. Set SMS_PIN in .env (4+ digits), then reply "
                                "YES <PIN> - or approve on the dashboard.")
                    if not hmac.compare_digest((y.group(2) or ""), pin):
                        return "Wrong or missing PIN. Reply YES followed by your PIN, e.g. YES 1234 - or NO."
                self.pending["answer"] = bool(y)
                self.pending["event"].set()
                return "Approved." if y else "Okay, cancelled."
        elif YES_RE.match(text) and not text.lower().startswith("ok "):
            return "Nothing is waiting for approval right now."
        images = []
        for media in m.get("media") or []:
            if str(media.get("type", "")).startswith("image") and media.get("url"):
                try:
                    with self.opener(urllib.request.Request(media["url"], headers={"User-Agent": "Friday"}),
                                     timeout=30) as r:
                        images.append(r.read())
                except Exception as e:
                    activity.log("error", f"sms: couldn't download a photo: {e}")
        if not text and not images:
            return None
        if self.on_message is None:
            return "Friday is starting up, try again in a minute."
        return self.on_message(text, images)

    def poll_once(self) -> int:
        # Fetch under a lock (so two pollers never get the same text), but handle outside it: handling a text
        # can ask for approval, and the approval wait must be able to poll for the "YES <PIN>" reply.
        with self._poll_lock:
            self.last_poll = time.time()
            msgs = self.fetch_new()
        for m in msgs:
            try:
                reply = self.handle_incoming(m)
            except Exception as e:
                reply = f"Sorry, something went wrong: {str(e)[:150]}"
                activity.log("error", f"sms handling failed: {e}")
            if reply:
                self.text_owner(reply)
        self.last_error = ""
        return len(msgs)

    def run_forever(self, running=lambda: True) -> None:
        backoff = 0
        while running():
            if self.configured():
                try:
                    self.poll_once()
                    backoff = 0
                except Exception as e:
                    self.last_error = str(e)[:300]
                    backoff = min(300, backoff * 2 or 30)
                    activity.log("error", f"sms poll failed: {self.last_error}")
            time.sleep(max(5, env_int("SMS_POLL_SECONDS", 20)) + backoff)

    # ------------------------------------------------------------------ approvals by text
    def ask_approval(self, prompt: str, wait: int) -> bool | None:
        """Text the owner a yes/no question. Returns True/False, or None if no answer in time."""
        with self._lock:
            ev = threading.Event()
            self.pending = {"prompt": prompt, "answer": None, "event": ev}
            try:
                pin = " <your PIN>" if env("SMS_PIN") else " <PIN - set SMS_PIN in .env first>"
                self.text_owner(f"Friday needs your OK: {prompt}\nReply YES{pin} to approve or NO to cancel.")
                # quicker polling while waiting
                end = time.time() + wait
                while time.time() < end and not ev.is_set():
                    try:
                        self.poll_once()
                    except Exception:
                        pass
                    ev.wait(6)
                return self.pending["answer"]
            finally:
                self.pending = None

    def status(self) -> dict:
        return {"configured": self.configured(), "owner_set": bool(env("OWNER_PHONE")), "pin_set": bool(env("SMS_PIN")),
                "last_poll": self.last_poll, "last_error": self.last_error,
                "waiting_for_approval": self.pending is not None}


LINK: SmsLink | None = None


def get_link() -> SmsLink:
    global LINK
    if LINK is None:
        LINK = SmsLink()
    return LINK


# ------------------------------------------------------------------ local vision (photos)
def describe_image(image: bytes, question: str = "") -> str:
    """Ask the LOCAL vision model (Ollama, VISION_MODEL) about a photo. Free, nothing leaves the PC."""
    model = env("VISION_MODEL", "qwen2.5vl:7b")
    body = {"model": model, "stream": False, "messages": [{
        "role": "user", "images": [base64.b64encode(image).decode()],
        "content": question or "Describe this photo briefly. If it shows a vehicle part, motorcycle, ATV, damage, "
                               "a label or a part number, say what it is and read any numbers."}]}
    if "qwen3" in model.lower():
        body["think"] = False  # Qwen3.5 can read photos; skip its slow "thinking" step
    req = urllib.request.Request(ollama_url() + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            from .llm import strip_think
            return strip_think(json.loads(r.read().decode())["message"]["content"])
    except Exception as e:
        return f"(I couldn't look at the photo with the local vision model {model}: {e}. Run setup_local_ai.bat.)"


@tool("read_texts", "Read recent text messages (SMS) from the shop's Quo number: with the owner by default, or with one "
      "customer if you give their phone number. Read-only; says plainly when texting isn't set up.",
      {"type": "object", "properties": {"phone": {"type": "string", "description": "Optional customer number"},
                                        "count": {"type": "integer"}}, "required": []})
def read_texts(ctx, phone: str = "", count: int = 8):
    link = get_link()
    if not link.configured():
        return "Texting isn't set up yet (Quo). Add it in Dashboard > Settings > SMS (Quo)."
    who = norm_phone(phone) if phone else link.owner
    if not who:
        return "I don't know whose texts to read. Give me a phone number."
    try:
        data = link._api("GET", "/messages", [("phoneNumberId", link.number_id()), ("participants[]", who),
                                              ("maxResults", max(1, min(int(count or 8), 20)))]).get("data", [])
    except Exception as e:
        return f"I couldn't read the texts right now ({type(e).__name__}: {str(e)[:120]})."
    if not data:
        return f"No texts with {who} yet."
    rows = sorted(data, key=lambda m: m.get("createdAt", ""))
    lines = []
    for m in rows:
        d = "from them" if m.get("direction") in ("incoming", "inbound") else "from the shop"
        t = (m.get("text") or m.get("body") or m.get("content") or "").strip().replace("\n", " ")
        lines.append(f"{m.get('createdAt', '')[:16].replace('T', ' ')} ({d}): {t[:200]}")
    return f"Texts with {who} (oldest first):\n" + "\n".join(lines)


@tool("text_me", "Send a text message (SMS) to the owner's own phone, e.g. a reminder or a result he asked to get by "
      "text. Only goes to the owner's number.",
      {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]})
def text_me(ctx, message: str):
    link = get_link()
    if not link.configured():
        return "Texting isn't set up yet. Add the Quo settings in Dashboard > Settings > SMS (Quo)."
    bad = placeholder_in(message)
    if bad:
        return (f"Error: the message still contains a placeholder ({bad}), so I did NOT send it. First get the real "
                "information (call the tool that has it), then call text_me again with the actual text.")
    return link.text_owner(message)


_PLACEHOLDER = re.compile(r"\[[^\]]{0,60}(will be|to be|insert|placeholder|details|goes here|fill in|todo|tbd)[^\]]{0,60}\]|"
                          r"\{\{[^}]*\}\}|<(?:insert|your|name|phone)[^>]*>|\blorem ipsum\b", re.I)


def placeholder_in(message: str) -> str:
    """The first fake-looking placeholder in an outgoing text ('[forecast details will be added here]'), or ''."""
    m = _PLACEHOLDER.search(message or "")
    return m.group(0) if m else ""


@tool("send_text_message", "Send a text message (SMS) from the shop's Quo number to someone else (a customer). "
      "Always asks the owner first.",
      {"type": "object", "properties": {"phone": {"type": "string"}, "message": {"type": "string"}},
       "required": ["phone", "message"]}, risk="send")
def send_text_message(ctx, phone: str, message: str):
    link = get_link()
    if not link.configured():
        return "Texting isn't set up yet. Add the Quo settings in Dashboard > Settings > SMS (Quo)."
    bad = placeholder_in(message)
    if bad:
        return f"Error: the message still contains a placeholder ({bad}), so I did NOT send it. Write the real text."
    return link.send(phone, message)
