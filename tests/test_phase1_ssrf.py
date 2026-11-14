"""Phase 1 item 8: web.fetch must not be steerable at this machine or the local network.

The model picks these URLs and a page can steer it, so http://127.0.0.1, http://169.254.169.254 (cloud metadata)
and friends must be refused before any request is made - including through a redirect."""
from __future__ import annotations

import socket

import pytest

from friday import web


@pytest.fixture()
def net(monkeypatch):
    """A fake opener that records every URL it was asked for."""
    asked: list[str] = []

    class Resp:
        def __init__(self, body: bytes, url: str):
            self._body, self.url = body, url
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, n=None):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        asked.append(req.full_url)
        return Resp(b"<html><body>ok</body></html>", req.full_url)

    monkeypatch.setattr(web, "OPENER", opener)
    monkeypatch.setattr(web, "DNS_CHECK", False)
    return asked


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8765/api/state", "https://127.0.0.1/",
    "http://localhost/", "http://localhost:8765/", "http://LOCALHOST/",
    "http://[::1]/", "http://0.0.0.0/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://100.64.0.1/", "http://metadata.google.internal/", "http://foo.localhost/",
    "http://printer.internal/",
])
def test_local_and_private_targets_are_refused(url, net):
    with pytest.raises(ValueError):
        web.fetch(url)
    assert net == [], f"{url} must be refused before any request is made"


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://example.com/x", "javascript:alert(1)",
                                 "data:text/html,hi"])
def test_non_http_schemes_are_refused(url, net):
    with pytest.raises(ValueError):
        web.fetch(url)
    assert net == []


def test_a_normal_public_page_still_loads(net):
    final, body = web.fetch("https://example.com/page")
    assert final == "https://example.com/page"
    assert "ok" in body
    assert net == ["https://example.com/page"]


def test_a_redirect_to_localhost_is_refused(monkeypatch):
    """The first URL is public, the redirect target is not: the final URL must be checked too."""
    class Resp:
        def __init__(self):
            self.url = "http://127.0.0.1:8765/api/state"
            self.headers = {"Content-Type": "text/html"}

        def read(self, n=None):
            return b"secret"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web, "OPENER", lambda req, timeout=0: Resp())
    monkeypatch.setattr(web, "DNS_CHECK", False)
    with pytest.raises(ValueError):
        web.fetch("https://example.com/redirect")


def test_dns_check_catches_a_public_name_pointing_at_a_private_address(monkeypatch):
    monkeypatch.setattr(web, "DNS_CHECK", True)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 80))])
    with pytest.raises(ValueError) as e:
        web.check_url("https://totally-public.example/x")
    assert "192.168.1.50" in str(e.value)


def test_dns_check_allows_a_public_address(monkeypatch):
    monkeypatch.setattr(web, "DNS_CHECK", True)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    assert web.check_url("https://example.com/x") == "https://example.com/x"


def test_an_unresolvable_host_is_left_to_the_fetch_to_fail(monkeypatch):
    monkeypatch.setattr(web, "DNS_CHECK", True)

    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert web.check_url("https://nope.invalid/x") == "https://nope.invalid/x"


def test_browse_web_reports_the_refusal_instead_of_fetching(net):
    from friday import tools as T

    out = T.dispatch("browse_web", {"url": "http://127.0.0.1:8765/api/state"}, T.ToolContext())
    assert "won't fetch" in out
    assert net == []
