"""Phone notifications. One call - notify.push(title, body) - reaches the owner wherever they are:
  - Web Push to the phone page (works with the screen off; on iPhone the page must be added to the Home Screen),
  - a short queue the open phone page polls (/v1/notifications), so it also works without Web Push,
  - optionally a text through Quo (SMS), because that arrives even with no data and no page installed.
Keys and subscriptions live in data/ (vapid_private.pem, push_subs.json). Nothing here is sent anywhere but to the
push service of the phone's own browser."""
from __future__ import annotations

import base64
import json
import threading
import time
from collections import deque
from pathlib import Path

from .env import data_dir, env, env_bool

_LOCK = threading.Lock()
_QUEUE: deque[dict] = deque(maxlen=50)
_COUNTER = [int(time.time())]


def _dir(base: Path | None = None) -> Path:
    p = Path(base or data_dir())
    p.mkdir(parents=True, exist_ok=True)
    return p


def _vapid(base: Path | None = None):
    from py_vapid import Vapid

    path = _dir(base) / "vapid_private.pem"
    if path.exists():
        return Vapid.from_file(str(path))
    v = Vapid()
    v.generate_keys()
    v.save_key(str(path))
    return v


def public_key(base: Path | None = None) -> str:
    """The key the phone page needs to subscribe (URL-safe base64, as browsers expect)."""
    from cryptography.hazmat.primitives import serialization

    raw = _vapid(base).public_key.public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _subs_path(base: Path | None = None) -> Path:
    return _dir(base) / "push_subs.json"


def subscriptions(base: Path | None = None) -> list[dict]:
    try:
        return json.loads(_subs_path(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def subscribe(sub: dict, base: Path | None = None) -> bool:
    if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://") or "keys" not in sub:
        return False
    with _LOCK:
        subs = [s for s in subscriptions(base) if s.get("endpoint") != sub["endpoint"]]
        subs.append({"endpoint": sub["endpoint"], "keys": sub["keys"]})
        _subs_path(base).write_text(json.dumps(subs[-10:]), encoding="utf-8")
    return True


def recent(since: int = 0) -> list[dict]:
    return [n for n in list(_QUEUE) if n["id"] > since]


def _send_webpush(title: str, body: str, base: Path | None = None, data: dict | None = None) -> int:
    subs = subscriptions(base)
    if not subs:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0
    key_path = str(_dir(base) / "vapid_private.pem")
    _vapid(base)
    sent, keep = 0, []
    for s in subs:
        try:
            webpush(s, json.dumps({"title": title, "body": body, "data": data or {}}), vapid_private_key=key_path,
                    vapid_claims={"sub": env("VAPID_CONTACT", "mailto:jarvis-assistant@users.noreply.github.com")},
                    ttl=3600)
            sent += 1
            keep.append(s)
        except WebPushException as e:
            if getattr(e.response, "status_code", 0) in (404, 410):  # the phone unsubscribed: forget it
                continue
            keep.append(s)
        except Exception:
            keep.append(s)
    if len(keep) != len(subs):
        with _LOCK:
            _subs_path(base).write_text(json.dumps(keep), encoding="utf-8")
    return sent


def push(title: str, body: str = "", text_too: bool | None = None, base: Path | None = None,
         data: dict | None = None) -> None:
    """Tell the owner's phone. Never raises. `data` rides along (e.g. an approval id + one-time token for the buttons)."""
    with _LOCK:
        _COUNTER[0] += 1
        item = {"id": _COUNTER[0], "title": title, "body": body, "time": time.time()}
        if data:
            item["data"] = data
        _QUEUE.append(item)

    def work():
        try:
            _send_webpush(title, body, base, data)
        except Exception as e:
            print(f"[notify] web push failed: {type(e).__name__}: {e}")
        if text_too if text_too is not None else env_bool("NOTIFY_BY_TEXT", True):
            try:
                from .sms import get_link

                link = get_link()
                if link.configured():
                    link.text_owner(f"{title}: {body}" if body else title)
            except Exception as e:
                print(f"[notify] text failed: {type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True, name="notify").start()
