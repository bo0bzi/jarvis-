"""Browser + computer control, with the owner's permission.

Browser: Friday drives its OWN browser window on the PC (Playwright; uses Chrome or Edge if Chromium isn't
installed). You can watch it. Opening and reading pages is free to do; clicking and typing ask first.
Desktop: look at the screen with the local vision model, click the mouse, type, press keys - these ask first.

"Control mode" (allow_computer_control) lets Friday click/type for N minutes without asking each time. It
always needs your OK to switch on, and typing passwords, card numbers or clicking buy/pay buttons ALWAYS
asks, even in control mode. Emergency stop for the mouse: slam it into a screen corner (pyautogui failsafe),
or say "stop computer control".

Needs: pip install playwright pyautogui (setup_browser.bat does it)."""
from __future__ import annotations

import io
import queue
import re
import threading
import time

from . import tools as T
from .env import data_dir, env

SETUP_MSG = ("Browser control isn't installed yet. Close Friday, double-click setup_browser.bat in the Friday "
             "folder (one time, needs internet), then start Friday again.")
DESKTOP_MSG = ("Mouse/keyboard control isn't installed yet. Close Friday, double-click setup_browser.bat in the "
               "Friday folder, then start Friday again.")
RISKY_CLICK = re.compile(r"\b(buy|pay|purchase|place (your )?order|checkout|check out|confirm (order|payment)|"
                         r"submit (order|payment)|subscribe|donate|send money|transfer|delete account)\b", re.I)
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# ------------------------------------------------------------------ control mode
_CONTROL = {"until": 0.0}


def control_active() -> bool:
    return time.time() < _CONTROL["until"]


def control_left() -> int:
    return max(0, int(_CONTROL["until"] - time.time()))


@T.tool("allow_computer_control", "Turn on control mode: lets Friday click and type in the browser and on the PC "
        "for some minutes without asking each time. Only when the user asks for it. Always asks the user first.",
        {"type": "object", "properties": {"minutes": {"type": "integer"}}, "required": []}, risk="system")
def allow_computer_control(ctx, minutes: int = 10):
    minutes = max(1, min(120, int(minutes or 10)))
    _CONTROL["until"] = time.time() + minutes * 60
    return (f"Control mode ON for {minutes} minutes: I can click and type without asking. Passwords, card numbers "
            "and buy/pay buttons still ask. Say 'stop computer control' to end it.")


@T.tool("stop_computer_control", "Turn control mode off (Friday asks before every click/type again).")
def stop_computer_control(ctx):
    _CONTROL["until"] = 0.0
    return "Control mode OFF. I'll ask before every click and keystroke again."


# ------------------------------------------------------------------ browser (one worker thread owns Playwright)
def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


class _Browser:
    """Playwright objects must stay on the thread that made them, so every browser job runs on one thread."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.thread = None
        self.pw = self.context = self.page = None

    def run(self, fn, timeout: float = 90):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, daemon=True, name="friday-browser")
            self.thread.start()
        box: dict = {}
        done = threading.Event()
        self.jobs.put((fn, box, done))
        if not done.wait(timeout):
            return "Error: the browser took too long to respond."
        if "error" in box:
            e = box["error"]
            return f"Error: browser: {type(e).__name__}: {str(e)[:300]}"
        return box["result"]

    def _loop(self):
        while True:
            fn, box, done = self.jobs.get()
            try:
                box["result"] = fn(self)
            except Exception as e:  # keep the worker alive
                box["error"] = e
            done.set()

    def ensure_page(self):
        if self.page is not None:
            try:
                if not self.page.is_closed():
                    return self.page
            except Exception:
                pass
        if self.pw is None:
            from playwright.sync_api import sync_playwright

            self.pw = sync_playwright().start()
        profile = data_dir() / "browser_profile"  # Friday's own profile: logins you do in it are remembered
        profile.mkdir(parents=True, exist_ok=True)
        last = None
        channels = [env("BROWSER_CHANNEL", "")] if env("BROWSER_CHANNEL", "") else [None, "chrome", "msedge"]
        for ch in channels:
            try:
                kw = {"headless": False, "viewport": None, "args": ["--start-maximized"]}
                if ch:
                    kw["channel"] = ch
                self.context = self.pw.chromium.launch_persistent_context(str(profile), **kw)
                break
            except Exception as e:
                last = e
                self.context = None
        if self.context is None:
            raise RuntimeError(f"couldn't start a browser (Chromium, Chrome or Edge): {last}")
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self.page

    def close(self):
        for obj in (self.context, self.pw):
            try:
                if obj is self.pw and obj:
                    obj.stop()
                elif obj:
                    obj.close()
            except Exception:
                pass
        self.pw = self.context = self.page = None


BROWSER = _Browser()

_CLICKABLES_JS = """() => {
  const els = Array.from(document.querySelectorAll('a[href], button, input, textarea, select, [role=button], [onclick]'));
  const out = [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || getComputedStyle(el).visibility === 'hidden') continue;
    let label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                 el.getAttribute('name') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (!label && tag !== 'input' && tag !== 'textarea') continue;
    el.setAttribute('data-friday-id', String(out.length + 1));
    out.push({id: out.length + 1, tag, type, label: label.slice(0, 70)});
    if (out.length >= 80) break;
  }
  return out;
}"""


def _snapshot(b: _Browser, max_text: int = 3500) -> str:
    page = b.ensure_page()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    items = page.evaluate(_CLICKABLES_JS) or []
    lines = [f"BROWSER PAGE: {page.title() or '(no title)'}", f"URL: {page.url}",
             "TEXT:\n" + (text[:max_text] + ("\n...(more - say browser_read or scroll)" if len(text) > max_text else ""))]
    if items:
        lines.append("CLICKABLE / FIELDS (use the number with browser_click or browser_type):")
        for it in items:
            kind = it["tag"] + (f":{it['type']}" if it["type"] else "")
            lines.append(f"[{it['id']}] {kind} {it['label']}")
    return "\n".join(lines)


def _locate(page, target: str):
    target = str(target).strip()
    if target.isdigit():
        return page.locator(f"[data-friday-id='{target}']").first
    if re.match(r"^[#.\[]|^\w+\[", target) or target.startswith(("css=", "xpath=", "//")):
        return page.locator(target).first
    for make in (lambda: page.get_by_role("button", name=target), lambda: page.get_by_role("link", name=target),
                 lambda: page.get_by_label(target), lambda: page.get_by_placeholder(target),
                 lambda: page.get_by_text(target)):
        loc = make()
        try:
            if loc.count():
                return loc.first
        except Exception:
            continue
    return page.get_by_text(target).first


def _need_browser():
    return None if available() else SETUP_MSG


@T.tool("browser_open", "Open a website in Friday's own browser window on the PC (you can watch it) and read it, "
        "including pages that need JavaScript. Lists the buttons/links/fields with numbers for clicking and typing.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
def browser_open(ctx, url: str):
    if _need_browser():
        return _need_browser()
    from .web import normalize_url

    url = normalize_url(url)

    def job(b):
        b.ensure_page().goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            b.page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        return _snapshot(b)
    return BROWSER.run(job)


@T.tool("browser_read", "Read the page that's open in Friday's browser right now (text + numbered clickables).")
def browser_read(ctx):
    if _need_browser():
        return _need_browser()
    return BROWSER.run(lambda b: _snapshot(b, 6000))


@T.tool("browser_scroll", "Scroll Friday's browser page up or down, then read what's visible.",
        {"type": "object", "properties": {"direction": {"type": "string", "enum": ["down", "up"]}}, "required": []})
def browser_scroll(ctx, direction: str = "down"):
    if _need_browser():
        return _need_browser()

    def job(b):
        b.ensure_page().mouse.wheel(0, -900 if direction == "up" else 900)
        time.sleep(0.6)
        return _snapshot(b)
    return BROWSER.run(job)


@T.tool("browser_back", "Go back one page in Friday's browser.")
def browser_back(ctx):
    if _need_browser():
        return _need_browser()

    def job(b):
        b.ensure_page().go_back(timeout=20000)
        return _snapshot(b)
    return BROWSER.run(job)


@T.tool("browser_click", "Click something in Friday's browser: a number from the list, the visible text of a "
        "button/link, or a CSS selector. Asks permission unless control mode is on.",
        {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}, risk="control")
def browser_click(ctx, target: str):
    if _need_browser():
        return _need_browser()

    def label(b):
        loc = _locate(b.ensure_page(), target)
        try:
            return (loc.inner_text(timeout=3000) or loc.get_attribute("value") or "").strip()[:80]
        except Exception:
            return ""
    name = BROWSER.run(label)
    if RISKY_CLICK.search(f"{target} {name}") and not ctx.confirm(
            f"Friday wants to click '{name or target}', which looks like it spends money or can't be undone. Allow?"):
        return "Cancelled: the user did not allow that click."

    def job(b):
        page = b.ensure_page()
        _locate(page, target).click(timeout=10000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        time.sleep(0.8)
        return f"Clicked '{name or target}'.\n" + _snapshot(b)
    return BROWSER.run(job)


@T.tool("browser_type", "Type text into a field in Friday's browser (a number from the list, the field's label or "
        "placeholder, or a CSS selector). submit=true presses Enter after. Asks permission unless control mode is on.",
        {"type": "object", "properties": {"field": {"type": "string"}, "text": {"type": "string"},
                                          "submit": {"type": "boolean"}}, "required": ["field", "text"]},
        risk="control")
def browser_type(ctx, field: str, text: str, submit: bool = False):
    if _need_browser():
        return _need_browser()

    def kind(b):
        loc = _locate(b.ensure_page(), field)
        try:
            return f"{loc.get_attribute('type') or ''} {loc.get_attribute('name') or ''} {loc.get_attribute('autocomplete') or ''}"
        except Exception:
            return ""
    info = BROWSER.run(kind).lower()
    secret = "password" in info or "cc-" in info or "card" in info or "cvc" in info or CARD.search(text or "")
    if secret and not ctx.confirm("Friday wants to type into a password or payment field. Allow it?"):
        return "Cancelled: the user did not allow typing into a password/payment field."

    def job(b):
        page = b.ensure_page()
        loc = _locate(page, field)
        loc.fill(text, timeout=10000)
        if submit:
            loc.press("Enter")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(1)
        shown = "(hidden)" if secret else f"'{text[:60]}'"
        return f"Typed {shown} into {field}" + (" and pressed Enter" if submit else "") + ".\n" + _snapshot(b)
    return BROWSER.run(job)


@T.tool("browser_look", "Take a picture of Friday's browser page and have the local vision model describe it or "
        "answer a question about it (layout, logo, images, colours). Free, stays on the PC.",
        {"type": "object", "properties": {"question": {"type": "string"}}, "required": []})
def browser_look(ctx, question: str = ""):
    if _need_browser():
        return _need_browser()
    shot = BROWSER.run(lambda b: b.ensure_page().screenshot(full_page=False))
    if isinstance(shot, str):
        return shot
    from .sms import describe_image

    return "What the browser page looks like: " + describe_image(
        shot, question or "Describe this web page: what site it is, the main text, logo, images, layout and colours.")


@T.tool("browser_close", "Close Friday's browser window.")
def browser_close(ctx):
    if BROWSER.thread is None:
        return "Friday's browser isn't open."
    BROWSER.run(lambda b: b.close())
    return "Closed Friday's browser."


# ------------------------------------------------------------------ desktop
def _grab() -> bytes:
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=False)
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@T.tool("look_at_screen", "Look at the PC screen right now with the local vision model and describe it or answer a "
        "question about what's on it. Free, the picture stays on the PC.",
        {"type": "object", "properties": {"question": {"type": "string"}}, "required": []})
def look_at_screen(ctx, question: str = ""):
    from . import screen

    try:  # exact window titles + OCR text first; the vision model's guess is only a labelled extra
        return "On the screen:\n" + screen.look(question, want_visual=True)
    except Exception as e:
        return f"Error: couldn't read the screen: {e}"


@T.tool("read_screen_text", "Read the exact text on the PC screen (window titles + OCR), fast, no guessing. Use this "
        "to read what an app, dialog or error says. Free, stays on the PC.",
        {"type": "object", "properties": {}, "required": []})
def read_screen_text(ctx):
    from . import screen

    try:
        return "On the screen:\n" + screen.look(want_visual=False)
    except Exception as e:
        return f"Error: couldn't read the screen: {e}"


def _pyautogui():
    try:
        import pyautogui

        pyautogui.FAILSAFE = True  # mouse into a corner = emergency stop
        return pyautogui
    except Exception:
        return None


@T.tool("mouse_click", "Click the mouse on the PC screen at x,y pixels (look_at_screen first). Asks permission "
        "unless control mode is on.",
        {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                          "button": {"type": "string", "enum": ["left", "right"]},
                                          "double": {"type": "boolean"}}, "required": ["x", "y"]}, risk="control")
def mouse_click(ctx, x: int, y: int, button: str = "left", double: bool = False):
    pg = _pyautogui()
    if pg is None:
        return DESKTOP_MSG
    pg.click(int(x), int(y), clicks=2 if double else 1, button=button if button in ("left", "right") else "left")
    return f"{'Double-clicked' if double else 'Clicked'} {button} at ({x}, {y})."


@T.tool("keyboard_type", "Type text on the PC keyboard into whatever window is active. Asks permission unless "
        "control mode is on.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, risk="control")
def keyboard_type(ctx, text: str):
    pg = _pyautogui()
    if pg is None:
        return DESKTOP_MSG
    if CARD.search(text or "") and not ctx.confirm("Friday wants to type something that looks like a card number. Allow?"):
        return "Cancelled: the user did not allow typing that."
    pg.write(text, interval=0.02)
    return f"Typed {len(text)} characters."


@T.tool("press_keys", "Press a key or shortcut on the PC, e.g. 'enter', 'ctrl+s', 'alt+tab'. Window control: 'win+d' "
        "minimizes all windows / shows the desktop, 'win+up' maximizes the active window, 'win+down' minimizes it, "
        "'alt+f4' closes it. Asks "
        "permission unless control mode is on.",
        {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, risk="control")
def press_keys(ctx, keys: str):
    pg = _pyautogui()
    if pg is None:
        return DESKTOP_MSG
    parts = [k.strip().lower() for k in re.split(r"[+ ]+", keys or "") if k.strip()]
    if not parts:
        return "Error: no keys given."
    pg.hotkey(*parts)
    return f"Pressed {'+'.join(parts)}."
