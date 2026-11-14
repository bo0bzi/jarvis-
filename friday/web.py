"""Real internet access: web_search returns actual results, browse_web fetches and reads a live page.
Read-only, so no permission needed. Plain Python (urllib) - no extra installs, nothing costs credits.
Tests swap OPENER for a fake so they never touch the network."""
from __future__ import annotations

import html as htmllib
import re
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser

from . import tools as T

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
MAX_TEXT = 6000
OPENER = urllib.request.urlopen  # swapped in tests
DNS_CHECK = True  # resolve hostnames to catch a public name pointing at a private address (tests turn this off)

# Hostnames that never mean "the public internet".
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
                  "metadata.google.internal", "instance-data", "169.254.169.254.nip.io"}


def _is_blocked_ip(ip) -> bool:
    """True for anything that is not a normal public address: loopback, private, link-local, CGNAT, reserved."""
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global)


def check_url(url: str) -> str:
    """Refuse a URL that would make Friday fetch something on this PC or the local network.

    The model chooses these URLs, and a page can steer it, so http://127.0.0.1, http://169.254.169.254 (cloud
    metadata) and friends must never be fetched. Returns the URL unchanged when it is safe; raises ValueError."""
    import ipaddress
    import socket

    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"only http and https links can be read, not '{parts.scheme or 'no scheme'}'")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ValueError("that link has no host name")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".internal"):
        raise ValueError(f"I won't fetch {host}: it points inside this machine or network")
    try:  # a literal address needs no DNS
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if _is_blocked_ip(ip):
            raise ValueError(f"I won't fetch {host}: it is a private or local address")
        return url
    if not DNS_CHECK:
        return url
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme.lower() == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return url  # can't resolve: let the fetch fail with its own error
    for info in infos:
        addr = info[4][0]
        try:
            if _is_blocked_ip(ipaddress.ip_address(addr)):
                raise ValueError(f"I won't fetch {host}: it resolves to the private address {addr}")
        except ValueError as e:
            if "I won't fetch" in str(e):
                raise
    return url


def fetch(url: str, timeout: float = 20, data: bytes | None = None) -> tuple[str, str]:
    """(final_url, html text). Raises on network errors, and on a URL that points inside this machine."""
    check_url(url)
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-CA,en;q=0.9"})
    with OPENER(req, timeout=timeout) as r:
        raw = r.read(3_000_000)
        final = getattr(r, "url", "") or url
        ctype = ""
        try:
            ctype = r.headers.get("Content-Type", "") or ""
        except Exception:
            pass
    if final != url:  # a redirect must not be a way around the check above
        check_url(final)
    m = re.search(r"charset=([\w-]+)", ctype, re.I) or re.search(rb'charset=["\']?([\w-]+)', raw[:3000], re.I)
    enc = m.group(1) if m else "utf-8"
    enc = enc.decode() if isinstance(enc, bytes) else enc
    try:
        return final, raw.decode(enc, errors="replace")
    except LookupError:
        return final, raw.decode("utf-8", errors="replace")


def normalize_url(url: str) -> str:
    url = (url or "").strip().strip("<>\"'")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


# ------------------------------------------------------------------ page reading
class _Reader(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
             "footer", "nav", "table", "ul", "ol", "form", "main", "aside", "blockquote", "pre"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.skip, self.in_title, self.in_a = base, 0, False, None
        self.title, self.parts, self.links, self.meta, self.headings = "", [], [], {}, []
        self._h = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "generator", "og:title", "og:description", "keywords") and a.get("content"):
                self.meta[key] = a["content"].strip()
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and a.get("href") and not a["href"].startswith(("javascript:", "#", "mailto:", "tel:")):
            self.in_a = [urllib.parse.urljoin(self.base, a["href"]), ""]
        if tag in ("h1", "h2", "h3"):
            self._h = [tag, ""]
        if tag == "img" and a.get("alt") and not self.skip:
            self.parts.append(f" [image: {a['alt'].strip()}] ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag == "a" and self.in_a:
            text = " ".join(self.in_a[1].split())
            if text and len(self.links) < 300:
                self.links.append((text[:80], self.in_a[0]))
            self.in_a = None
        if self._h and tag == self._h[0]:
            t = " ".join(self._h[1].split())
            if t:
                self.headings.append(t[:120])
            self._h = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self.parts.append(data)
        if self.in_a is not None:
            self.in_a[1] += data
        if self._h is not None:
            self._h[1] += data

    def text(self) -> str:
        t = "".join(self.parts)
        lines = [" ".join(line.split()) for line in t.splitlines()]
        out, blank = [], False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


TECH = [(r"wp-content|wp-includes", "WordPress"), (r"cdn\.shopify|Shopify\.theme", "Shopify"),
        (r"static\.wixstatic|wix\.com", "Wix"), (r"squarespace", "Squarespace"), (r"/_next/|__NEXT_DATA__", "Next.js"),
        (r"__nuxt|/_nuxt/", "Nuxt"), (r"gatsby", "Gatsby"), (r"netlify", "Netlify"), (r"vercel", "Vercel"),
        (r"webflow", "Webflow"), (r"godaddy|img1\.wsimg", "GoDaddy Website Builder"), (r"jquery", "jQuery"),
        (r"bootstrap(\.min)?\.(css|js)", "Bootstrap"), (r"tailwind", "Tailwind CSS"), (r"react", "React"),
        (r"vue(\.min)?\.js|data-v-", "Vue"), (r"cloudflare", "Cloudflare"), (r"google-analytics|gtag\(", "Google Analytics")]


def detect_tech(raw: str, meta: dict) -> list[str]:
    found = [name for pat, name in TECH if re.search(pat, raw, re.I)]
    gen = meta.get("generator")
    if gen:
        found.insert(0, f"generator: {gen}")
    return list(dict.fromkeys(found))


def read_page(url: str) -> dict:
    final, raw = fetch(normalize_url(url))
    r = _Reader(final)
    try:
        r.feed(raw)
    except Exception:
        pass
    return {"url": final, "title": " ".join(r.title.split()), "text": r.text(), "links": r.links,
            "meta": r.meta, "headings": r.headings, "tech": detect_tech(raw, r.meta), "raw_len": len(raw)}


def page_report(page: dict, question: str = "", max_text: int = MAX_TEXT) -> str:
    out = [f"PAGE: {page['title'] or '(no title)'}", f"URL: {page['url']}"]
    if page["meta"].get("description"):
        out.append(f"Description: {page['meta']['description']}")
    if page["tech"]:
        out.append("Built with (detected): " + ", ".join(page["tech"]))
    if page["headings"]:
        out.append("Headings: " + " | ".join(page["headings"][:15]))
    text = page["text"]
    if len(text) > max_text:
        text = text[:max_text] + f"\n...(cut; page has {len(page['text']):,} characters of text)"
    out.append("TEXT:\n" + (text or "(almost no text - the page is probably built with JavaScript. Use "
                                    "browser_open to load it in a real browser.)"))
    gh = [u for _, u in page["links"] if "github.com/" in u]
    if gh:
        out.append("GitHub links: " + ", ".join(dict.fromkeys(gh[:10])))
    if page["links"]:
        out.append("Some links: " + "; ".join(f"{t} -> {u}" for t, u in page["links"][:25]))
    if question:
        out.append(f"(Answer the user's question from this page: {question})")
    return "\n".join(out)


@T.tool("browse_web", "Fetch a LIVE web page and read it: title, text, headings, links, and what it's built with. "
        "Use for any URL or website the user mentions, or to read a search result. Read-only.",
        {"type": "object", "properties": {"url": {"type": "string"},
                                          "question": {"type": "string", "description": "what to look for (optional)"}},
         "required": ["url"]})
def browse_web(ctx, url: str, question: str = ""):
    try:
        page = read_page(url)
    except Exception as e:
        return f"Error: couldn't load {normalize_url(url)}: {type(e).__name__}: {e}"
    if len(page["text"]) < 200:  # JavaScript site: try the real browser if it is set up
        try:
            from . import computer

            if computer.available():
                return computer.browser_open(ctx, page["url"])
        except Exception:
            pass
    return page_report(page, question)


# ------------------------------------------------------------------ search
def _ddg_url(href: str) -> str:
    href = htmllib.unescape(href)
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if q.get("uddg"):
            return q["uddg"][0]
    return ("https:" + href) if href.startswith("//") else href


def _strip(s: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", "", s)).split())


def parse_ddg(raw: str) -> list[dict]:
    results = []
    for block in re.split(r'<div[^>]+class="[^"]*\bresult\b', raw)[1:]:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S) or \
            re.search(r'href="([^"]+)"[^>]*class="result__a"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>', block, re.S)
        url = _ddg_url(a.group(1))
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        results.append({"title": _strip(a.group(2)), "url": url, "snippet": _strip(sn.group(1)) if sn else ""})
    return results


def _bing_url(href: str) -> str:
    """Bing wraps results in bing.com/ck/a?...&u=a1<base64url of the real link>."""
    href = htmllib.unescape(href)
    if "bing.com/ck/" in href:
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            import base64

            try:
                b = u[2:]
                return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
            except Exception:
                pass
    return href


def parse_bing(raw: str) -> list[dict]:
    results = []
    for block in re.split(r'<li class="b_algo"', raw)[1:]:
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        sn = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        results.append({"title": _strip(a.group(2)), "url": _bing_url(a.group(1)),
                        "snippet": _strip(sn.group(1)) if sn else ""})
    return results


def relevant(results: list[dict], query: str) -> list[dict]:
    """Drop junk: search engines sometimes answer bots with random pages. Keep results that mention the query."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 4] or \
        re.findall(r"[a-z0-9]+", query.lower())
    if not words:
        return results
    return [r for r in results if any(w in f"{r['title']} {r['snippet']} {r['url']}".lower() for w in words)]


def search(query: str, limit: int = 8) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    errors = []
    for url, parser, wait in ((f"https://html.duckduckgo.com/html/?q={q}", parse_ddg, 8),
                              (f"https://www.bing.com/search?q={q}&setlang=en&cc=CA", parse_bing, 15)):
        try:
            res = relevant(parser(fetch(url, timeout=wait)[1]), query)
            if res:
                return res[:limit]
            errors.append(f"{url.split('/')[2]} gave no matching results")
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return []


@T.tool("web_search", "Search the internet and get REAL results back (titles, links, snippets). Then use "
        "browse_web on a result to read the full page. open_in_browser=true also shows the search on screen.",
        {"type": "object", "properties": {"query": {"type": "string"},
                                          "open_in_browser": {"type": "boolean"}}, "required": ["query"]})
def web_search(ctx, query: str, open_in_browser: bool = False):
    if open_in_browser:
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
    try:
        results, err = search(query), ""
    except Exception as e:
        results, err = [], str(e)
    if not results:
        try:  # plain search was blocked: use the real browser on the PC instead, if it is set up
            from . import computer

            if computer.available():
                return computer.browser_open(ctx, "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(query))
        except Exception:
            pass
        return f"Error: web search failed: {err}" if err else f"No results found for '{query}'."
    lines = [f"Web results for '{query}':"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} - {r['url']}" + (f"\n   {r['snippet'][:300]}" if r["snippet"] else ""))
    lines.append("(Call browse_web with a URL to read a full page.)")
    return "\n".join(lines)


# Names small models invent for these tools - mapped so the call still works.
T.ALIASES.update({"web_browse": "browse_web", "browse": "browse_web", "fetch_url": "browse_web",
                  "fetch_page": "browse_web", "read_url": "browse_web", "read_webpage": "browse_web",
                  "open_url_and_read": "browse_web", "google_search": "web_search", "search_web": "web_search",
                  "internet_search": "web_search"})
