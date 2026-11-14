"""Honest screen reading: exact facts first (window titles + OCR text), vision model only as a labelled extra.

The vision model alone invents details (it once 'saw' a spreadsheet that was not there), so look_at_screen reports:
  1. the real window titles (Windows API) - exact,
  2. the text actually read off the screen by offline OCR (rapidocr) - exact,
  3. optionally a visual description from the local vision model, clearly marked as possibly inaccurate.
Everything stays on the PC."""
from __future__ import annotations

import io
import threading

_OCR = None
_OCR_LOCK = threading.Lock()


def window_titles() -> tuple[str, list[str]]:
    """(foreground window title, titles of other visible windows). Empty on failure."""
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        fg = u.GetForegroundWindow()
        titles: list[str] = []

        def title_of(h) -> str:
            n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            return buf.value.strip()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _):
            if u.IsWindowVisible(h) and not u.IsIconic(h):
                t = title_of(h)
                if t and t not in ("Program Manager", "Settings") and t not in titles:
                    titles.append(t)
            return True

        u.EnumWindows(cb, 0)
        ft = title_of(fg) if fg else ""
        return ft, [t for t in titles if t != ft][:15]
    except Exception:
        return "", []


def ocr_text(png: bytes, max_chars: int = 3500) -> str:
    """Text found in the picture, top-to-bottom. '' if OCR isn't installed or finds nothing."""
    global _OCR
    try:
        with _OCR_LOCK:
            if _OCR is None:
                from rapidocr_onnxruntime import RapidOCR

                _OCR = RapidOCR()
            result, _ = _OCR(png)
    except Exception:
        return ""
    if not result:
        return ""
    rows = sorted(result, key=lambda r: (round(r[0][0][1] / 14), r[0][0][0]))  # reading order: line, then left-to-right
    lines, last_y, cur = [], None, []
    for box, text, conf in rows:
        if float(conf) < 0.5:
            continue
        y = round(box[0][1] / 14)
        if last_y is not None and y != last_y and cur:
            lines.append(" ".join(cur))
            cur = []
        cur.append(text)
        last_y = y
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)[:max_chars]


def grab_png(width: int = 2200) -> bytes:
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=False).convert("RGB")
    img.thumbnail((width, width))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


GROUNDED_PROMPT = ("Describe ONLY what is visibly on this screenshot: layout, colours, images, buttons. Do NOT guess app names, "
                   "documents, numbers or text you cannot clearly read; say 'unclear' instead. Known facts: {facts}. Question: {q}")


def look(question: str = "", want_visual: bool = True) -> str:
    png = grab_png()
    fg, others = window_titles()
    text = ocr_text(png)
    out = []
    if fg or others:
        out.append("Windows open (exact): " + (f"IN FRONT: {fg}. " if fg else "") + ("Others: " + "; ".join(others) if others else ""))
    out.append("Text read off the screen (exact, may skip tiny text):\n" + (text or "(none found)"))
    if want_visual:
        from .sms import describe_image

        facts = f"front window '{fg}'" + (f", other windows {others[:6]}" if others else "")
        d = describe_image(png, GROUNDED_PROMPT.format(facts=facts, q=question or "what does the screen look like?"))
        out.append("Visual impression from the local vision model (can be wrong - trust the exact parts above first): " + d[:900])
    return "\n".join(out)
