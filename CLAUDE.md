# CLAUDE.md — guide for Claude Code

Friday is a Windows voice assistant (Python 3.10+). The user is not a programmer; explain changes simply.

## Layout
- `friday/main.py` – `Assistant` main loop (modes: always / wake / ptt / text), `_listen_cycle()` (always-on listening,
  follow-up window `bus.flags["conversation_until"]`, conversation mode, echo guard, mic auto-retry), `confirm()` for dangerous
  tools (console, voice or dashboard Approve/Deny), `control(action)` used by dashboard/tray/tools, CLI flags.
- `friday/events.py` – `EventBus`: thread-safe status, flags, agent statuses (`AGENTS`), event log, chat feed, SSE pub/sub.
- `friday/dashboard.py` + `friday/static/dashboard.html` – stdlib local web dashboard (127.0.0.1 only, Host check,
  token from `data/dashboard_token.txt`). JSON API under `/api/*`, live updates via `/api/events` (SSE).
  Five tabs: Home (who answers: local -> free -> Abacus -> Claude), Activity, Learning, Shop & credits, Settings
  (settings search, Tools, Health). `/api/limits` also returns `credits` (`credit_summary`: caps, allowance, 14 days).
- `friday/settings_store.py` – `SCHEMA` of editable settings and `EnvStore` (validated `.env` writes, backups in `data/backups`,
  secrets masked). `apply` = live / brain / audio / restart.
- `friday/checks.py` – status checks (internet, api, backup, credits, microphone, stt, tts, system, packages). Abacus credits come from
  `GET https://api.abacus.ai/api/v0/_getOrganizationComputePoints` (header `apiKey`); undocumented, so fail soft.
- `friday/usage.py` – local token/request counter (`data/usage.json`).
- `friday/llm.py` – `Brain.chat()`: OpenAI-compatible tool loop (Abacus RouteLLM default, `https://routellm.abacus.ai/v1`),
  Anthropic tool loop, and a JSON text protocol fallback (`TOOL_MODE=json/auto`). System prompt: `build_system_prompt`.
  Hybrid mode: main brain (often `custom` = local Ollama) + optional `Brain.backup` built from `Settings.backup_settings()`
  (`BACKUP_PROVIDER`). Escalates on phrases (`ESCALATE_PHRASES`), main-AI errors / stuck loop (`ESCALATE_ON_ERROR`), or the
  model replying `ESCALATE` (`SELF_ESCALATE`). Daily cap `BACKUP_MAX_PER_DAY`, counted in `data/backup_usage.json`.
  `Brain.last_route` = "main" or "backup (<reason>)".
  Hand-over order (`Brain._escalate`): free cloud (freellmapi, `free_router`) -> Abacus (`backup`) -> Claude (last).
  `friday/split.py`: general requests go to the free brain first, private ones stay local, reasoning / memory questions
  (`wants_reasoning`) go straight to Abacus. Helper brains have `sub = True` and never hand over onward. A rate-limited
  free tier is skipped for `FREE_COOLDOWN_SECONDS`.
  Brain modes (`brainmode.MODES`): auto / local / online (Claude) / **teach**. Teach (`RouterMixin._teach`): Abacus (`backup`) does every
  request and its steps are recorded (`blackbox.begin_turn`); the local AI only takes a request when `playbooks.match` finds a TESTED
  playbook ("graduated") or it is about passwords/keys (`split.touches_secrets`); a failing local answer escalates to Abacus. In Teach mode
  `_backup_allowed()` ignores BACKUP_MAX_PER_DAY (only the Credit Gate limits it; a declined credit request falls back to the local AI).
  **Abacus has NO limit**: `credits.is_abacus(provider)` tasks are auto-approved (no cap, no question, no BACKUP_MAX_PER_DAY, no allowance);
  their spend goes to `abacus_days` (never the caps) and `CreditGate._count_abacus` calls `gate.on_warn` each time the total passes another
  `ABACUS_WARN_EVERY` (1000) credits. Only Claude / other cloud AIs keep CREDIT_DAILY_CAP / MONTHLY_CAP and Claude always asks.
- `friday/updates.py` – Jarvis tells you things without being asked (called from `main._background_loop`): credit warnings, problems that
  last (local AI down -> it tries to start Ollama; mic lost; brain errors) and when fixed, a check-in every `UPDATES_EVERY_HOURS` when
  something happened, overnight results, tool `give_update`. Log `data/updates.jsonl` (dashboard Home card, phone Status), phone push always,
  spoken only at a good moment (`can_speak_now`: idle, nobody talking, not in a conversation, not in `UPDATES_QUIET_HOURS`).
- `friday/projects.py` – background projects: `start_project` / `list_projects` / `project_status` / `cancel_project` (+ dashboard Home card,
  `/api/projects`, phone Status). Each project = its own worker thread with its own Abacus `ProjectBrain` (own `ProjectMemory`, sees the owner's
  facts), `PROJECTS_MAX_PARALLEL` (3) at once, the rest queue. No PC control (keys/mouse/screen/browser) unless `needs_screen`: then the project
  holds the screen alone and `tools.dispatch` refuses everyone else's control tools (`projects.screen_conflict`). Never allowed in a project:
  `NEVER` (start_project, change_myself, settings, power, brain mode). Things that MUST stay per-thread for this to work: the credit task
  (`CreditGate.ticket` is thread-local), the black-box turn (`blackbox._CUR`), and `ApprovalsMixin.confirm` (one question at a time, locked).
  `projects._brain_class().no_prefetch` stops the web pre-search from using the long brief as a query. Tests: `tests/test_projects.py`.
- `pytest.ini` limits pytest to `tests/`: skill packs Jarvis installs into `skills/_hub` ship their own tests and must never be collected.
- `scripts/keep_friday_running.ps1` + `scripts/install_keepalive.ps1` (`enable_start_on_boot.bat`) – scheduled task "Friday keep-alive": starts
  Friday and Ollama at log on and re-checks every 5 minutes. `-Remove` undoes it (`disable_start_on_boot.bat`).
- `scripts/setup_local_ai.ps1` (`setup_local_ai.bat`) – installs Ollama, picks/pulls a qwen2.5 model, sets
  `OLLAMA_CONTEXT_LENGTH=8192`, writes LLM_PROVIDER=custom + BACKUP_PROVIDER=abacus into `.env`.
- `friday/tools.py` – tools registered with `@tool(name, description, json_schema, dangerous=False)`; each returns a string.
  Never include `"strict"` in schemas (RouteLLM rejects it).
- `friday/toolpick.py` – the local brain only sees CORE + the `GROUPS` whose word list matches the message + `by_description`
  (safety net: tools sharing two uncommon words with the message). Every built-in tool must be in CORE or a group; short
  words in a pattern need `\b` (e.g. `\bphone\b`, or "microphone" loads the SMS tools). A model can't use a tool it wasn't shown.
- `friday/blackbox.py` – full, redacted record of everything the AI does (`data/blackbox/*.jsonl`): each request, every tool with exact
  arguments (key presses, typed text, shell commands), results, front window, answers. Hooked in `Brain._run_tool/_trace/_chat_inner2`.
  `redact()` blanks keys/passwords/cards; `main._save_trace` redacts `trace.jsonl` too. Never records the user's own keystrokes.
- `friday/playbooks.py` + `friday/distill.py` – the "learn from the online AI" loop. `distill.run` (nightly in `routines.run_nightly`, voice
  `review_everything`, dashboard Learning tab) sends the black box to Abacus (`CreditGate.task(..., uncapped=True)`: no Friday-side cap, spend goes
  to `review_days`, not the caps), gets playbooks + rules, then TESTS each playbook on the local model by teacher-forcing the recorded steps
  (`evaluate`, nothing is run); only `proven` ones are `enabled` and shown (`playbooks.prompt_section` in the local prompt, `tools_for` in
  `toolpick.pick`). Playbooks are advice only: permissions stay in `autonomy.py`. Risky tools (send/delete/spend) keep no arguments.
- `friday/verify.py` – night-time check of what was learned, run at the end of `distill.run` (and by voice `verify_learning`). Real use:
  `blackbox.mark_used` records which playbooks / conditional rules were shown per request; the review AI grades those requests;
  `verify.record/judge` switch off a playbook that failed (<50% of >=3 uses), pause a night-learned rule (<40% of >=4), only report
  the owner's own rules (never touched). Re-test: playbooks keep their recorded `cases`; `verify.retest` replays a few each night on the
  local AI (no tool is run) and switches them off (redundant / no help) or back on. Anything run while testing must be wrapped in
  `blackbox.quiet()` or it looks like real use. Acting out learned jobs at night is deliberately NOT done (it would press keys).
- `friday/selfsettings.py` – tools `list_settings` / `change_setting` / `undo_setting_change`: Jarvis fixes his OWN settings (validated by
  `EnvStore`, backed up, applied live via `ctx.apply_settings`, logged in `data/setting_changes.jsonl`). Only `ALLOWED_GROUPS` minus `NEVER`:
  never permissions, credits, provider / URLs, sending, the API or any secret. `selfmod.py` (`change_myself`) can read any non-secret code
  file it asks for (`need_files`); `PROTECTED` also covers approvals, guards, blackbox, distill, phone and selfsettings.
- `friday/voice.py` – recording is debounced (`START_BLOCKS`), the automatic trigger has a floor (`MIN_TRIGGER`, `suggest_trigger`), and
  `BargeDetector` decides when you talk over Jarvis (window rule, not a run of loud blocks).
- `friday/voice.py` – `Recorder` (sounddevice, energy VAD), `SpeechToText` (faster-whisper / Google), `TextToSpeech` (edge-tts + miniaudio, pyttsx3 fallback), `find_wake_word`.
- `friday/email_tools.py` – email skill (stdlib IMAP/SMTP): check_email / search_email / read_email (readonly, BODY.PEEK),
  send_email / reply_email (dangerous). Settings read from env at call time (EMAIL_*), presets in `PROVIDERS`.
- `friday/skills.py` – learned skills. Files in `skills/*.py` (env `SKILLS_DIR`) are plugins using `from friday.tools import tool`.
  `check_code()` = safety rules (no top-level calls, no eval/exec/ctypes/winreg/importlib, no .env access, only
  `friday.tools` import; risky calls require dangerous=True on every tool). `load_all()` at startup; `learn_skill` asks
  `ctx.ask_ai` (Brain.complete -> backup brain if set) to write code, checks, test-loads in a subprocess, asks the user,
  installs. `_pending`, `_backup`, `_disabled` subfolders. Learned tools have `Tool.source == "skill:<name>"`.
- `friday/local_api.py` + `friday/static/phone.html` – phone page over Tailscale (key in `X-API-Key`). Four screens: Talk,
  Approvals, Control, Status. `friday/phone.py` holds what they need: `ask_stream` (`POST /v1/ask/stream`, one JSON object
  per line: delta / step / final), `control` (`POST /v1/control`, only `CONTROL_ACTIONS` and `brain:<mode>`), `status`
  (`GET /v1/status`, never secrets), `open_events` (`GET /v1/events`, stays open: state + approvals). Old pages: `static/_old`.
- `friday/config.py` – all settings come from `.env` (template: `.env.example`).

## Writing a skill by hand (preferred over editing tools.py for new abilities)
Create `skills/<name>.py`: docstring, `from friday.tools import tool`, stdlib imports, constants, and `@tool(...)` functions
`(ctx, ...) -> str`. Follow the rules in `skills.py` (`check_code` must return no problems). Then say "Friday, reload your skills".

## Rules
- Never hardcode or print API keys; never read or echo `.env` contents back to the user.
- Anything destructive (delete, shell, power, wiping data) must use `dangerous=True`.
- Keep spoken replies short and markdown-free.
- New settings: add to `Settings`, `load_settings`, `.env.example` (with a comment) and `settings_store.SCHEMA`.
- Dashboard API must stay localhost-only and token-protected; never return secret values (use `EnvStore.public_view`).
- New Python packages: add to `requirements.txt` (installer installs line by line).

## Test
```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m friday --text      # manual test without a microphone
```
