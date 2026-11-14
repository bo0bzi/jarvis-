"""Brain split: decide, per request, whether the fast free cloud brain (freellmapi) or the local brain answers.

General questions, chat, web facts, timers, opening apps -> free cloud (about 3 s, no GPU load).
Anything that touches the owner's private data (notes, texts, email, customers, jobs, files, screen, passwords, money) ->
local brain, so that data never leaves the PC. If the free brain fails or is rate-limited the local brain answers, and the
other way round. Reasoning and memory questions (wants_reasoning) skip the free brain and go to Abacus, which shares the
memory. Order when a stronger brain is needed: free cloud -> Abacus -> Claude (last). BRAIN_SPLIT=false turns this off."""
from __future__ import annotations

import re

from .env import env_bool

_PRIVATE = re.compile(
    r"\b(my|our|mine)\b[^.?!]{0,60}\b(notes?|texts?|sms|e-?mails?|inbox|messages?|customers?|clients?|jobs?|invoices?|"
    r"orders?|files?|folders?|downloads?|documents?|contacts?|phone|number|password|passwords|pin|bank|account|balance|"
    r"credit|card|ssn|address|schedule|calendar|appointments?|reminders?|parts?|prices?|margins?|screen|clipboard)\b|"
    r"\b(screen|clipboard|\.env|api key|password|passcode|login|credentials?|read (?:the )?(?:file|folder)|in my downloads)\b|"
    r"\b(change yourself|teach yourself|learn (?:a )?skill|remember that|forget (?:that|about)|lesson)\b", re.I)


# Questions that need thinking it through, or recalling what was said before: these go to Abacus (the backup brain),
# which sees the same memory. Short commands and plain facts never match.
_REASONING = re.compile(
    r"\b(why (?:is|are|does|do|did|would|should|can'?t|won'?t)|explain|compare|pros and cons|trade-?offs?|"
    r"plan (?:out|for|to)|game ?plan|strategy|analy[sz]e|figure out|work out|step by step|think (?:through|about|it over)|"
    r"should (?:i|we)|what(?:'s| is| would be) the best (?:way|option|approach)|how (?:should|would) (?:i|we|you)|"
    r"diagnos\w*|troubleshoot\w*|recommend|which (?:one )?(?:is|would be) better|what do you think|decide|"
    r"what(?:'s| is) wrong with|make sense of|summari[sz]e (?:what|our|everything))\b", re.I)
_MEMORY = re.compile(
    r"\b(do you remember|what did (?:i|we|you) (?:say|tell|talk|discuss|decide|agree|mention)|last time (?:i|we|you)|"
    r"(?:earlier|before|yesterday|last (?:night|week)) (?:i|we|you) (?:said|told|talked|asked|mentioned|decided)|"
    r"we (?:talked|spoke|discussed|agreed) (?:about|on)|remind me what|what was (?:that|the) .{0,40}(?:i|we|you) "
    r"(?:mentioned|said|talked about)|(?:like|as) (?:i|we) (?:said|discussed) (?:before|earlier)|"
    r"what have (?:i|we) (?:been )?(?:talking|working) (?:about|on))\b", re.I)


_SECRETS = re.compile(r"\b(passwords?|passcode|api[ _-]?keys?|\.env\b|credentials?|log ?in|sign ?in|ssn|social security|bank|"
                      r"credit card|card number|\bpin\b|secret)\b", re.I)


def touches_secrets(user_text: str) -> bool:
    """Requests about passwords, keys, logins or bank details never go to the online AI, not even in Teach mode."""
    return bool(_SECRETS.search(user_text or ""))


def wants_reasoning(user_text: str) -> bool:
    """True for a real reasoning or memory question (at least 5 words), which Abacus should answer."""
    t = (user_text or "").strip()
    if len(t.split()) < 5 or len(t) > 4000:
        return False
    return bool(_REASONING.search(t) or _MEMORY.search(t))


def enabled() -> bool:
    return env_bool("BRAIN_SPLIT", True)


def wants_free(user_text: str) -> bool:
    """True when this request is safe and sensible to send to the free cloud brain."""
    t = (user_text or "").strip()
    if not t or len(t) > 1500:
        return False
    return not _PRIVATE.search(t)
