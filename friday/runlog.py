"""Run ledger: answers 'why did Jarvis stop?'.

While Jarvis runs it writes data/run_state.json every 30 s (a heartbeat). A clean exit (quit, restart, Ctrl+C) marks the
file clean with a reason. If Windows closes the console / logs off / shuts down, a console handler records that reason.
At the next start, a file that is neither clean nor fresh means the last run was killed from outside (crash, process
killed, sleep, PC off); that is logged to the activity log with the last time Jarvis was seen alive."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

HEARTBEAT = 30.0
_PATH: Path | None = None
_LOCK = threading.Lock()
_STATE: dict = {}
_CTRL_NAMES = {0: "Ctrl+C", 1: "Ctrl+Break", 2: "the console window was closed", 5: "the user logged off", 6: "Windows is shutting down"}
_handler_ref = None  # keep the ctypes callback alive


def _write() -> None:
    if _PATH is None:
        return
    try:
        with _LOCK:
            tmp = _PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(_STATE), encoding="utf-8")
            tmp.replace(_PATH)
    except OSError:
        pass


def previous_run(data_dir) -> dict:
    try:
        return json.loads((Path(data_dir) / "run_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def describe_previous(prev: dict, now: float | None = None) -> str:
    """'' if the last run ended cleanly (or there was none); else a plain sentence about how it ended."""
    if not prev or prev.get("clean"):
        return ""
    seen = prev.get("heartbeat", 0)
    when = datetime.fromtimestamp(seen).strftime("%a %I:%M %p").replace(" 0", " ") if seen else "an unknown time"
    reason = prev.get("reason")
    if reason:
        return f"The last run (seen alive {when}) was stopped by: {reason}."
    return (f"The last run (seen alive {when}) ended without saying goodbye: it was killed from outside "
            "(crash, process killed, PC asleep or switched off).")


def start(data_dir) -> str:
    """Call once at startup. Returns a sentence about how the previous run ended ('' if fine)."""
    global _PATH, _STATE
    _PATH = Path(data_dir) / "run_state.json"
    prev = previous_run(data_dir)
    msg = describe_previous(prev)
    _STATE = {"pid": os.getpid(), "started": time.time(), "heartbeat": time.time(), "clean": False, "reason": ""}
    _write()

    def beat():
        while True:
            time.sleep(HEARTBEAT)
            _STATE["heartbeat"] = time.time()
            _write()
    threading.Thread(target=beat, daemon=True, name="run-heartbeat").start()
    _install_console_handler()
    return msg


def mark_clean(reason: str) -> None:
    _STATE.update(clean=True, reason=reason, heartbeat=time.time())
    _write()


def _install_console_handler() -> None:
    """Windows tells us why the console is going away (window closed, logoff, shutdown): write it down before we die."""
    global _handler_ref
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        def handler(ctrl):
            if ctrl in (2, 5, 6):  # close / logoff / shutdown: the process is about to be killed
                _STATE.update(clean=False, reason=_CTRL_NAMES.get(ctrl, f"console event {ctrl}"), heartbeat=time.time())
                _write()
            return False  # let the normal handling continue

        _handler_ref = handler
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    except Exception:
        pass
