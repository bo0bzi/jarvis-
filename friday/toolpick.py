"""Tool picker for the LOCAL brain. A 9B model given all 65+ tools (about 5,000 tokens of tool text) gets
confused and runs out of room. So each turn it only sees a small core set, the groups that match the
message, and the tools it used in the last two turns. It can call load_tools to get any other group.
Cloud brains still see every tool. TOOL_PICKER=off in .env turns this off."""
from __future__ import annotations

import re

from . import tools as T
from .env import env

CORE = ["get_time_date", "web_search", "browse_web", "remember_fact", "search_memory", "what_did_you_do", "job_status",
        "load_tools"]

GROUPS: dict[str, tuple[str, list[str]]] = {
    "skills": (r"skill|pack|github|plugin|mcp|install|instal|repo|add-?on|learn|ability|abilities",
               ["find_skills", "inspect_skill", "install_github_skill", "list_github_skills", "remove_github_skill",
                "use_skill", "search_installed_skills", "list_skills", "learn_skill"]),
    "email": (r"e-?mail|inbox|gmail|outlook|reply", ["check_email", "read_email", "search_email", "send_email",
                                                     "reply_email"]),
    "texts": (r"\btext|\bsms\b|\bquo\b|message|\bphone\b|ready for pick ?up|bike (is )?ready",
              ["send_text_message", "text_me", "text_customer_bike_ready", "read_texts"]),
    "shop": (r"job|quote|estimate|\bparts?\b|customer|bike|sled|atv|utv|quad|seadoo|sea-doo|pwc|repair|service|invoice|"
             r"work ?order|shop", ["add_job", "update_job", "list_jobs", "draft_quote", "add_parts_reminder",
                                   "list_parts_reminders", "text_customer_bike_ready"]),
    "pc": (r"open|close|launch|\bapps?\b|program|volume|screenshot|screen|file|folder|directory|downloads|documents|desktop|"
           r"delete|shell|command|powershell|\bcmd\b|shut ?down|restart|reboot|sleep|\block\b|clipboard|copy|paste|\bcpu\b|"
           r"\bram\b|disk|drive|system|\bpc\b|computer|machine|hardware|motherboard|\bgpu\b|graphics card|processor|specs?\b",
           ["open_app", "close_app", "set_volume", "take_screenshot", "list_folder", "search_files", "open_file", "delete_file",
            "run_shell_command", "power_action", "clipboard", "system_info", "open_dashboard", "set_listening"]),
    "reminders": (r"remind|timer|alarm|note|later|tomorrow|minutes|hours", ["set_reminder", "list_reminders",
                                                                             "set_timer", "add_note", "read_notes",
                                                                             "clear_notes"]),
    "knowledge": (r"manual|knowledge|document|pdf|spec|torque|wiring|diagram|look up|lookup",
                  ["search_manuals", "add_to_knowledge", "update_knowledge_base"]),
    "memory": (r"remember|forget|memory|know about me|told you", ["remember_fact", "forget_fact", "search_memory", "list_facts"]),
    "maintenance": (r"backup|restore|nightly|report|fix|improve|credit|usage|briefing|morning|reload|disable|broken|digest|"
                    r"what did you do|what have you done|status check|system check|give me an update|catch me up|any news|what.s going on",
                    ["backup_now", "list_backups", "restore_backup", "nightly_report", "run_nightly_now",
                     "install_nightly_fixes", "credit_status", "morning_briefing", "reload_skills", "disable_skill",
                     "restore_skill", "daily_digest", "system_info", "give_update"]),
    "software": (r"(?s)^(?!.*\b(skill|skills|pack|github|plugin|mcp|repo)\b).*\b(download|install|instal|set ?up|software|"
                 r"program|winget|cpu-?z|installer|\.exe)\b",
                 ["winget_search", "install_app", "download_file", "open_file", "run_shell_command"]),
    "lessons": (r"teach|lesson|from now on|always |never |rule|way of thinking|how i want you|learn (that|this|how)|"
                r"evolve|self[- ]improv|learn from|what did you learn|learned|playbook|review everything|full review|"
                r"online ai|study what|check what you learned|verify what|is it working",
                ["teach_way_to_think", "list_ways_of_thinking", "forget_way_of_thinking", "evolve_now",
                 "what_did_you_learn", "review_lessons", "review_everything", "list_playbooks", "forget_playbook", "verify_learning"]),
    "livefacts": (r"anydesk|rustdesk|remote (desktop|access|id)|\bip\b|wi-?fi|network|drive|disk|storage|space|"
                  r"slow|memory usage|using (the )?(most )?(ram|memory|cpu)|installed|do i have .* (installed|on)|"
                  r"motherboard|hardware|\bgpu\b|graphics card|\bbios\b|specs?\b|performance",
                  ["remote_desktop_id", "disk_report", "top_processes", "network_info", "installed_apps", "system_info"]),
    "brainmode": (r"go (?:online|local|offline)|online mode|local mode|offline mode|use claude|claude|opus|sonnet|haiku|"
                  r"which (?:model|brain)|what (?:model|mode)|switch (?:to|your)|local model|models? (?:do you|are)|teach(?:ing)? mode|"
                  r"apprentice|you do the work",
                  ["set_brain_mode", "brain_status", "list_local_models", "switch_local_model"]),
    "selfmod": (r"yourself|your (?:own )?(?:code|programming)|reprogram|undo (?:your|the) last (?:change|update)|revert",
                ["change_myself", "undo_last_change", "list_self_changes", "learn_skill"]),
    "calendar": (r"calendar|schedule|agenda|appointment|meeting|booked|book (?:a|an|him|her|them)|am i free|what'?s on|"
                 r"my day|add .{0,40}\b(?:to|on) (?:the |my )?(?:calendar|schedule)|cancel .{0,40}(?:appointment|event)",
                 ["add_event", "list_events", "cancel_event"]),
    "weather": (r"weather|rain|snow|temperature|forecast|cold|hot|wind", ["get_weather"]),
    "projects": (r"project|in the background|background (?:task|job)|at the same time|in parallel|multitask|while you (?:work|do)|"
                 r"what are you (?:working|doing)|cancel (?:that|the|this)",
                 ["start_project", "list_projects", "project_status", "cancel_project"]),
    "settings": (r"setting|threshold|sensitiv|silence|noise|noisy|interrupt|barge|cuts? (?:me|you) off|cutting (?:me|you) off|"
                 r"let me finish|finish (?:talking|speaking|my)|(?:speak|talk)s? (?:faster|slower)|too (?:fast|slow|quiet|loud)|"
                 r"\bmic(?:rophone)?\b|undo that setting|wake word|fix (?:it|that|yourself)",
                 ["list_settings", "change_setting", "undo_setting_change"]),
    "web": (r"youtube|video|song|music|play|website|site|browser|google|search|news|price|https?://|www\.|\.com|"
            r"\.ca\b|url|link|page|browse|internet|online|look up|lookup",
            ["play_youtube", "open_website", "web_search", "browse_web", "browser_open", "browser_read"]),
    "computer": (r"browser|click|\btyp(?:e|ing)\b|\bfill|\bforms?\b|log ?in|sign ?in|screen|mouse|keyboard|\bpress|control|"
                 r"use (my|the) (computer|pc)|scroll|button|look at|minimi[sz]e|maximi[sz]e|\bwindows?\b|desktop|shortcut|"
                 r"hot ?key|\bkeys?\b|alt\+|ctrl\+|win\+|switch to|bring up|full ?screen",
                 ["browser_open", "browser_read", "browser_scroll", "browser_back", "browser_click", "browser_type",
                  "browser_look", "browser_close", "look_at_screen", "read_screen_text", "mouse_click", "keyboard_type", "press_keys",
                  "allow_computer_control", "stop_computer_control"]),
}


# The bundled official MCP servers only appear when the message calls for them (keeps every prompt short).
BUNDLED_MCP = {"mcp:fetch", "mcp:time", "mcp:thinking"}
GROUPS["worldtime"] = (r"time ?zones?|what time is it in|time in [a-z]|convert .{0,30}\btime\b|\b(?:est|pst|utc|gmt)\b",
                       ["time__get_current_time", "time__convert_time", "time__get_time_ago"])
GROUPS["deepthink"] = (r"think (?:it |this )?(?:through|carefully|step by step)|step by step|work (?:it )?out|figure out|"
                       r"plan (?:out|for)|puzzle|trade-?offs?|pros and cons|why (?:is|does|did) .{10,}",
                       ["thinking__sequentialthinking"])
GROUPS["readpage"] = (r"fetch (?:this|that|the)|read (?:this|that|the) (?:page|article|link|site)|summari[sz]e (?:this|that|the) "
                      r"(?:page|article|link|site)", ["fetch__fetch"])


def enabled(brain) -> bool:
    return not getattr(brain, "cloud", True) and env("TOOL_PICKER", "on").lower() not in ("off", "false", "0", "no")


def groups_for(text: str) -> list[str]:
    low = (text or "").lower()
    return [g for g, (pattern, _) in GROUPS.items() if re.search(pattern, low)]


_STOP = {"that", "this", "with", "from", "have", "what", "when", "your", "user", "users", "about", "which", "they", "them",
         "will", "would", "should", "could", "there", "their", "then", "than", "into", "only", "also", "does", "need",
         "want", "please", "jarvis", "friday", "tell", "give", "show", "make", "some", "more", "most", "just", "like"}


def _stems(text: str) -> set[str]:
    """Words of 4+ letters with plural / -ing / -ed endings cut off, so 'folders' meets 'folder'."""
    out = set()
    for w in re.findall(r"[a-z]{4,}", (text or "").lower().replace("_", " ")):
        if w in _STOP:
            continue
        for end in ("ing", "ed", "es", "s"):
            if w.endswith(end) and len(w) - len(end) >= 4:
                w = w[:-len(end)]
                break
        out.add(w)
    return out


def by_description(text: str, limit: int = 3) -> list[str]:
    """Safety net for requests no word list expected: built-in tools whose own name and description share at least
    two uncommon words with the message. (A word used by many tools, like 'list', says nothing about which one.)"""
    want = _stems(text)
    if len(want) < 2:
        return []
    tools = [t for t in T.active_tools() if not t.source.startswith(("skill:", "mcp:"))]
    words = {t.name: _stems(f"{t.name} {t.description}") for t in tools}
    common: dict[str, int] = {}
    for ws in words.values():
        for w in ws:
            common[w] = common.get(w, 0) + 1
    scored = sorted(((len({w for w in want & ws if common[w] <= 6}), n) for n, ws in words.items()), reverse=True)
    return [n for score, n in scored[:limit] if score >= 2]


def pick(text: str, recent: list[str] | None = None, extra_groups: set[str] | None = None) -> list[str]:
    """Tool names for this turn (only tools that exist and are switched on)."""
    names = list(CORE)
    for g in [*groups_for(text), *sorted(extra_groups or ())]:
        names += GROUPS.get(g, ("", []))[1]
    names += by_description(text)
    try:
        from . import playbooks

        names += playbooks.tools_for(text)  # the tools a matching playbook uses must be on the list too
    except Exception:
        pass
    names += list(recent or [])
    low = (text or "").lower()
    extras = [t for t in T.active_tools() if t.source.startswith(("skill:", "mcp:")) and t.source not in BUNDLED_MCP]
    for t in extras:  # learned skills and MCP tools: all of them if few, else the ones the message mentions
        words = set(re.findall(r"[a-z0-9]+", f"{t.name} {t.source}".lower().replace("_", " "))) - {"skill", "mcp"}
        if len(extras) <= 8 or any(len(w) > 2 and w in low for w in words):
            names.append(t.name)
    active = {t.name for t in T.active_tools()}
    seen: list[str] = []
    for n in names:
        if n in active and n not in seen:
            seen.append(n)
    return seen


@T.tool("load_tools", "Get more tools for this turn when the ones you have can't do the job. Groups: "
        + ", ".join(GROUPS) + ".",
        {"type": "object", "properties": {"group": {"type": "string", "description": "one group name"}},
         "required": ["group"]})
def load_tools(ctx, group: str):
    g = group.strip().lower()
    if g not in GROUPS:
        return f"Unknown group '{group}'. Groups: {', '.join(GROUPS)}."
    brain = getattr(ctx, "brain", None)
    if brain is not None:
        brain._extra_groups.add(g)
    names = [n for n in GROUPS[g][1] if n in T.REGISTRY]
    return f"Loaded the {g} tools: {', '.join(names)}. You can call them now."
