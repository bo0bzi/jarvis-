"""v2.0.8: real web search, live page reading, browser/computer control with permission. Fakes only - no network."""
import io
import json

import pytest

from friday import autonomy, computer, quick, toolpick, web
from friday import tools as T
from friday.llm import is_fake_promise, parse_json_tool_call

from test_v207 import _resp, brain  # noqa: F401

PAGE = b"""<html><head><title>Integrity Powersports | Repairs</title>
<meta name="description" content="Motorcycle and ATV repair in St. Catharines">
<meta name="generator" content="WordPress 6.5"><script>var junk = 'SECRETJS';</script>
<style>.x{color:red}</style><link href="/wp-content/themes/x.css"></head>
<body><nav><a href="/services">Services</a></nav><h1>We fix bikes</h1>
<p>Call us for <b>sled</b> and ATV service.</p><a href="https://github.com/acme/site">Source</a></body></html>"""

DDG = """<div class="result results_links"><a rel="nofollow" class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fintegritypowersports.com%2F&amp;rut=x">Integrity <b>Powersports</b></a>
<a class="result__snippet" href="#">Motorcycle repair in St. Catharines</a></div>
<div class="result results_links"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result results_links"><a class="result__a" href="https://www.kijiji.ca/powersports">Powersports deals</a>
<a class="result__snippet">Used ATVs</a></div>"""


class Resp(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def __call__(self, req, timeout=0):
        self.urls.append(req.full_url)
        for key, body in self.pages.items():
            if key in req.full_url:
                return Resp(body if isinstance(body, bytes) else body.encode(), req.full_url)
        raise OSError("no fake page for " + req.full_url)


@pytest.fixture
def net(monkeypatch):
    op = Opener({"duckduckgo.com/html": DDG, "integritypowersports.com": PAGE})
    monkeypatch.setattr(web, "OPENER", op)
    monkeypatch.setattr(computer, "available", lambda: False)
    return op


def test_web_search_returns_real_results(net):
    out = T.dispatch("web_search", {"query": "integrity powersports"}, T.ToolContext())
    assert "1. Integrity Powersports - https://integritypowersports.com/" in out
    assert "Motorcycle repair" in out and "y.js" not in out and "kijiji" in out


def test_search_drops_junk_results(net, monkeypatch):
    net.pages["duckduckgo.com/html"] = DDG.replace("Integrity", "Olympic").replace("integrity", "olympic").replace(
        "Powersports", "Park").replace("powersports", "park")
    net.pages["bing.com"] = "<html>nothing</html>"
    out = T.dispatch("web_search", {"query": "integrity powersports"}, T.ToolContext())
    assert out.startswith("Error: web search failed") and "no matching results" in out


def test_bing_link_decoding():
    raw = ('<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=1&amp;u=a1aHR0cHM6Ly9pbnRlZ3JpdHkuY29tLw'
           '&amp;ntb=1">Integrity</a></h2><p>Snip</p></li>')
    assert web.parse_bing(raw)[0]["url"] == "https://integrity.com/"


def test_browse_web_reads_page(net):
    out = T.dispatch("browse_web", {"url": "integritypowersports.com"}, T.ToolContext())
    assert "PAGE: Integrity Powersports | Repairs" in out and "URL: https://integritypowersports.com" in out
    assert "We fix bikes" in out and "sled and ATV service" in out and "SECRETJS" not in out and "color:red" not in out
    assert "WordPress" in out and "github.com/acme/site" in out and "Services -> https://integritypowersports.com/services" in out


def test_invented_tool_name_web_browse_still_works(net):
    assert "We fix bikes" in T.dispatch("web_browse", {"url": "https://integritypowersports.com"}, T.ToolContext())
    assert parse_json_tool_call('{"tool": "web_browse", "args": {"url": "x.com"}}') == ("browse_web", {"url": "x.com"})


def test_bare_link_is_read_directly_without_ai(brain, net):  # noqa: F811
    b, fake = brain([])
    out = b.chat("read https://integritypowersports.com")
    assert "Integrity Powersports | Repairs" in out and "We fix bikes" in out and fake.calls == []
    out = b.chat("integritypowersports.com")
    assert "We fix bikes" in out and fake.calls == []


def test_open_site_opens_it_for_real(brain, net, monkeypatch):  # noqa: F811
    from friday import tools as T

    opened = []
    monkeypatch.setattr(T.webbrowser, "open", lambda u: opened.append(u))
    # used to be left to the AI, which sometimes only SAID "Opening..." without doing it
    assert quick.handle(brain()[0], "open youtube.com") == "Opened https://youtube.com."
    assert opened == ["https://youtube.com"]
    assert quick.handle(brain()[0], "install https://github.com/a/b") != quick._short_page("x")


def test_cant_browse_excuse_gets_nudged(brain, net, monkeypatch):  # noqa: F811
    monkeypatch.setenv("WEB_PREFETCH", "off")  # this test is about the tool path; prefetch answers without tools
    excuse = ("The issue here is browser automation permissions or your environment blocking direct navigation. "
              "Your call: search-based analysis or manual reconstruction from memory?")
    assert is_fake_promise("look at integritypowersports.com and tell me what it's built with", excuse)
    assert is_fake_promise("check the website", "Sorry, I can't access websites directly.")
    assert not is_fake_promise("what's 2+2", "4")
    b, fake = brain([_resp(excuse), _resp(tool="browse_web", args={"url": "integritypowersports.com"}),
                     _resp("It's a WordPress site.")])
    assert b.chat("tell me what integritypowersports.com is built with") == "It's a WordPress site."
    assert any("browse_web" in json.dumps(m) for m in fake.calls[1]["messages"][-1:])
    sent = [t["function"]["name"] for t in fake.calls[0]["tools"]]
    assert "browse_web" in sent and "web_search" in sent


def test_click_and_type_need_permission_unless_control_mode(monkeypatch):
    computer._CONTROL["until"] = 0
    asked = []
    ctx = T.ToolContext(confirm=lambda p: asked.append(p) or False)
    monkeypatch.setattr(computer, "_pyautogui", lambda: pytest.fail("must not click without permission"))
    for name, args in (("mouse_click", {"x": 1, "y": 2}), ("keyboard_type", {"text": "hi"}),
                       ("press_keys", {"keys": "ctrl+s"}), ("browser_click", {"target": "Buy"}),
                       ("browser_type", {"field": "q", "text": "x"})):
        assert T.dispatch(name, args, ctx).startswith("Cancelled"), name
    assert len(asked) == 5
    # turning control mode on always asks; when allowed, clicks go through without asking again
    assert T.dispatch("allow_computer_control", {"minutes": 5}, ctx).startswith("Cancelled")
    clicks = []
    monkeypatch.setattr(computer, "_pyautogui", lambda: type("P", (), {"click": lambda *a, **k: clicks.append(a)})())
    ok = T.ToolContext(confirm=lambda p: True)
    assert "Control mode ON for 5 minutes" in T.dispatch("allow_computer_control", {"minutes": 5}, ok)
    assert autonomy.needs_approval(T.REGISTRY["mouse_click"]) is False
    assert T.dispatch("mouse_click", {"x": 10, "y": 20}, ctx).startswith("Clicked") and clicks
    assert "OFF" in T.dispatch("stop_computer_control", {}, ctx)
    assert autonomy.needs_approval(T.REGISTRY["mouse_click"]) is True


def test_card_number_always_asks_even_in_control_mode(monkeypatch):
    computer._CONTROL["until"] = 9e18
    try:
        monkeypatch.setattr(computer, "_pyautogui", lambda: type("P", (), {"write": lambda *a, **k: None})())
        no = T.ToolContext(confirm=lambda p: False)
        assert T.dispatch("keyboard_type", {"text": "4111 1111 1111 1111"}, no).startswith("Cancelled")
        assert T.dispatch("keyboard_type", {"text": "hello"}, no).startswith("Typed 5")
    finally:
        computer._CONTROL["until"] = 0


def test_missing_browser_gives_setup_steps(monkeypatch):
    monkeypatch.setattr(computer, "available", lambda: False)
    for name, args in (("browser_open", {"url": "x.com"}), ("browser_read", {}), ("browser_look", {})):
        assert "setup_browser.bat" in T.dispatch(name, args, T.ToolContext())


def test_picker_gives_browser_tools_for_computer_requests():
    names = toolpick.pick("open the browser and click the login button")
    assert {"browser_open", "browser_click", "browser_type"} <= set(names) and len(names) < 40
    assert "browse_web" in toolpick.pick("hi")
