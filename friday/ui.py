"""Console status display + optional system-tray icon (pystray)."""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable

if sys.platform.startswith("win"):
    os.system("")  # enables ANSI colours in the classic Windows console

COLORS = {"idle": "90", "listening": "92", "thinking": "93", "speaking": "96", "tool": "95", "error": "91",
          "sleeping": "90", "conversation": "92", "muted": "91"}
ICONS = {"idle": "o", "listening": "*", "thinking": "~", "speaking": ">", "tool": "#", "error": "!", "sleeping": "z",
         "conversation": "*", "muted": "x"}
TRAY_RGB = {"idle": (120, 120, 120), "listening": (0, 200, 90), "thinking": (240, 190, 0),
            "speaking": (0, 170, 255), "tool": (190, 80, 255), "error": (230, 40, 40), "sleeping": (70, 70, 70),
            "conversation": (0, 230, 160), "muted": (160, 40, 40)}

BANNER = r"""
  ███████╗██████╗ ██╗██████╗  █████╗ ██╗   ██╗
  ██╔════╝██╔══██╗██║██╔══██╗██╔══██╗╚██╗ ██╔╝
  █████╗  ██████╔╝██║██║  ██║███████║ ╚████╔╝
  ██╔══╝  ██╔══██╗██║██║  ██║██╔══██║  ╚██╔╝
  ██║     ██║  ██║██║██████╔╝██║  ██║   ██║
  ╚═╝     ╚═╝  ╚═╝╚═╝╚═════╝ ╚═╝  ╚═╝   ╚═╝   your personal AI assistant
"""


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


class UI:
    def __init__(self, name: str = "Friday", tray: bool = True, on_quit: Callable[[], None] | None = None,
                 on_toggle_mute: Callable[[], bool] | None = None, bus=None,
                 on_toggle_mic: Callable[[], bool] | None = None, on_open_dashboard: Callable[[], None] | None = None,
                 ):
        self.name = name
        self.status = "idle"
        self.on_quit = on_quit or (lambda: os._exit(0))
        self.on_toggle_mute = on_toggle_mute
        self.on_toggle_mic = on_toggle_mic
        self.on_open_dashboard = on_open_dashboard
        self.bus = bus
        self._last_printed = None
        self._icon = None
        self._lock = threading.Lock()
        if tray:
            self._start_tray()

    # ---------------------------------------------------------------- console
    def banner(self, lines: list[str]) -> None:
        try:
            print(_c("96", BANNER))
        except UnicodeEncodeError:
            print(f"=== {self.name.upper()} ===")
        for line in lines:
            print("  " + line)
        print()

    def set_status(self, status: str, detail: str = "") -> None:
        with self._lock:
            self.status = status
            if self.bus is not None:
                self.bus.set_status(status, detail)
            label = f"[{ICONS.get(status, '?')} {status.upper()}]"
            line = f"{label} {detail}".rstrip()
            if line != self._last_printed:  # always-on listening repeats states a lot; don't spam
                print(_c(COLORS.get(status, "0"), line), flush=True)
                self._last_printed = line
            if self._icon is not None:
                try:
                    self._icon.icon = self._make_image(status)
                    self._icon.title = f"{self.name}: {status}"
                except Exception:
                    pass

    def user(self, text: str, via: str = "") -> None:
        if self.bus is not None:
            self.bus.chat("user", text, via)
        print(_c("97", f"You: {text}"), flush=True)

    def assistant(self, text: str) -> None:
        if self.bus is not None:
            self.bus.chat("assistant", text)
        print(_c("96", f"{self.name}: {text}"), flush=True)

    def info(self, text: str) -> None:
        if self.bus is not None:
            self.bus.log_event("friday", text)
        print(_c("90", text), flush=True)

    # ---------------------------------------------------------------- tray
    @staticmethod
    def _make_image(status: str):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(20, 30, 45, 255), outline=TRAY_RGB.get(status, (120, 120, 120)), width=6)
        d.ellipse((22, 22, 42, 42), fill=TRAY_RGB.get(status, (120, 120, 120)))
        return img

    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception as e:
            self.info(f"(tray icon disabled: {e})")
            return

        def toggle_mute(icon, item):
            if self.on_toggle_mute:
                muted = self.on_toggle_mute()
                self.info("Voice muted." if muted else "Voice unmuted.")

        def toggle_mic(icon, item):
            if self.on_toggle_mic:
                muted = self.on_toggle_mic()
                self.info("Microphone muted." if muted else "Microphone listening.")

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: f"Status: {self.status}", None, enabled=False),
            pystray.MenuItem("Open dashboard", lambda icon, item: self.on_open_dashboard and self.on_open_dashboard(),
                             default=True),
            pystray.MenuItem("Mute / unmute microphone", toggle_mic),
            pystray.MenuItem("Mute / unmute voice", toggle_mute),
            pystray.MenuItem(f"Quit {self.name}", lambda icon, item: (icon.stop(), self.on_quit())),
        )
        try:
            self._icon = pystray.Icon("friday", self._make_image("idle"), f"{self.name}: idle", menu)
            self._icon.run_detached()
        except Exception as e:
            self._icon = None
            self.info(f"(tray icon disabled: {e})")

    def close(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
