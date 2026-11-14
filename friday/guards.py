"""Honesty guards: catch a reply that claims work nobody did, or answers about the owner's own data with no tool
behind it. Kept apart from the Brain so routing, the prompt and the tests share one set of rules without importing
the model client."""
from __future__ import annotations

import re

ESCALATE_TOKEN = "ESCALATE"
STUCK_REPLY = "I got stuck in a loop of actions, so I stopped. Could you rephrase?"
# A reply that promises work ("installing it now", "let me fetch...") although no tool ran = a fake promise.
_PROMISE_RE = re.compile(r"\b(i'?ll|i will|i'?m (now )?(going to|about to|installing|fetching|pulling|downloading|"
                         r"checking|working|grabbing|setting)|let me|stand ?by|give me a (moment|sec|second|minute)|"
                         r"one moment|hang tight|on it|working on it|pulling (it|that) down|installing|fetching|"
                         r"downloading)\b", re.I)
_ACTION_RE = re.compile(r"\b(install|instal|download|fetch|find|search|get|list|open|close|send|text|email|remind|"
                        r"set|play|check|look|run|start|stop|delete|add|update|make|create|show|pull|grab|connect|fix|repair|review|"
                        r"analy[sz]e|go through|configure|setup|test|try|read|write|call|upload|sync|scan)\b", re.I)
NUDGE = ("You did NOT call any tool, so nothing happened - never say something was sent, saved, configured or done "
         "unless a tool result this turn says so. Don't promise work or make excuses: you CAN use the internet "
         "(browse_web, web_search, browser_open), and you CAN text through Quo: text_me texts the owner, "
         "send_text_message texts someone else. To get a program use winget_search then install_app; to save a "
         "file use download_file and use the path it returns. You never need Twilio or any extra setup. Call the "
         "right tool now "
         "(or load_tools to get more tools), or say plainly that you can't do it.")
NUDGE_WEB = ("You already have the live web data in the conversation above - it was fetched for you. Do NOT say you "
             "will search, open or navigate anything. Read that data and give the user the actual answer now, in a "
             "few plain sentences. If the data really doesn't contain it, say exactly that.")
HONEST_FAIL = ("I couldn't do that one: I kept saying I would without actually doing it, so nothing was done. "
               "Try rephrasing it, or say 'think hard' and I'll hand it to a stronger AI.")


def is_guard_fail(reply: str) -> bool:
    """True for the replies that mean 'the local AI kept faking' (so a stronger brain should take over)."""
    r = (reply or "").strip()
    return r == HONEST_FAIL or r.startswith("I need to correct myself:")


# "I can't browse / no internet access / reconstruct from memory" excuses although browse_web exists.
_EXCUSE_RE = re.compile(r"(can'?t|cannot|can not|unable to|not able to|don'?t have( the)? (ability|access)( to)?)\s+"
                        r"(directly\s+)?(browse|access|visit|open|fetch|load|reach|navigate|see)\b[^.]{0,40}"
                        r"(internet|web|site|page|url|link|browser|online)|"
                        r"(no|without) (internet|web|live web|browsing) access|browser automation permission|"
                        r"environment (is )?block|reconstruct(ion)? from memory|from my training data|"
                        r"search-indexed version|cached (html|version)", re.I)
_WEBISH_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(com|ca|net|org|io|app|dev|co)\b|\b(website|site|web ?page|"
                        r"browse|internet|online|search|look up|browser|click|screen)\b", re.I)


# Claiming an action is DONE when no tool ran ("the weather update has been sent", "configured successfully").
_CLAIM_RE = re.compile(r"\b(has|have|had) (now )?been (sent|configured|saved|installed|set up|created|updated|completed|"
                       r"added|scheduled|deleted)\b|\bi('ve| have) (just )?(sent|configured|saved|installed|set up|created|"
                       r"updated|added|scheduled|texted)\b|\bsuccessfully (sent|configured|installed|saved|set up)\b|"
                       r"\b(was|is now) (sent|configured|installed)\b|\bshould receive the (message|text)\b", re.I)
_QUESTION_RE = re.compile(r"^\s*(did|do|does|have|has|was|were|is|are|what|why|how|when|where|who|which|can you tell)\b", re.I)
# Inventing a blocker for texting: Jarvis texts through Quo (text_me / send_text_message), never Twilio.
_TEXT_EXCUSE_RE = re.compile(r"twilio|sms gateway|account sid|auth(entication)? token|"
                             r"(unable|can'?t|cannot|not able) to send (an? )?(sms|text)|"
                             r"don'?t have (the )?(capability|ability|tool) to send", re.I)
_TEXTISH_RE = re.compile(r"\b(text|texts|sms|quo|message me)\b", re.I)


def _straight(s: str) -> str:
    """Curly quotes (I'll) -> straight quotes (I'll): models use both and the patterns below expect straight ones."""
    return ((s or "").replace("\u2019", "'").replace("\u2018", "'")
            .replace("\u201c", '"').replace("\u201d", '"'))


def is_fake_promise(user_text: str, reply: str) -> bool:
    user_text, reply = _straight(user_text), _straight(reply)
    if _EXCUSE_RE.search(reply or "") and _WEBISH_RE.search(user_text or ""):
        return True
    if _TEXT_EXCUSE_RE.search(reply or "") and _TEXTISH_RE.search(user_text or ""):
        return True
    if _CLAIM_RE.search(reply or "") and _ACTION_RE.search(user_text or "") and not _QUESTION_RE.match(user_text or ""):
        return True
    return bool(_PROMISE_RE.search(reply or "")) and bool(_ACTION_RE.search(user_text or ""))


# Claiming a specific action happened, but none of the tools that could do it ran during this request.
_CLAIM_BACKING = [
    ("downloaded", re.compile(r"\b(i('ve| have)( just)? downloaded|(has|have) (now )?been downloaded|successfully "
                              r"downloaded|downloaded successfully|finished downloading|is (now )?downloaded)\b", re.I),
     {"download_file", "install_app", "install_github_skill", "run_shell_command", "browser_click"}),
    ("installed", re.compile(r"\b(i('ve| have)( just)? installed|(has|have) (now )?been installed|successfully "
                             r"installed|installed successfully|finished installing|installation (is )?complete)\b", re.I),
     {"install_app", "install_github_skill", "run_shell_command", "learn_skill"}),
    ("opened", re.compile(r"\b(i('ve| have)( just)? opened|(has|have) (now )?been opened|successfully opened|"
                          r"(?:^|[.!?]\s+|i'?m |now )opening (?!hours\b)\S)", re.I),
     {"open_app", "open_file", "open_website", "browser_open", "play_youtube", "run_shell_command", "open_dashboard"}),
    ("sent", re.compile(r"\b(i('ve| have)( just)? (sent|texted|emailed)|(has|have) been (sent|texted|emailed)|"
                        r"successfully sent)\b", re.I),
     {"text_me", "send_text_message", "send_email", "reply_email", "text_customer_bike_ready"}),
    ("saved", re.compile(r"\b(?:note|fact|reminder|event|job|that|it|information|details?) (?:has been |was |is |now )?"
                         r"(?:saved|added|recorded|logged|stored|remembered|noted)\b|\b(?:i'?ve|i have|i|got it[,.!]? i)(?:'ll| will)? "
                         r"(?:just |now )?(?:saved|noted|recorded|logged|stored|added|remember)\b|\b(?:note|fact) (?:saved|added|remembered)\b|"
                         r"\bfact remembered\b", re.I),
     {"add_note", "remember_fact", "add_job", "update_job", "add_event", "teach_way_to_think", "add_parts_reminder",
      "set_reminder", "set_timer", "learn_skill", "add_to_knowledge", "forget_fact", "evolve_now"}),
    ("clicked or pressed", re.compile(r"\b(?:mouse ?click(?:ed)?\s*:|key ?pressed\s*:|pressed\s*:\s*'|status\s*:\s*\w+ button clicked|"
                                      r"(?:i'?ve|i have|i)\s+(?:just\s+)?(?:clicked|pressed)\b|approval sent|"
                                      r"(?:button|yes|enter|key) (?:was |has been )?(?:clicked|pressed))", re.I),
     {"mouse_click", "press_keys", "keyboard_type", "browser_click"}),
    ("minimized or closed", re.compile(r"\b(?:all )?windows? (?:minimi[sz]ed|closed)\b|\bdesktop clean\b|\b(?:i'?ve|i have) (?:just )?"
                                       r"(?:minimi[sz]ed|closed|muted|locked)\b", re.I),
     {"press_keys", "mouse_click", "run_shell_command", "close_app", "lock_pc", "mute_mic", "set_volume"}),
    ("changed", re.compile(r"\b(?:change confirmed|result\s*:\s*change|i'?ve (?:just )?changed myself|"
                           r"(?:now )?responds? to .{1,40} with)\b", re.I),
     {"change_myself", "teach_way_to_think", "learn_skill"}),
    ("set", re.compile(r"\b(?:(?:i'?m |i am |i'?ll be |now )?(?:setting|starting) (?:a |an |the |your )?(?:\w+[- ]){0,3}"
                       r"(?:alarm|timer|reminder)|(?:i'?ve|i have) (?:just )?set (?:a |an |the |your )?(?:\w+[- ]){0,3}"
                       r"(?:alarm|timer|reminder)|(?:alarm|timer|reminder) (?:is |has been )?(?:now )?set|"
                       r"i'?ll remind you)\b", re.I),
     {"set_timer", "set_reminder"}),
]


# Questions about the owner's OWN data. A small model answers these from imagination (invented emails, texts, jobs, phone
# numbers), so a reply with no tool behind it is never accepted.
_DATA_Q = re.compile(r"\b(?:my|our|the shop'?s?)\b[^.?!]{0,40}\b(?:e-?mails?|inbox|texts?|text messages?|sms|messages?|notes?|reminders?|"
                     r"timers?|jobs?|customers?|appointments?|invoices?|orders?|downloads?(?: folder)?|files?|folders?|"
                     r"phone number|contacts?|parts?|schedule|balance|credits)\b|"
                     r"\b(?:who (?:texted|emailed|called)|what (?:texts?|e-?mails?|messages?) (?:did i|have i|do i)|"
                     r"do i have any (?:new |unread )?(?:e-?mails?|texts?|messages?))\b", re.I)
DATA_NUDGE = ("That was about the owner's own data and you answered WITHOUT reading it, so it may be invented. Call the right "
              "tool now (read_notes, list_facts, check_email, list_jobs, list_reminders, list_parts_reminders, search_memory, "
              "list_files, credit_status...). Answer ONLY from the tool result. If no tool can read it, say plainly that you can't check that.")
DATA_FAIL = "I can't check that right now, and I won't guess. Tell me where it lives (notes, email, shop jobs...) or ask me to look it up another way."


_LOOKUP_START = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:(?:jarvis|friday),?\s*)?(?:please\s+|can you\s+|could you\s+)?"
                           r"(?:what|which|who|whose|how many|how much|do i|did i|have i|are there|is there|any|show|list|read|"
                           r"check|tell me|give me|look up|find)\b", re.I)


def data_unbacked(user_text: str, tools_ran: list[str]) -> bool:
    """A lookup question about the owner's own data answered with no tool call (so it would be invented)."""
    t = _plain(user_text)
    return not tools_ran and bool(_LOOKUP_START.match(t)) and bool(_DATA_Q.search(t))


def _honest_fail(verb: str = "") -> str:
    if not verb:
        return HONEST_FAIL
    return (f"I need to correct myself: I said something was {verb}, but no tool actually did that, so it did NOT "
            "happen. Tell me again what you want and I'll do it for real.")


def unbacked_claim(reply: str, tools_ran: list[str]) -> str:
    """'downloaded' / 'installed' / 'opened' / 'sent' when no tool that can do that ran: returns the verb, else ''."""
    ran, reply = set(tools_ran or []), _straight(reply)
    for verb, pattern, tools in _CLAIM_BACKING:
        if pattern.search(reply or "") and not (ran & tools):
            return verb
    return ""


# Requests that need the PC to DO something (so tools must stay available even if web data was fetched).
_DOES_THINGS_RE = re.compile(r"\b(open|install|download|click|type|send|text|email|delete|remove|run|play|close|launch|"
                             r"start|stop|set|remind|save|write|create|shut ?down|restart|lock|screenshot)\b", re.I)

_AFFIRM_RE = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|proceed|please do|go|continue|confirmed?|"
                        r"correct|that'?s right|sounds good|yes please|go for it)\W*$", re.I)


def resolve_short_reply(user_text: str, history: list[dict]) -> str:
    """'yes' / 'go ahead' alone loses its meaning for a small model. Spell out what it is answering and tell it to act."""
    if not _AFFIRM_RE.match(user_text or ""):
        return user_text
    last = next((m["content"] for m in reversed(history) if m.get("role") == "assistant"), "")
    if not last:
        return user_text
    return (f"{user_text}\n\n[Context: that is the user's answer to your last message: \"{last[:400]}\". If you "
            "proposed an action, do it NOW by calling the right tool. Don't ask again and don't repeat yourself.]")


_CLOCK_RE = re.compile(r"\n\[clock: [^\]]*\]\s*$")


def _plain(text: str) -> str:
    """The user's message without the [clock: ...] marker the brain adds (tool picking, lessons and guards use this)."""
    return _CLOCK_RE.sub("", text or "")
