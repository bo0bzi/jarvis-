"""Automatic live web access for the brain.

Small local models often fail to call web tools, or ignore them and make things up. So before the model
answers, Friday looks at the message itself:
  - a website/link in it  -> read that page now (real browser for JavaScript sites)
  - a "search / look up / latest / news / price / who is ..." question -> run a real web search and read
    the best result
and hands the REAL text to the model as context, so it only has to answer from facts.
WEB_PREFETCH=off in .env turns this off."""
from __future__ import annotations

import re
import time

from . import tools as T
from .env import env

MAX_CHARS = 6500  # total live data given to the model per message

_URL = re.compile(r"((?:https?://)?(?:www\.)?[a-z0-9][\w-]*(?:\.[\w-]+)*\.(?:com|ca|net|org|io|app|dev|co|us|info|biz|"
                  r"ai|shop|store|me|tv|gg|xyz)(?:/[^\s,;)\"']*)?)", re.I)
_SKIP_URL = re.compile(r"github\.com|(?:^|/)[\w-]+\.(?:py|js|json|txt|md|bat|ps1|exe|dll)$", re.I)
_SEARCH = re.compile(
    r"\b(search|google|look ?up|find (?:out|me|info)|latest|news|today'?s|current(?:ly)?|right now|price of|how much "
    r"(?:is|are|does)|who is|who was|what is the (?:best|latest|price|score)|review|reviews|research|"
    r"is .{2,40} (?:open|closed)|score|stock|release date|near me|compare|check (?:if|whether|to see)|"
    r"long weekends?|holidays?|are there any|is there any)\b", re.I)
# "go to google.com and check ..." means SEARCH, not "read Google's empty homepage"
_ENGINE = re.compile(r"(?:^|//|\.)(?:google|bing|duckduckgo|yahoo|ecosia|brave|startpage)\.(?:com|ca|net|org|co)\b", re.I)
_NOT_SEARCH = re.compile(r"\b(my (?:files?|folder|computer|pc|screen)|volume|remind|timer|alarm|email|inbox|text message|"
                         r"(?:switch|change|swap)\b.{0,25}\b(?:models?|brains?)|your (?:brain|models?)|weather|forecast|open (?:the )?app|shut ?down|restart|skills?|install|ram|memory|cpu|gpu|processes|"
                         r"programs? (?:using|running|open)|disk|drives?|storage|any ?desk|rust ?desk|wi-?fi|ip address)\b", re.I)
_BROWSE_WORDS = re.compile(r"\b(go ?to|visit|browse|check|look at|open|read|what'?s on|summari[sz]e|see)\b", re.I)


def enabled() -> bool:
    return env("WEB_PREFETCH", "on").lower() not in ("off", "false", "0", "no")


def urls_in(text: str) -> list[str]:
    found: list[str] = []
    for m in _URL.finditer(text or ""):
        u = m.group(1).rstrip(".!?")
        if _SKIP_URL.search(u):
            continue
        if u not in found:
            found.append(u)
    return found[:2]


def wants_search(text: str) -> bool:
    return bool(_SEARCH.search(text or "")) and not _NOT_SEARCH.search(text or "")


def _trim(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + "\n...(cut)"


def _first_result_url(search_text: str) -> str:
    m = re.search(r"^\s*1\. .*? - (https?://\S+)", search_text, re.M)
    return m.group(1) if m else ""


_CACHE: dict[str, tuple[float, str]] = {}


def _cached_dispatch(name: str, args: dict, ctx) -> str:
    """Same page / same search within a few minutes: reuse it (faster, and no repeat browser windows).
    Errors are never cached. WEB_CACHE_SECONDS=0 turns this off."""
    ttl = float(env("WEB_CACHE_SECONDS", "300") or 0)
    key = name + "|" + repr(sorted(args.items()))
    hit = _CACHE.get(key)
    if ttl > 0 and hit and time.time() - hit[0] < ttl:
        return hit[1]
    out = T.dispatch(name, args, ctx)
    if ttl > 0 and not str(out).startswith("Error"):
        _CACHE[key] = (time.time(), out)
        if len(_CACHE) > 60:
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    return out


def gather(brain, user_text: str) -> str:
    """Live web text relevant to this message, or '' if the message needs none."""
    if not enabled():
        return ""
    ctx = brain.ctx
    parts: list[str] = []
    budget = MAX_CHARS
    urls = urls_in(user_text)
    engines = [u for u in urls if _ENGINE.search(u)]
    urls = [u for u in urls if u not in engines]
    core = _query_core(user_text) if engines else ""
    search_wanted = (bool(engines) and len(re.findall(r"[A-Za-z]{3,}", core)) >= 2) or wants_search(user_text)
    if urls and "browse_web" in T.REGISTRY:
        for u in urls:
            brain.on_tool("browse_web", {"url": u})
            page = _cached_dispatch("browse_web", {"url": u}, ctx)
            parts.append(f"=== PAGE READ LIVE: {u} ===\n" + _trim(page, budget // len(urls)))
    elif search_wanted and "web_search" in T.REGISTRY:
        brain.on_tool("web_search", {"query": user_text[:120]})
        prev = ""
        try:
            hist = brain.memory.history() if brain.memory is not None else []
            prev = next((m["content"] for m in reversed(hist) if m["role"] == "user"), "")
        except Exception:
            pass
        res = _cached_dispatch("web_search", {"query": _query(user_text, prev)}, ctx)
        parts.append("=== WEB SEARCH RESULTS ===\n" + _trim(res, 2200))
        top = _first_result_url(res)
        if top and "browse_web" in T.REGISTRY and not res.startswith(("Error", "BROWSER")):
            brain.on_tool("browse_web", {"url": top})
            page = _cached_dispatch("browse_web", {"url": top}, ctx)
            parts.append(f"=== TOP RESULT READ LIVE: {top} ===\n" + _trim(page, budget - 2200))
    if not parts:
        return ""
    try:
        brain._trace("web", f"fetched live web data for the AI ({sum(len(p) for p in parts):,} chars)",
                     "\n\n".join(p[:500] for p in parts))
    except Exception:
        pass
    return ("LIVE WEB DATA, fetched just now by your own tools for the user's next message. This is real. Answer "
            "from it, be specific, never say you can't browse, and don't invent anything it doesn't contain:\n\n"
            + "\n\n".join(parts))


_VAGUE = re.compile(r"\b(that|this|it|those|them|these|there|the same|how to do that|do that)\b", re.I)


def _query(text: str, prev_user: str = "") -> str:
    """The search words. 'lookup how to do that' means nothing alone, so a vague request borrows the topic of the
    user's previous message ('how can we set you up to receive phone calls through Quo')."""
    q = _query_core(text)
    if prev_user and _VAGUE.search(text) and _content_words(q) <= 2:  # only when the request has almost no topic of its own
        q = (prev_user.strip(" ?.!")[:110] + " " + q)[:170]
    return q


_FILLER = {"that", "this", "those", "them", "these", "there", "with", "give", "please", "about", "tell", "show", "from",
           "into", "what", "have", "would", "could", "should", "need", "want", "like", "some", "something", "then"}


def _content_words(q: str) -> int:
    """How many real topic words (4+ letters, not filler or 'that/this/it') a request has."""
    return len([w for w in re.findall(r"[a-z]{4,}", q.lower()) if w not in _FILLER and not _VAGUE.fullmatch(w)])


def _query_core(text: str) -> str:
    q = re.sub(r"^(?:hey |ok |okay )?(?:friday|jarvis)[,!]?\s*", "", text.strip(), flags=re.I)
    q = _URL.sub(" ", q)  # 'go to google.com, check ...' -> the web address is not part of the question
    q = re.sub(r"^(?:[\s,.:;-]|can you |could you |please |go ahead and |go to |open |visit |and |then |use |on |in )+", "",
               q, flags=re.I)
    q = re.sub(r"^(?:search(?: the (?:web|internet))?(?: for)?|google|look ?up|find (?:out|me))\s+", "", q, flags=re.I)
    q = re.sub(r"^check (?:if|whether|to see (?:if|whether)?)\s+(?:there (?:is|are)\s+)?(?:any\s+)?", "", q, flags=re.I)
    return q.strip(" ?.!,")[:150] or text[:150]


def history_with_web(brain, history: list[dict], user_text: str) -> list[dict]:
    """history plus (if needed) a user/assistant pair that carries the live data. Never stored in memory."""
    try:
        data = gather(brain, user_text)
    except Exception as e:  # never let web trouble break chatting
        data = f"(Live web lookup failed: {type(e).__name__}: {str(e)[:150]}. Tell the user exactly this.)"
    if not data:
        return history
    return [*history, {"role": "user", "content": data},
            {"role": "assistant", "content": "Understood. I'll answer from that live data."}]
