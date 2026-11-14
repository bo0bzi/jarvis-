"""Direct commands that never go through the AI, so a small local model can't fumble them:
'install <GitHub link>', 'list skills', 'status / update?' about background jobs, and a bare website link
(read it) or 'open <site> in the browser'.
Works the same from the PC, the dashboard, the phone page and text messages."""
from __future__ import annotations

import re

from . import jobs
from . import tools as T
from .env import env

_SMS_PREFIX = re.compile(r"^\(sent by text message[^)]*\)\s*", re.I)
_URL = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?(?=[/\s?#,!)]|\.(?:\s|$)|$)", re.I)
_INSTALL = re.compile(r"\b(?:install|instal|isntall|intall|insall|instll|add|grab|pull|download|set ?up)\b", re.I)
_OWNER_REPO = re.compile(r"\b(?:install|instal|isntall|intall|insall|instll)\s+(?:the\s+)?(?:skill\s+pack\s+|pack\s+|repo\s+)?"
                         r"([A-Za-z0-9][\w.-]*/[\w.-]+)\b", re.I)
_LIST = re.compile(r"^(?:can you |please |hey friday,? |friday,? )*(?:list|show|tell me|what are)(?: me)?(?: all)?"
                   r"(?: of)?(?: your| my| the| installed)*(?: installed)? skills?(?: you have| installed)?\s*$|"
                   r"^what skills (?:do you have|are installed|have you got|you got)\s*$", re.I)
_STATUS = re.compile(r"^(?:status|update|updates|any update|any updates|progress|im waiting|i'?m waiting|still waiting|"
                     r"is it done|is it finished|did it finish|done yet|how'?s it going|how is it going)\b|"
                     r"\b(?:update|status|progress) (?:on|of) (?:the )?(?:skill|pack|install|download|job)", re.I)


_WEB_URL = r"((?:https?://)?(?:www\.)?[a-z0-9][\w-]*(?:\.[\w-]+)*\.(?:com|ca|net|org|io|app|dev|co|us|info|biz|ai|shop)(?:/[^\s]*)?)"
_READ = re.compile(r"^(?:hey friday,? |friday,? |can you |please |could you )*(?:read|check|browse|visit|look at|"
                   r"fetch|what'?s on|whats on|summarize|summarise)?\s*(?:the )?(?:site |website |page )?"
                   + _WEB_URL + r"\s*(?:for me|please|now)?$", re.I)
_OPEN_IN_BROWSER = re.compile(r"^(?:hey friday,? |friday,? |can you |please )*(?:open|go to|load)\s+" + _WEB_URL +
                              r"\s+in (?:the |my |your )?(?:browser|chrome|edge)\s*(?:please)?$", re.I)


_GO_TO = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),? |can you |could you |please )*(?:go to|open|pull up|launch)\s+"
                    + _WEB_URL + r"\s*(?:for me|please|now)?$", re.I)

# Teaching: "teach: always check the manuals first", "from now on, double-check prices", "lesson: ..."
_TEACH = re.compile(r"^(?:(?:hey )?(?:friday|jarvis)[,!]?\s+)?(?:teach(?: you)?|lesson|new lesson|learn this|new rule|"
                    r"rule|way of thinking)\s*[:,-]\s*(.{8,})$|^(?:(?:hey )?(?:friday|jarvis)[,!]?\s+)?"
                    r"(from now on[,:]?\s+.{8,})$", re.I | re.S)
_LESSONS_LIST = re.compile(r"^(?:what (?:have i|did i) taught you|what (?:lessons|rules) (?:do you have|did i teach)|"
                           r"(?:list|show) (?:me )?(?:my |your |the )?(?:lessons|rules|ways of thinking)|"
                           r"how (?:do )?i want you to think)\W*$", re.I)
_EVOLVE = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),? )?(?:please |go (?:ahead )?and )?(?:evolve|self[- ]?improve|"
                     r"evolve now|learn and evolve|learn from (?:today|yesterday|the day|your mistakes|the logs)|go learn|"
                     r"study (?:the )?(?:logs|today|your mistakes)|train yourself|improve yourself|learn tonight)"
                     r"(?: now| tonight| please)?\W*$", re.I)
_EVOLVE_REPORT = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),? )?(?:what (?:did|have) you learn(?:ed)?(?: last night| "
                            r"today| lately| so far)?|show (?:me )?(?:the )?(?:learning|evolve) report)\W*$", re.I)
_LESSON_FORGET = re.compile(r"^(?:forget|delete|remove) (?:the )?(?:lesson|rule) (?:#|number )?(.+)$", re.I)


# "send a text through quo", "text my customer", "send me an sms" -> if texting isn't set up, say exactly what's missing
_TEXT_INTENT = re.compile(r"\b(?:send|sending|write|compose)\b.{0,25}\b(?:a )?(?:text|texts|text message|sms)\b|"
                          r"\btext (?:me|my|him|her|them|the|a customer)\b|"
                          r"\b(?:text|texts|sms|texting)\b.{0,30}\bquo\b|\bquo\b.{0,30}\b(?:text|texts|sms|texting)\b", re.I)


# "what's the weather (in X) (right now/today)": straight to the weather tool. Tomorrow/weekend/week questions go to the AI.
_WEATHER_Q = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),? )?(?:please )?(?:what(?:'s| is| s)|how(?:'s| is)|tell me|give me|"
                        r"check)?\s*(?:the |today'?s |current )*(?:weather|temperature|forecast)(?: like)?"
                        r"(?: (?:in|for|at) (?P<city>[a-z][a-z .'-]{1,40}?))?(?: (?:right now|now|today|currently|outside|"
                        r"at the moment|tonight))?(?: please)?$", re.I)

_REMOTE_ID_Q = re.compile(r"\b(?:any ?desk|rust ?desk)\b.*\b(?:id|number|address|code)\b|\bmy (?:remote (?:desktop )?|any ?desk )"
                          r"(?:id|number)\b", re.I)

_P = r"^(?:(?:hey )?(?:jarvis|friday),?\s*)?(?:please |can you |could you )?"
_NOTE_Q = re.compile(_P + r"(?:add|make|take|save|write|jot)(?: down)? (?:a )?note(?: that| saying| to self| for me)?[:,]?\s+(?P<x>.{4,})$", re.I)
_REMEMBER_Q = re.compile(_P + r"remember (?:that |this[:,]? )?(?!to\b)(?P<x>.{6,})$", re.I)
_MODE_Q = re.compile(_P + r"(?:(?P<on>go online|switch to online(?: mode)?|online mode|use claude(?: as your (?:main )?brain)?|"
                          r"use the cloud|switch to claude|go cloud)|(?P<loc>go local|go offline|local mode|offline mode|local only|"
                          r"stay local|only use (?:the )?local|switch to local(?: mode)?|stop using (?:the )?(?:cloud|claude))|"
                          r"(?P<auto>back to auto|auto mode|go auto|normal mode|back to normal|automatic mode))\s*(?:please|now|mode)?$", re.I)
_STATUS_Q = re.compile(_P + r"(?:what(?:'s| is) your (?:mode|brain|model)(?: status)?|what mode are you in|which (?:ai|brain|model) "
                            r"(?:are you|is) (?:using|active|running)|brain status|what are you running on)\??$", re.I)
_MODELS_Q = re.compile(_P + r"(?:list (?:your |the )?(?:local )?models|what (?:local )?models do you have|which (?:local )?models "
                            r"(?:are )?(?:installed|downloaded)|show (?:me )?(?:your |the )?models|"
                            r"(?:which|what) (?:local |ai )?models (?:do you have|are available|can you use)|"
                            r"what (?:ai )?models (?:are )?(?:on|in) (?:my|this) (?:pc|computer))\??$", re.I)
_FOLDER_Q = re.compile(_P + r"(?:(?:what(?:'s| is) in|show(?: me)?|list(?: what(?:'s| is) in)?|open and list)\s+)?(?:my |the )?"
                            r"(?P<f>downloads|desktop|documents|pictures|music|videos)(?: folder)?(?: right now)?\??$", re.I)
_DATA_ROUTES = [
    (re.compile(_P + r"(?:what|which) (?:texts?|text messages|messages|sms)(?: did i| have i|\b.{0,20}\b(?:get|got|receive[d]?))?.{0,25}"
                     r"(?:today|recently|lately|new)?\??$|(?:check|read|show) (?:me )?(?:my |the )?(?:new |recent )?(?:texts|text messages|sms)\.?$|"
                     r"(?:do i have|any) (?:any )?(?:new )?(?:texts|text messages)\??$", re.I), "read_texts", {}),  # personal-data questions are answered from the real store, never from the AI's imagination
    (re.compile(_P + r"(?:what (?:are|is) |show (?:me )?|read (?:me )?|list |give me )?(?:my |the |all )?(?:saved )?notes(?: do i have| i saved)?\??$", re.I), "read_notes", {}),
    (re.compile(_P + r"(?:what (?:are|is) |show (?:me )?|list |give me )?(?:my |the |all )?(?:active |running )?(?:timers|alarms|reminders)"
                     r"(?: do i have| are set| are running| do i have set| are active)?\??$|how many (?:timers|alarms|reminders) do i have(?: running| set)?\??$", re.I), "list_reminders", {}),
    (re.compile(_P + r"(?:what (?:are|is) |show (?:me )?|list |give me )?(?:my |the |all |open )*parts reminders\??$", re.I), "list_parts_reminders", {}),
    (re.compile(_P + r"(?:what do you (?:remember|know) about me|what (?:facts|things) (?:do you|have you) (?:remember|stored|saved)(?: about me)?|"
                     r"what did i tell you to remember)\??$", re.I), "list_facts", {}),
    (re.compile(_P + r"(?:do i have|check|any|are there)(?: any)? (?:new |unread )?e-?mails?(?: do i have| today| right now)?\??$|"
                     r"what(?:'s| is) in my inbox\??$|check my (?:e-?mail|inbox)\.?$", re.I), "check_email", {}),
]
_JOBS_Q = re.compile(_P + r"(?:what (?:are|is) |show (?:me )?|list |give me )?(?:my |the |all )?(?:open |current )?(?:shop )?jobs"
                          r"(?: (?:do i have|are open|are there|right now))?\??$", re.I)
_CAPS_Q = re.compile(_P + r"(?:what can you do|what are you able to do|what are your (?:abilities|skills|features)|help me with)\??$", re.I)
_SWITCH_MODEL = re.compile(_P + r"(?:switch|change) (?:your |the )?(?:main |local )?(?:model|brain) to (?P<m>[\w.:\-]+)\s*$", re.I)
_SCREEN_Q = re.compile(_P + r"(?:what(?:'s| is) on (?:my |the )?screens?|(?:look at|read|check) (?:my |the )?screens?|"
                            r"what (?:do you|can you) see(?: on (?:my |the )?screens?)?|can you see (?:my |the )?screens?)\??$", re.I)
_CONTROL_ON = re.compile(_P + r"(?:turn on|enable|allow|start) (?:computer |pc )?control(?: mode)?(?: for (?P<n>\d{1,3}) ?(?:min|minutes))?\.?$", re.I)
_CONTROL_OFF = re.compile(_P + r"(?:turn off|disable|stop) (?:computer |pc )?control(?: mode)?\.?$", re.I)
_DESKTOP_Q = re.compile(_P + r"(?:minimi[sz]e (?:everything|all(?: (?:my )?windows)?)(?: on my (?:desktop|screen))?|"
                             r"show (?:me )?(?:the |my )?desktop|clear (?:my |the )?desktop)\.?$", re.I)
_DIGEST_Q = re.compile(_P + r"(?:(?:give me|show me|read|send me) )?(?:the |my )?(?:daily |evening )?digest\.?$|"
                            r"what did you (?:do|get up to) (?:today|in the last day)\??$", re.I)
_LESSON_REVIEW_Q = re.compile(_P + r"(?:review|check|audit) (?:your |my |the )?(?:lessons|what i taught you|ways of thinking)\.?$", re.I)
_SWITCH_VAGUE = re.compile(r"\b(?:switch|change|swap|pick|choose)\b.{0,25}\b(?:ai|llm|local)? ?(?:models?|brains?)\b|"
                           r"\b(?:models?|brains?)\b.{0,25}\b(?:switch|change|swap)\b", re.I)
_SELF_CHANGE = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),?\s*)?(?:please |can you |could you |i want you to |i need you to )?"
                          r"(?:change|reprogram|rewrite|modify|upgrade|improve|update|fix|adjust|edit)\s+(?:yourself|your own code|"
                          r"your code|your programming)\b.{6,}", re.I)
_SELF_UNDO = re.compile(r"\b(?:undo|revert|roll ?back)\b.{0,25}\b(?:your|the) last (?:change|update|edit)\b|"
                        r"\brevert yourself\b", re.I)
_SELF_LIST = re.compile(r"\bwhat (?:changes )?(?:have you|did you) (?:made?|change)\b.{0,20}\b(?:yourself|your (?:own )?code)\b", re.I)
_AGENDA_Q = re.compile(r"^(?:(?:hey )?(?:jarvis|friday),?\s*)?(?:please )?(?:what(?:'s| is| do i have)|what do i have|show me|show|read|"
                       r"check|tell me|give me|any|do i have anything|am i booked)?\s*(?:(?:on|in) )?(?:my day|(?:my |the )?(?:calendar|"
                       r"schedule|agenda|appointments?|meetings?))(?: (?:for|on))?\s*(?P<when>today|tomorrow|"
                       r"this week|the week|next 7 days|monday|tuesday|wednesday|thursday|friday|saturday|sunday)?$|"
                       r"^(?:what'?s on|what do i have)\s*(?P<when2>today|tomorrow|this week)$", re.I)
_INSTALLED_Q = re.compile(r"^(?:do i have|have i got|is) (?P<a>[\w .+-]{2,40}?) (?:installed|on (?:my|this) (?:pc|computer))$|"
                          r"^is (?P<b>[\w .+-]{2,40}?) installed(?: on (?:my|this) (?:pc|computer))?$")
_DISK_Q = re.compile(r"\bhow much (?:free |disk |storage |hard drive )?(?:space|storage)\b|\b(?:free|left|available) (?:disk |drive |storage )?space\b|"
                     r"\b(?:disk|drive|storage) space\b|\bhow full (?:is|are) (?:my |the )?(?:drive|disk|c:|d:|storage)", re.I)
_TOP_PROCS_Q = re.compile(r"\b(?:biggest|top|most)\b.*\b(?:programs?|apps?|processes)\b.*\b(?:memory|ram|cpu)\b|"
                          r"\bwhat(?:'s| is) (?:using|eating|hogging) (?:my |the )?(?:memory|ram|cpu)\b|"
                          r"\bwhy is my (?:pc|computer) (?:so )?slow\b", re.I)

# Pure small talk: answered instantly, no AI. Anything longer or with a request in it still goes to the AI.
_SMALL_TALK = {"hi": "Yes, sir?", "hello": "Yes, sir?", "hey": "Yes, sir?", "yo": "Yes, sir?",
               "good morning": "Good morning, sir.", "good afternoon": "Good afternoon, sir.",
               "good evening": "Good evening, sir.", "good night": "Good night, sir.",
               "thanks": "Anytime, sir.", "thank you": "Anytime, sir.", "thanks a lot": "Anytime, sir.",
               "cheers": "Anytime, sir.", "are you there": "Here, sir.", "you there": "Here, sir.",
               "are you online": "Online and listening, sir.", "are you awake": "Always, sir.",
               "bye": "Goodbye, sir.", "goodbye": "Goodbye, sir.", "see you later": "Goodbye, sir."}

_TIME_Q = re.compile(r"^(?:(?:hey|ok|okay) )?(?:(?:friday|jarvis),? )?(?:please )?(?:what(?:'s| is| s) (?:the )?"
                     r"(?:current )?(?:time|date|day)(?: and (?:the )?(?:time|date))?|what time is it|what day is it|"
                     r"what(?:'s| is) today'?s date|what(?:'s| is) the date today)(?: right now| now| today| please)?$", re.I)


_INSTALL_APP = re.compile(
    r"^(?:(?:hey )?(?:jarvis|friday),? )?(?:(?:can|could|would) you |please |i want you to |i need you to |go )*"
    r"(?:download and install|install and download|download|install|get me|set ?up)\s+(?:the |a |an )?(.+?)"
    r"(?:\s+(?:for me|please|now|on (?:this|my|the) (?:pc|machine|computer|laptop)|"
    r"to (?:this|my|the) (?:pc|machine|computer|laptop)))*[\s?.!]*$", re.I)
_NOT_AN_APP = re.compile(r"skill|pack\b|github|repo\b|plugin|mcp|https?:|www\.|/|\.com|\bit\b|\bthat\b|\bthis\b|"
                         r"\bfile\b|\breport\b|\bupdate", re.I)


def _install_app_name(text: str) -> str:
    """'download and install vlc media player' -> 'vlc media player'; '' when it isn't a plain program request."""
    m = _INSTALL_APP.match(text.strip())
    if not m:
        return ""
    name = m.group(1).strip(" .,!?")
    if not name or len(name.split()) > 5 or _NOT_AN_APP.search(name):
        return ""
    return name


def _sms_setup_help() -> str:
    """'' if texting works (let the AI use its send_text_message tool), else exactly which settings are missing."""
    from .env import env, env_bool

    missing = []
    if not env_bool("SMS_ENABLED", False):
        missing.append("SMS_ENABLED=true")
    if not env("QUO_API_KEY"):
        missing.append("QUO_API_KEY (Quo app > Settings > API)")
    if not (env("FRIDAY_SMS_NUMBER") or env("QUO_PHONE_NUMBER_ID")):
        missing.append("FRIDAY_SMS_NUMBER (the Quo number to text from)")
    if not env("OWNER_PHONE"):
        missing.append("OWNER_PHONE (your own cell number)")
    if not missing:
        return ""
    return ("Texting through Quo isn't set up yet, so I can't send one. Still missing: " + "; ".join(missing) +
            ". Open the dashboard > Settings > SMS (Quo), fill those in and save. Your key is stored in .env and "
            "never shown. Then say 'text me test' and I'll send one.")


def _clean(text: str) -> str:
    return _SMS_PREFIX.sub("", (text or "").lstrip("﻿")).strip()


def repo_in(text: str) -> str:
    """The GitHub repo the user asked to install, or ''."""
    t = _clean(text)
    m = _URL.search(t)
    if m and _INSTALL.search(t):
        return m.group(1)
    m = _OWNER_REPO.search(t)
    return m.group(1) if m else ""


def handle(brain, text: str) -> str | None:
    """Run a direct command and return Friday's reply, or None to let the AI answer."""
    t = _clean(text)
    low = t.lower().strip(" .!?")
    repo = repo_in(t)
    if repo:
        ctx = brain.ctx
        ctx.explicit_repo = repo  # the user named this exact repo: a skill pack (instructions only) needs no 2nd ask
        try:
            brain.on_tool("install_github_skill", {"repo": repo})
            return T.dispatch("install_github_skill", {"repo": repo}, ctx)
        finally:
            ctx.explicit_repo = ""
    m = _TEACH.match(t)
    if m:
        from . import lessons

        it = lessons.add("", (m.group(1) or m.group(2)).strip(), ctx=brain.ctx)
        brain.on_trace("lessons", f"saved lesson #{it['id']}: {it['how'][:200]}", "")
        return f"Learned. Lesson {it['id']} saved: {it['how'][:160]}. I'll follow it from now on."
    if _LESSONS_LIST.search(low):
        return T.dispatch("list_ways_of_thinking", {}, brain.ctx)
    m = _LESSON_FORGET.match(low)
    if m:
        return T.dispatch("forget_way_of_thinking", {"which": m.group(1)}, brain.ctx)
    if _EVOLVE.match(low):  # "evolve" / "learn from today": review yesterday and write new rules (free, no credits)
        brain.on_tool("evolve_now", {})
        brain.on_trace("tool", "learning session: reviewing the last 24 hours")
        return T.dispatch("evolve_now", {}, brain.ctx)
    if _EVOLVE_REPORT.match(low):
        return T.dispatch("what_did_you_learn", {}, brain.ctx)
    wm = _WEATHER_Q.match(t.strip(" .!?"))  # original case, so city names keep their capitals
    if wm and "get_weather" in T.REGISTRY:  # live facts come from the tool, never from the AI's imagination
        city = (wm.group("city") or "").strip(" ,.")
        brain.on_tool("get_weather", {"city": city} if city else {})
        return T.dispatch("get_weather", {"city": city} if city else {}, brain.ctx)
    if _REMOTE_ID_Q.search(low) and "remote_desktop_id" in T.REGISTRY:  # read the real number, never guess it
        brain.on_tool("remote_desktop_id", {})
        return T.dispatch("remote_desktop_id", {}, brain.ctx)
    from . import timeparse

    nm = _NOTE_Q.match(t)
    if nm and "add_note" in T.REGISTRY:  # saved for real by the tool, never just "Note saved"
        brain.on_tool("add_note", {"text": nm.group("x").strip()})
        return T.dispatch("add_note", {"text": nm.group("x").strip()}, brain.ctx)
    rm = _REMEMBER_Q.match(t)
    if rm and "remember_fact" in T.REGISTRY:
        brain.on_tool("remember_fact", {"fact": rm.group("x").strip()})
        return T.dispatch("remember_fact", {"fact": rm.group("x").strip()}, brain.ctx)
    bm = _MODE_Q.match(low)
    if bm and "set_brain_mode" in T.REGISTRY:  # "go online" / "go local" / "back to auto": instant, no AI
        mode = ("online" if bm.group("on") else "local" if bm.group("loc") else "auto")
        brain.on_tool("set_brain_mode", {"mode": mode})
        return T.dispatch("set_brain_mode", {"mode": mode}, brain.ctx)
    if _STATUS_Q.match(low) and "brain_status" in T.REGISTRY:
        return T.dispatch("brain_status", {}, brain.ctx)
    if _MODELS_Q.match(low) and "list_local_models" in T.REGISTRY:
        return T.dispatch("list_local_models", {}, brain.ctx)
    if _SCREEN_Q.match(low) and "read_screen_text" in T.REGISTRY:  # real OCR + window titles (fast, exact; no vision model swap)
        brain.on_tool("read_screen_text", {})
        return T.dispatch("read_screen_text", {}, brain.ctx)
    fm = _FOLDER_Q.match(low)
    if fm and "list_folder" in T.REGISTRY:
        brain.on_tool("list_folder", {"folder": fm.group("f").title()})
        return T.dispatch("list_folder", {"folder": fm.group("f").title()}, brain.ctx)
    for pat, tool_name, targs in _DATA_ROUTES:
        if pat.match(low) and tool_name in T.REGISTRY:
            brain.on_tool(tool_name, targs)
            return T.dispatch(tool_name, targs, brain.ctx)
    if _JOBS_Q.match(low) and "list_jobs" in T.REGISTRY:
        brain.on_tool("list_jobs", {})
        return T.dispatch("list_jobs", {}, brain.ctx)
    if _CAPS_Q.match(low):
        return ("I can: answer questions and check live facts (time, weather, disk, memory); set timers, alarms and reminders; "
                "keep notes and remember facts; run your calendar and shop jobs; read your screen and, with your OK, click and "
                "type; control apps and files; search and read the web; read email; learn new skills and change my own code safely; "
                "and switch between local AI and Claude. Ask for any of them by name.")
    if _LESSON_REVIEW_Q.match(low) and "review_lessons" in T.REGISTRY:
        brain.on_tool("review_lessons", {})
        return T.dispatch("review_lessons", {}, brain.ctx)
    if _DIGEST_Q.match(low) and "daily_digest" in T.REGISTRY:  # real log, not an improvised "digest"
        brain.on_tool("daily_digest", {})
        return T.dispatch("daily_digest", {}, brain.ctx)
    cm = _CONTROL_ON.match(low)
    if cm and "allow_computer_control" in T.REGISTRY:
        args = {"minutes": int(cm.group("n"))} if cm.group("n") else {}
        brain.on_tool("allow_computer_control", args)
        return T.dispatch("allow_computer_control", args, brain.ctx)
    if _CONTROL_OFF.match(low) and "stop_computer_control" in T.REGISTRY:
        brain.on_tool("stop_computer_control", {})
        return T.dispatch("stop_computer_control", {}, brain.ctx)
    if _DESKTOP_Q.match(low) and "press_keys" in T.REGISTRY:  # a small model once claimed "minimized" without doing it
        brain.on_tool("press_keys", {"keys": "win+d"})
        return T.dispatch("press_keys", {"keys": "win+d"}, brain.ctx)
    sm = _SWITCH_MODEL.match(low)
    if not sm and _SWITCH_VAGUE.search(low) and "list_local_models" in T.REGISTRY:
        # "can you switch the AI models you're using as a brain?" -> never a web search: show what I can switch to
        brain.on_tool("list_local_models", {})
        return (T.dispatch("brain_status", {}, brain.ctx) + "\n" + T.dispatch("list_local_models", {}, brain.ctx) +
                "\nYes, I can switch. Say 'switch your model to <name>', 'go online' for Claude, or 'go local'.")
    if sm and "switch_local_model" in T.REGISTRY:
        brain.on_tool("switch_local_model", {"name": sm.group("m").strip()})
        return T.dispatch("switch_local_model", {"name": sm.group("m").strip()}, brain.ctx)
    if _SELF_CHANGE.match(low) and "change_myself" in T.REGISTRY:  # "change yourself so that ..." -> the safe pipeline
        brain.on_tool("change_myself", {"request": t[:400]})
        return T.dispatch("change_myself", {"request": t[:400]}, brain.ctx)
    if _SELF_UNDO.search(low) and "undo_last_change" in T.REGISTRY:
        brain.on_tool("undo_last_change", {})
        return T.dispatch("undo_last_change", {}, brain.ctx)
    if _SELF_LIST.search(low) and "list_self_changes" in T.REGISTRY:
        return T.dispatch("list_self_changes", {}, brain.ctx)
    am = _AGENDA_Q.search(low)
    if am and "list_events" in T.REGISTRY:  # read the real calendar, never invent appointments
        rng = (am.group("when") or am.group("when2") or "today").strip()
        brain.on_tool("list_events", {"range": rng})
        return T.dispatch("list_events", {"range": rng}, brain.ctx)
    tp = timeparse.parse(t)
    if tp and "set_timer" in T.REGISTRY and "set_reminder" in T.REGISTRY:  # really set it; never just say it was set
        if tp["kind"] == "timer":
            brain.on_tool("set_timer", {"seconds": int(tp["seconds"]), "label": tp["label"]})
            return T.dispatch("set_timer", {"seconds": int(tp["seconds"]), "label": tp["label"]}, brain.ctx)
        args = {"message": tp["message"], "in_minutes": tp["seconds"] / 60}
        brain.on_tool("set_reminder", args)
        return T.dispatch("set_reminder", args, brain.ctx)
    im = _INSTALLED_Q.search(low)
    if im and "installed_apps" in T.REGISTRY:  # check the real list instead of guessing
        name = (im.group("a") or im.group("b") or "").strip()
        brain.on_tool("installed_apps", {"contains": name})
        return T.dispatch("installed_apps", {"contains": name}, brain.ctx)
    if _DISK_Q.search(low) and "disk_report" in T.REGISTRY:  # real numbers from the drives, never guessed
        brain.on_tool("disk_report", {})
        return T.dispatch("disk_report", {}, brain.ctx)
    if _TOP_PROCS_Q.search(low) and "top_processes" in T.REGISTRY:
        brain.on_tool("top_processes", {})
        return T.dispatch("top_processes", {}, brain.ctx)
    greet = _SMALL_TALK.get(re.sub(r"^(?:hey |ok |okay )?(?:jarvis|friday),?\s*", "", low).strip(" ,"))
    if greet and env("INSTANT_SMALL_TALK", "on").lower() not in ("off", "false", "0", "no"):
        return greet  # "hi" / "thanks" used to take ~8 s through the AI; no AI is needed for these
    if _TIME_Q.match(low) and "get_time_date" in T.REGISTRY:  # no AI needed: instant, free, always right
        brain.on_tool("get_time_date", {})
        return T.dispatch("get_time_date", {}, brain.ctx)
    app = _install_app_name(t)
    if app and "install_app" in T.REGISTRY:  # "install vlc": look it up and install it for real, no AI guessing
        from . import software

        hit = software.best_match(app)
        if hit:
            name, app_id = hit
            brain.on_tool("install_app", {"app_id": app_id})
            brain.on_trace("tool", f"matched '{app}' to {name} ({app_id}) in winget")
            return f"{name}: " + T.dispatch("install_app", {"app_id": app_id}, brain.ctx)
    if _TEXT_INTENT.search(low):
        reply = _sms_setup_help()
        if reply:
            return reply
    m = _OPEN_IN_BROWSER.match(t.strip(" .!?"))
    if m and "browser_open" in T.REGISTRY:
        brain.on_tool("browser_open", {"url": m.group(1)})
        return T.dispatch("browser_open", {"url": m.group(1)}, brain.ctx)
    m = _GO_TO.match(t.strip(" .!?"))
    if m and "open_website" in T.REGISTRY:  # "go to google.com": really open it (the AI used to just SAY it did)
        brain.on_tool("open_website", {"url": m.group(1)})
        return T.dispatch("open_website", {"url": m.group(1)}, brain.ctx)
    m = _READ.match(t.strip(" .!?"))
    if m and "browse_web" in T.REGISTRY:
        brain.on_tool("browse_web", {"url": m.group(1)})
        page = T.dispatch("browse_web", {"url": m.group(1)}, brain.ctx)
        return _short_page(page)
    if _LIST.search(low):
        brain.on_tool("list_github_skills", {})
        parts = [T.dispatch("list_github_skills", {}, brain.ctx)]
        if "list_skills" in T.REGISTRY:
            parts.append(T.dispatch("list_skills", {}, brain.ctx))
        return "\n".join(p for p in parts if p)
    if _STATUS.search(low) and (jobs.recent(1) or re.search(r"skill|pack|install|download", low)):
        return jobs.status_text()
    return None


def _short_page(report: str) -> str:
    """A bare link gets a readable answer without the AI: title, description, tech and the start of the text."""
    if report.startswith(("Error", "Browser control", "BROWSER PAGE")):
        return report[:2500]
    keep = []
    for line in report.splitlines():
        if line.startswith("TEXT:"):
            keep.append("What it says:")
            continue
        if line.startswith(("GitHub links:", "Some links:")):
            if line.startswith("GitHub"):
                keep.append(line)
            continue
        keep.append(line)
    return "\n".join(keep)[:1800]
