"""Administrator (UAC) awareness.

Windows draws its "Do you want to allow this app to make changes?" box on a protected screen that NO program can
click, Jarvis included - that is a deliberate Windows protection and we don't try to get around it. What Jarvis does:
  - notice that the box is waiting (the Windows process consent.exe is running),
  - tell the owner right away (spoken + text) and say how to approve it from anywhere (the AnyDesk ID),
  - keep waiting instead of silently timing out.
The owner approves the box (at the PC, or remotely through AnyDesk)."""
from __future__ import annotations

import contextlib
import threading
import time

NOTICE_EVERY = 600  # seconds: don't text again for the same wait


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def uac_prompt_active() -> bool:
    """True while Windows is showing an administrator approval box."""
    try:
        import psutil

        return any((p.info["name"] or "").lower() == "consent.exe" for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def waiting_message(what: str) -> str:
    try:
        from .livefacts import anydesk_id

        ad = anydesk_id()
    except Exception:
        ad = ""
    how = f" You can approve it remotely with AnyDesk (ID {ad})." if ad else ""
    return f"Jarvis needs your OK: Windows is asking for administrator permission to {what}.{how} I'm waiting."


@contextlib.contextmanager
def watch_uac(what: str, notify, poll: float = 2.0, active=uac_prompt_active):
    """While the block runs, call notify(message) when a Windows admin box appears (at most once per NOTICE_EVERY)."""
    stop = threading.Event()

    def loop():
        last = 0.0
        while not stop.wait(poll):
            if active() and time.time() - last > NOTICE_EVERY:
                last = time.time()
                try:
                    notify(waiting_message(what))
                except Exception:
                    pass

    t = threading.Thread(target=loop, daemon=True, name="uac-watch")
    t.start()
    try:
        yield
    finally:
        stop.set()


def notify_owner(ctx, message: str) -> None:
    """Say it out loud, show it, and text the owner's phone if texting is set up."""
    try:
        if getattr(ctx, "speak", None):
            ctx.speak(message)
    except Exception:
        pass
    try:  # phone notification (web push) and, if texting is set up, a text
        from . import notify

        notify.push("Jarvis needs you", message)
    except Exception:
        pass
    try:  # a real text message to the owner's phone (only if SMS is configured; fails soft)
        from . import sms

        link = sms.get_link()
        if link.status().get("configured"):
            link.text_owner(message)
    except Exception:
        pass
