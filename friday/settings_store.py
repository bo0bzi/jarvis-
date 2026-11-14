"""Read / edit the .env file safely for the dashboard (comments kept, backups made, secrets masked)."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import INPUT_MODES

# apply: live = takes effect immediately, brain = AI client rebuilt, audio = mic/speech restarted,
#        restart = Friday must restart.
SCHEMA: list[dict] = [
    # ---- AI brain
    {"key": "LLM_PROVIDER", "label": "AI provider", "group": "AI brain", "type": "select",
     "options": ["abacus", "anthropic", "openai", "custom"], "default": "abacus", "apply": "brain"},
    {"key": "ABACUS_API_KEY", "label": "Abacus.AI API key", "group": "AI brain", "type": "secret", "apply": "brain",
     "help": "Get it at https://abacus.ai/app/route-llm-apis"},
    {"key": "ABACUS_MODEL", "label": "Abacus model", "group": "AI brain", "type": "text", "default": "route-llm",
     "apply": "brain", "help": "route-llm lets Abacus pick the best model"},
    {"key": "ABACUS_BASE_URL", "label": "Abacus base URL", "group": "AI brain", "type": "text",
     "default": "https://routellm.abacus.ai/v1", "apply": "brain"},
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic (Claude) API key", "group": "AI brain", "type": "secret",
     "apply": "brain", "help": "Billed separately from a Claude Code subscription"},
    {"key": "ANTHROPIC_MODEL", "label": "Claude model", "group": "AI brain", "type": "text",
     "default": "claude-sonnet-4-5", "apply": "brain"},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "group": "AI brain", "type": "secret", "apply": "brain"},
    {"key": "OPENAI_MODEL", "label": "OpenAI model", "group": "AI brain", "type": "text", "default": "gpt-4o-mini",
     "apply": "brain"},
    {"key": "CUSTOM_BASE_URL", "label": "Custom / local AI server URL", "group": "AI brain", "type": "text",
     "default": "http://localhost:11434/v1", "apply": "brain",
     "help": "Ollama: http://localhost:11434/v1 · FreeLLMAPI: http://localhost:3001/v1"},
    {"key": "CUSTOM_API_KEY", "label": "Custom server API key", "group": "AI brain", "type": "secret", "apply": "brain",
     "help": "Usually 'not-needed' for LM Studio / Ollama."},
    {"key": "CUSTOM_MODEL", "label": "Custom / local model", "group": "AI brain", "type": "text",
     "default": "qwen2.5:7b", "apply": "brain", "help": "Ollama: e.g. qwen3.5:9b (newest, fits 8-12 GB GPU), qwen2.5:7b, qwen2.5:14b · FreeLLMAPI: auto"},
    {"key": "TOOL_MODE", "label": "Tool calling mode", "group": "AI brain", "type": "select",
     "options": ["auto", "native", "json"], "default": "auto", "apply": "brain"},
    # ---- Backup brain (only for hard cases -> saves credits)
    {"key": "BACKUP_PROVIDER", "label": "Backup AI (for hard cases only)", "group": "Backup brain", "type": "select",
     "options": ["none", "abacus", "anthropic", "openai", "custom"], "default": "none", "apply": "brain",
     "help": "Uses that provider's key/model from 'AI brain'. Typical: main = custom (local), backup = abacus."},
    {"key": "FREE_ROUTER_KEY", "label": "freellmapi unified key (turns the free cloud tier on)", "group": "Free cloud (freellmapi)",
     "type": "secret", "apply": "brain",
     "help": "freellmapi unified key. Hard questions go here FIRST (free tiers), then to Abacus only if this fails."},
    {"key": "FREE_ROUTER_URL", "label": "OmniRoute address", "group": "Free cloud (freellmapi)", "type": "text",
     "default": "http://localhost:20128/v1", "apply": "brain"},
    {"key": "FREE_ROUTER_MODEL", "label": "OmniRoute model", "group": "Free cloud (freellmapi)", "type": "text",
     "default": "auto/cheap", "apply": "brain",
     "help": "auto/cheap = cheapest first (free providers). Connect ONLY free providers in OmniRoute to guarantee $0."},
    {"key": "FREE_ROUTER_MAX_PER_DAY", "label": "Max free-cloud uses per day", "group": "Free cloud (freellmapi)",
     "type": "number", "default": "300", "min": 0, "max": 5000, "apply": "brain"},
    {"key": "BRAIN_SPLIT", "label": "Brain split: general requests to the free cloud brain, reasoning and memory to Abacus, private ones local",
     "group": "Free cloud (freellmapi)", "type": "bool", "default": "true", "apply": "live"},
    {"key": "BACKUP_MAX_PER_DAY", "label": "Max backup uses per day (not for Abacus: it has no limit)", "group": "Backup brain",
     "type": "number", "default": "25", "min": 0, "max": 1000, "apply": "brain",
     "help": "Only applies when the backup brain is not Abacus. 0 = never."},
    {"key": "ESCALATE_ON_ERROR", "label": "Use backup when the main AI fails or is offline", "group": "Backup brain",
     "type": "bool", "default": "true", "apply": "brain"},
    {"key": "SELF_ESCALATE", "label": "Let the main AI hand off requests it finds too hard", "group": "Backup brain",
     "type": "bool", "default": "true", "apply": "brain"},
    {"key": "ESCALATE_PHRASES", "label": "Phrases that force the backup (comma separated)", "group": "Backup brain",
     "type": "text", "default": "think hard,big brain,use the big brain,ask abacus,use abacus,go deep", "apply": "brain"},
    # ---- Email skill
    {"key": "EMAIL_PROVIDER", "label": "Email provider", "group": "Email", "type": "select",
     "options": ["gmail", "outlook", "yahoo", "icloud", "custom"], "default": "gmail", "apply": "live",
     "help": "gmail also covers Google Workspace business email (e.g. you@yourbusiness.com on Google)."},
    {"key": "EMAIL_ADDRESS", "label": "Email address", "group": "Email", "type": "text", "apply": "live"},
    {"key": "EMAIL_APP_PASSWORD", "label": "App password (not your normal password)", "group": "Email", "type": "secret",
     "apply": "live", "help": "Google: myaccount.google.com/apppasswords (needs 2-Step Verification)."},
    {"key": "EMAIL_IMAP_SERVER", "label": "IMAP server (custom only)", "group": "Email", "type": "text", "apply": "live"},
    {"key": "EMAIL_SMTP_SERVER", "label": "SMTP server (custom only)", "group": "Email", "type": "text", "apply": "live"},
    {"key": "EMAIL_SMTP_PORT", "label": "SMTP port (custom only)", "group": "Email", "type": "number", "default": "465",
     "min": 1, "max": 65535, "apply": "live"},
    # ---- Assistant & listening
    {"key": "ASSISTANT_NAME", "label": "Assistant name", "group": "Assistant", "type": "text", "default": "Friday",
     "apply": "live"},
    {"key": "USER_NAME", "label": "Your name", "group": "Assistant", "type": "text", "apply": "live"},
    {"key": "INPUT_MODE", "label": "Listening mode", "group": "Listening", "type": "select",
     "options": list(INPUT_MODES), "default": "always", "apply": "live",
     "help": "always = always listening + follow-up conversation; wake = wake word every time; ptt = push-to-talk; text = typing"},
    {"key": "WAKE_WORDS", "label": "Wake words (comma separated)", "group": "Listening", "type": "text",
     "default": "hey friday,friday,jarvis,hey jarvis", "apply": "live"},
    {"key": "FOLLOWUP_SECONDS", "label": "Follow-up window (seconds, no wake word needed)", "group": "Listening",
     "type": "number", "default": "20", "min": 0, "max": 600, "apply": "live"},
    {"key": "CONVERSATION_MODE", "label": "Conversation mode (answer everything, no wake word)", "group": "Listening",
     "type": "bool", "default": "false", "apply": "live"},
    {"key": "PTT_HOTKEY", "label": "Push-to-talk hotkey", "group": "Listening", "type": "text",
     "default": "ctrl+alt+f", "apply": "restart"},
    {"key": "MUTE_HOTKEY", "label": "Mute-microphone hotkey", "group": "Listening", "type": "text",
     "default": "ctrl+alt+m", "apply": "restart"},
    {"key": "STOP_HOTKEY", "label": "Stop-speaking hotkey", "group": "Listening", "type": "text",
     "default": "ctrl+alt+s", "apply": "restart"},
    {"key": "BARGE_IN", "label": "Talk over Jarvis to interrupt it (works best with a headset)", "group": "Listening",
     "type": "bool", "default": "true", "apply": "live"},
    {"key": "CLAUDE_TIERS", "label": "Use Claude (Haiku / Sonnet / Opus) when the local AI can't answer (needs the Anthropic key)",
     "group": "AI brain", "type": "bool", "default": "true", "apply": "live"},
    {"key": "CLAUDE_MODEL_HAIKU", "label": "Claude Haiku model (cheap, everyday)", "group": "AI brain", "type": "text",
     "default": "claude-haiku-5-5", "apply": "live"},
    {"key": "CLAUDE_MODEL_SONNET", "label": "Claude Sonnet model (hard questions, code)", "group": "AI brain", "type": "text",
     "default": "claude-sonnet-5-5", "apply": "live"},
    {"key": "CLAUDE_MODEL_OPUS", "label": "Claude Opus model (only when you ask for it by name)", "group": "AI brain",
     "type": "text", "default": "claude-opus-5-5", "apply": "live"},
    {"key": "CLAUDE_MAX_HAIKU_PER_DAY", "label": "Max Haiku answers per day", "group": "AI brain", "type": "number",
     "default": "40", "min": 0, "max": 1000, "apply": "live"},
    {"key": "CLAUDE_MAX_SONNET_PER_DAY", "label": "Max Sonnet answers per day", "group": "AI brain", "type": "number",
     "default": "12", "min": 0, "max": 500, "apply": "live"},
    {"key": "CLAUDE_MAX_OPUS_PER_DAY", "label": "Max Opus answers per day", "group": "AI brain", "type": "number",
     "default": "3", "min": 0, "max": 100, "apply": "live"},
    {"key": "CLAUDE_MAX_CALLS_PER_MONTH", "label": "Hard stop: max Claude answers per month (all tiers)", "group": "AI brain",
     "type": "number", "default": "600", "min": 0, "max": 100000, "apply": "live"},
    {"key": "CLAUDE_AUTO_OPUS", "label": "Let Jarvis climb to Opus by itself when Sonnet fails (off = only when you ask)",
     "group": "AI brain", "type": "bool", "default": "false", "apply": "live"},
    {"key": "CALENDAR_ICS_URL", "label": "Your calendar's private ICS address (Google: Integrate calendar > Secret address "
     "in iCal format). Read-only.", "group": "Listening", "type": "secret", "apply": "live"},
    {"key": "STREAM_VOICE", "label": "Start talking while the AI is still writing its answer (faster replies)",
     "group": "Listening", "type": "bool", "default": "true", "apply": "live"},
    {"key": "BARGE_IN_LEVEL", "label": "How loud you must be to interrupt (raise it if its own voice cuts itself off)",
     "group": "Listening", "type": "number", "default": "0.06", "min": 0.02, "max": 0.3, "apply": "live"},
    # ---- Microphone & speech
    {"key": "MIC_DEVICE", "label": "Microphone", "group": "Microphone & speech", "type": "mic", "apply": "audio"},
    {"key": "ENERGY_THRESHOLD", "label": "Voice sensitivity threshold (0 = automatic)", "group": "Microphone & speech",
     "type": "number", "default": "0", "min": 0, "max": 1, "step": 0.001, "apply": "live"},
    {"key": "SILENCE_SECONDS", "label": "Silence that ends a sentence (s)", "group": "Microphone & speech",
     "type": "number", "default": "1.0", "min": 0.3, "max": 5, "step": 0.1, "apply": "live"},
    {"key": "MAX_RECORD_SECONDS", "label": "Max sentence length (s)", "group": "Microphone & speech",
     "type": "number", "default": "15", "min": 3, "max": 120, "apply": "live"},
    {"key": "STT_ENGINE", "label": "Speech recognition", "group": "Microphone & speech", "type": "select",
     "options": ["whisper", "google"], "default": "whisper", "apply": "audio"},
    {"key": "WHISPER_MODEL", "label": "Whisper model", "group": "Microphone & speech", "type": "select",
     "options": ["tiny.en", "base.en", "small.en", "medium.en", "large-v3", "distil-large-v3"], "default": "base.en",
     "apply": "audio", "help": "With an NVIDIA GPU, small.en or medium.en is fast and much more accurate."},
    {"key": "WHISPER_DEVICE", "label": "Speech recognition runs on", "group": "Microphone & speech", "type": "select",
     "options": ["auto", "cuda", "cpu"], "default": "auto", "apply": "audio",
     "help": "auto = graphics card if it works, otherwise the processor."},
    # ---- Voice
    {"key": "TTS_ENGINE", "label": "Voice engine", "group": "Voice", "type": "select",
     "options": ["edge", "pyttsx3", "none"], "default": "edge", "apply": "live"},
    {"key": "EDGE_VOICE", "label": "Voice", "group": "Voice", "type": "select", "default": "en-GB-RyanNeural",
     "options": ["en-GB-RyanNeural", "en-GB-ThomasNeural", "en-GB-SoniaNeural", "en-GB-LibbyNeural",
                 "en-IE-EmilyNeural", "en-IE-ConnorNeural", "en-US-GuyNeural", "en-US-AriaNeural",
                 "en-US-JennyNeural", "en-AU-WilliamNeural", "en-CA-LiamNeural", "en-CA-ClaraNeural"],
     "apply": "live", "allow_custom": True},
    {"key": "EDGE_RATE", "label": "Voice speed (e.g. +5%, -10%)", "group": "Voice", "type": "text", "default": "+5%",
     "apply": "live"},
    {"key": "PYTTSX3_RATE", "label": "Offline voice speed", "group": "Voice", "type": "number", "default": "185",
     "min": 80, "max": 400, "apply": "live"},
    # ---- Misc
    {"key": "DEFAULT_CITY", "label": "Default city (weather)", "group": "General", "type": "text",
     "default": "Toronto", "apply": "live"},
    {"key": "MEMORY_MAX_MESSAGES", "label": "Messages kept in memory", "group": "General", "type": "number",
     "default": "40", "min": 2, "max": 500, "apply": "restart"},
    {"key": "ALLOW_SHELL", "label": "Allow shell commands (always asks first)", "group": "General", "type": "bool",
     "default": "true", "apply": "live"},
    {"key": "TRAY_ICON", "label": "System-tray icon", "group": "General", "type": "bool", "default": "true",
     "apply": "restart"},
    {"key": "DASHBOARD_OPEN_BROWSER", "label": "Open dashboard when Friday starts", "group": "Dashboard",
     "type": "bool", "default": "true", "apply": "live"},
    {"key": "DASHBOARD_PORT", "label": "Dashboard port", "group": "Dashboard", "type": "number", "default": "8765",
     "min": 1025, "max": 65535, "apply": "restart"},
    {"key": "AUTO_CHECK_MINUTES", "label": "Automatic status checks every (minutes, 0 = off)", "group": "Dashboard",
     "type": "number", "default": "10", "min": 0, "max": 1440, "apply": "live"},
    # ---- v2.0: Credits (Abacus / cloud AI spending)
    {"key": "ABACUS_WARN_EVERY", "label": "Warn me each time Abacus use passes another N credits (0 = never)", "group": "Credits",
     "type": "number", "default": "1000", "min": 0, "max": 1000000, "apply": "live",
     "help": "Abacus has NO limit in Friday: no cap, no question. Set a limit on abacus.ai itself if you want one."},
    {"key": "CREDIT_DAILY_CAP", "label": "Max credits per day for Claude and other capped cloud AIs (not Abacus)", "group": "Credits",
     "type": "number", "default": "200", "min": 0, "max": 100000, "apply": "live",
     "help": "Friday never goes over this for Claude. Every Claude job still asks you first with an estimate."},
    {"key": "CREDIT_MONTHLY_CAP", "label": "Max credits per month for Claude and other capped cloud AIs (not Abacus)", "group": "Credits", "type": "number",
     "default": "2000", "min": 0, "max": 1000000, "apply": "live"},
    {"key": "CREDIT_PREAPPROVED_PER_DAY", "label": "Credits Friday may use per day without asking (0 = always ask)",
     "group": "Credits", "type": "number", "default": "0", "min": 0, "max": 100000, "apply": "live",
     "help": "Only for capped cloud AIs other than Claude (which always asks). Abacus never asks and has no limit."},
    {"key": "APPROVAL_WAIT_SECONDS", "label": "How long Friday waits for your yes/no (s)", "group": "Credits",
     "type": "number", "default": "180", "min": 10, "max": 3600, "apply": "live"},
    # ---- v2.0: SMS (Quo)
    {"key": "SMS_ENABLED", "label": "Text Friday by SMS", "group": "SMS (Quo)", "type": "bool", "default": "false",
     "apply": "restart"},
    {"key": "QUO_API_KEY", "label": "Quo API key (for Friday)", "group": "SMS (Quo)", "type": "secret",
     "apply": "restart", "help": "Quo app > Settings > API. Make a key just for Friday."},
    {"key": "FRIDAY_SMS_NUMBER", "label": "Quo number Friday texts from (+1...)", "group": "SMS (Quo)", "type": "text",
     "default": "", "apply": "restart", "pattern": r"^(\+?[\d\-\s().]{10,20})?$"},
    {"key": "OWNER_PHONE", "label": "Your cell number (+1...) - the only number Friday obeys", "group": "SMS (Quo)",
     "type": "text", "default": "", "apply": "restart", "pattern": r"^(\+?[\d\-\s().]{10,20})?$"},
    {"key": "SMS_PIN", "label": "Approval PIN (reply e.g. YES 4821)", "group": "SMS (Quo)", "type": "secret",
     "apply": "live"},
    {"key": "SMS_POLL_SECONDS", "label": "Check for new texts every (s)", "group": "SMS (Quo)", "type": "number",
     "default": "20", "min": 10, "max": 600, "apply": "restart"},
    # ---- v2.0: Autonomy
    {"key": "AUTONOMY_LEVEL", "label": "How much Friday does on her own", "group": "Autonomy", "type": "select",
     "options": ["0", "1", "2"], "default": "1", "apply": "live",
     "help": "0 = asks before every action. 1 = asks for risky ones. 2 = only asks for system changes. "
             "Sending, deleting and spending ALWAYS ask."},
    {"key": "NEVER_TOOLS", "label": "Tools Friday must never use (comma separated)", "group": "Autonomy",
     "type": "text", "default": "", "apply": "live"},
    {"key": "AGENT_MAX_STEPS", "label": "Max steps per task", "group": "Autonomy", "type": "number", "default": "12",
     "min": 2, "max": 40, "apply": "live"},
    # ---- v2.0: Local AI (free, runs on your graphics card)
    {"key": "LOCAL_CODER_MODEL", "label": "Local coding model (Ollama)", "group": "Local AI", "type": "select",
     "options": ["qwen2.5-coder:14b", "qwen2.5-coder:7b", ""], "default": "", "allow_custom": True, "apply": "live"},
    {"key": "VISION_MODEL", "label": "Local photo model (Ollama)", "group": "Local AI", "type": "select",
     "options": ["qwen2.5vl:7b", "qwen3.5:9b", "llava:7b"], "default": "qwen2.5vl:7b", "allow_custom": True, "apply": "live"},
    {"key": "EMBED_MODEL", "label": "Local search model for manuals (Ollama)", "group": "Local AI", "type": "select",
     "options": ["nomic-embed-text", "mxbai-embed-large"], "default": "nomic-embed-text", "allow_custom": True,
     "apply": "live"},
    {"key": "GITHUB_TOKEN", "label": "GitHub token (optional, for finding skills)", "group": "Local AI",
     "type": "secret", "apply": "live", "help": "Read-only, no scopes needed. Only raises GitHub's search limit."},
    # ---- v2.0: Shop
    {"key": "SHOP_NAME", "label": "Shop name", "group": "Shop", "type": "text", "default": "Integrity Powersports",
     "apply": "live"},
    {"key": "LABOUR_RATE", "label": "Labour rate ($/hour, for quotes)", "group": "Shop", "type": "number",
     "default": "0", "min": 0, "max": 1000, "apply": "live"},
    {"key": "TAX_RATE", "label": "Tax rate (%)", "group": "Shop", "type": "number", "default": "13", "min": 0,
     "max": 30, "apply": "live"},
    {"key": "SHOP_READY_TEXT", "label": "'Bike ready' text", "group": "Shop", "type": "text",
     "default": "Hi {first}, your {vehicle} is ready for pickup at {shop}. {extra}", "apply": "live",
     "help": "{first} = customer first name, {vehicle}, {shop}, {extra} = anything you add."},
    # ---- v2.0: Routines
    {"key": "BRIEFING_ENABLED", "label": "Morning briefing", "group": "Routines", "type": "bool", "default": "true",
     "apply": "live"},
    {"key": "BRIEFING_TIME", "label": "Briefing time (HH:MM)", "group": "Routines", "type": "text",
     "default": "07:30", "apply": "live", "pattern": r"^([01]?\d|2[0-3]):[0-5]\d$"},
    {"key": "BRIEFING_SMS", "label": "Also text me the briefing", "group": "Routines", "type": "bool",
     "default": "true", "apply": "live"},
    {"key": "SMART_ENDPOINT", "label": "Answer sooner: stop listening at a short pause after a finished sentence",
     "group": "Microphone & speech", "type": "bool", "default": "true", "apply": "audio"},
    {"key": "DIGEST_ENABLED", "label": "Evening digest of what Jarvis did today", "group": "Routines", "type": "bool",
     "default": "true", "apply": "live"},
    {"key": "DIGEST_TIME", "label": "Digest time (24h, like 20:00)", "group": "Routines", "type": "text",
     "default": "20:00", "apply": "live"},
    {"key": "DIGEST_SMS", "label": "Also text me the digest (default: phone notification only)", "group": "Routines",
     "type": "bool", "default": "false", "apply": "live"},
    {"key": "NIGHTLY_ENABLED", "label": "Nightly self-check and repair (local AI only)", "group": "Routines",
     "type": "bool", "default": "true", "apply": "live"},
    {"key": "EVOLVE_ENABLED", "label": "Learn from its mistakes at night (writes itself new rules, free)", "group": "Routines",
     "type": "bool", "default": "true", "apply": "live",
     "help": "Part of the nightly run. Also: say 'evolve', or press the button on the Thinking tab."},
    {"key": "PROJECTS_MAX_PARALLEL", "label": "Background projects that run at the same time", "group": "Routines", "type": "number",
     "default": "3", "min": 1, "max": 10, "apply": "live",
     "help": "More wait in line. Each project is its own Abacus conversation, so each uses credits (Abacus has no limit)."},
    {"key": "UPDATES_ENABLED", "label": "Tell me things without being asked (problems, credit warnings, check-ins)", "group": "Routines",
     "type": "bool", "default": "true", "apply": "live",
     "help": "Always sent to the phone page; spoken out loud only when it is a good moment (not while you talk, not in quiet hours)."},
    {"key": "UPDATES_SPEAK", "label": "Say updates out loud (not just on the phone)", "group": "Routines", "type": "bool",
     "default": "true", "apply": "live"},
    {"key": "UPDATES_QUIET_HOURS", "label": "Quiet hours: nothing is spoken (HH:MM-HH:MM)", "group": "Routines", "type": "text",
     "default": "22:00-07:00", "apply": "live", "pattern": r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"},
    {"key": "UPDATES_EVERY_HOURS", "label": "Check in every N hours when something happened (0 = never)", "group": "Routines",
     "type": "number", "default": "4", "min": 0, "max": 48, "apply": "live"},
    {"key": "DISTILL_ENABLED", "label": "Full review at night: the online AI studies everything Jarvis did and teaches the local AI (uses Abacus credits)",
     "group": "Routines", "type": "bool", "default": "true", "apply": "live",
     "help": "Friday puts NO credit cap on this: set the limit on Abacus itself. Also: say 'review everything'. Passwords and keys are blanked before anything is saved or sent."},
    {"key": "DISTILL_MAX_PLAYBOOKS", "label": "Playbooks to keep", "group": "Routines", "type": "number", "default": "60",
     "min": 5, "max": 500, "apply": "live"},
    {"key": "BLACKBOX_ENABLED", "label": "Keep a full record of what Jarvis does (needed for the full review)", "group": "Routines",
     "type": "bool", "default": "true", "apply": "live",
     "help": "Only what the AI itself does (every tool, key press and command it runs). Your own keystrokes are never recorded."},
    {"key": "BLACKBOX_KEEP_DAYS", "label": "Days to keep that record", "group": "Routines", "type": "number", "default": "60",
     "min": 1, "max": 365, "apply": "live"},
    {"key": "NIGHTLY_TIME", "label": "Nightly time (HH:MM)", "group": "Routines", "type": "text", "default": "02:00",
     "apply": "live", "pattern": r"^([01]?\d|2[0-3]):[0-5]\d$"},
    # ---- v2.0: Remote access / API
    {"key": "FRIDAY_API_KEY", "label": "Local API key (16+ characters)", "group": "API", "type": "secret",
     "apply": "restart", "help": "Lets your phone or other apps talk to Friday. Leave empty to turn the API off."},
    {"key": "REMOTE_ACCESS", "label": "Allow other devices (use with Tailscale only)", "group": "API",
     "type": "bool", "default": "false", "apply": "restart"},
    {"key": "API_PORT", "label": "API port", "group": "API", "type": "number", "default": "8766", "min": 1025,
     "max": 65535, "apply": "restart"},
    # ---- v2.0: Backup
    {"key": "BACKUP_KEEP", "label": "Backups to keep", "group": "Backup", "type": "number", "default": "14",
     "min": 1, "max": 365, "apply": "live"},
    {"key": "BACKUP_INCLUDE_ENV", "label": "Include settings & keys (.env) in backups", "group": "Backup",
     "type": "bool", "default": "false", "apply": "live"},
]
SCHEMA_BY_KEY = {f["key"]: f for f in SCHEMA}
EXTRA_KEYS = {"DISABLED_TOOLS"}  # editable through other dashboard panels
_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _unescape(s: str) -> str:
    """Undo _quote's escaping: \\" -> " and \\\\ -> \\ (a single pass, so nothing is unescaped twice)."""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in ('"', "\\"):
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _unquote(v: str) -> str:
    v = v.strip()
    if v[:1] in ('"', "'"):
        q, i = v[0], 1
        while i < len(v):
            if v[i] == "\\" and i + 1 < len(v):
                i += 2  # skip the escaped character
                continue
            if v[i] == q:
                return _unescape(v[1:i])
            i += 1
        return _unescape(v[1:])  # unterminated quote: take the rest
    if " #" in v:  # inline comment
        v = v.split(" #", 1)[0].rstrip()
    return v


def _quote(v: str) -> str:
    if v == "" or re.fullmatch(r"[A-Za-z0-9_\-+.,:/%@ ]*", v) and v == v.strip():
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mask(value: str) -> str:
    if not value:
        return ""
    return "••••••" + value[-4:] if len(value) > 8 else "••••"


class EnvStore:
    def __init__(self, env_path: Path | None = None, backup_dir: Path | None = None):
        from . import config as _cfg

        self.path = Path(env_path or _cfg.ENV_PATH)
        self.backup_dir = Path(backup_dir or self.path.parent / "data" / "backups")

    def read(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                m = _LINE_RE.match(line)
                if m and not line.lstrip().startswith("#"):
                    values[m.group(1)] = _unquote(m.group(2))
        return values

    def public_view(self) -> list[dict]:
        """Schema + current values for the dashboard. Secrets are never sent, only a mask."""
        from .config import is_placeholder

        vals = self.read()
        out = []
        for f in SCHEMA:
            item = {k: v for k, v in f.items()}
            v = vals.get(f["key"], f.get("default", ""))
            if f["type"] == "secret":
                real = vals.get(f["key"], "")
                item["has_value"] = bool(real) and not is_placeholder(real)
                item["value"] = mask(real) if item["has_value"] else ""
            else:
                item["value"] = v
            out.append(item)
        return out

    def validate(self, updates: dict[str, str]) -> dict[str, str]:
        """Return {key: error} for bad values."""
        errors = {}
        for k, v in updates.items():
            f = SCHEMA_BY_KEY.get(k)
            if f is None and k not in EXTRA_KEYS:
                errors[k] = "Unknown setting."
                continue
            v = "" if v is None else str(v)
            if "\n" in v or "\r" in v:
                errors[k] = "Must be a single line."
                continue
            if f is None:
                continue
            t = f["type"]
            if t == "number":
                try:
                    n = float(v)
                except ValueError:
                    errors[k] = "Must be a number."
                    continue
                if ("min" in f and n < f["min"]) or ("max" in f and n > f["max"]):
                    errors[k] = f"Must be between {f.get('min')} and {f.get('max')}."
            elif t == "bool" and v.lower() not in ("true", "false"):
                errors[k] = "Must be true or false."
            elif t == "select" and not f.get("allow_custom") and v not in f["options"]:
                errors[k] = f"Must be one of: {', '.join(f['options'])}."
            elif f.get("pattern") and not re.match(f["pattern"], v):
                errors[k] = "That doesn't look right (times are HH:MM like 07:30, phone numbers like +19055551234)."
            elif k == "FRIDAY_API_KEY" and v and len(v) < 16:
                errors[k] = "Use at least 16 characters."
        return errors

    def backup(self) -> Path | None:
        if not self.path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"env-{datetime.now():%Y%m%d-%H%M%S-%f}.bak"
        shutil.copy2(self.path, dest)
        for old in sorted(self.backup_dir.glob("env-*.bak"))[:-20]:  # keep the last 20
            try:
                old.unlink()
            except OSError:
                pass
        return dest

    def write(self, updates: dict[str, str]) -> dict:
        """Apply updates in place (keeps comments/order). Returns {"changed": [...], "backup": path, "apply": set}."""
        updates = {k: ("" if v is None else str(v).strip()) for k, v in updates.items()}
        errors = self.validate(updates)
        if errors:
            raise ValueError(errors)
        current = self.read()
        changed = {k: v for k, v in updates.items() if current.get(k) != v}
        if not changed:
            return {"changed": [], "backup": None, "apply": []}
        backup = self.backup()
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        seen = set()
        for i, line in enumerate(lines):
            m = _LINE_RE.match(line)
            if m and not line.lstrip().startswith("#") and m.group(1) in changed:
                key = m.group(1)
                lines[i] = f"{key}={_quote(changed[key])}"
                seen.add(key)
        missing = [k for k in changed if k not in seen]
        if missing:
            lines += ["", "# ---- added by the Friday dashboard ----"] + [f"{k}={_quote(changed[k])}" for k in missing]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        applies = sorted({SCHEMA_BY_KEY[k]["apply"] if k in SCHEMA_BY_KEY else "live" for k in changed})
        return {"changed": sorted(changed), "backup": str(backup) if backup else None, "apply": applies}
