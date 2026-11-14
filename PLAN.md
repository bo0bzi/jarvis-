# Jarvis improvement plan

Note: live web research was unavailable when this was written, so this plan comes from well-known practice for
local voice assistants (tiered models, tool grounding, memory, streaming speech), not from fresh sources.

## Done
1. Tiered brains: local Ollama -> free OmniRoute -> Abacus with credit gate.
2. Anti-hallucination guards, web prefetch, hard-routed weather/time/install.
3. Teachable lessons and nightly self-learning (evolve).
4. Barge-in (talk over it) on PC and phone.
5. Speech starts sooner: replies are spoken sentence by sentence, the next one is made while the current one plays.

## Next (in order of value)
1. Reconnect Gemini (agy) in OmniRoute so the free tier answers hard questions.
2. Long-term semantic memory: store facts and past answers with embeddings (Ollama `nomic-embed-text`), fetch the
   few most relevant ones per message instead of dumping everything in the prompt.
3. Streaming model output into speech (start talking before the full answer is written).
4. Read-only live-fact tools (AnyDesk ID, disk/CPU status, installed apps) so it stops guessing.
5. Self-check pass for factual answers without tools (verify or say "I'm not sure").
6. Review auto-learned lessons weekly from the dashboard; prune bad ones.
7. Proactive helpers: morning brief, reminders about open jobs, failed-install retries.
8. Vision: screenshot understanding with a local vision model when asked "what's on my screen".

---

# Gameplan: what people add to Jarvis to make it smarter (researched 2026-11-12)

Sources actually read: the official MCP reference-servers page (github.com/modelcontextprotocol/servers) and GitHub's
"jarvis-assistant" topic sorted by stars (157 repos). Web search itself was down, so this is not exhaustive.

## What the popular projects have in common
- **MCP servers** are the standard way to add abilities. The official ones: Fetch (clean web pages), Filesystem,
  Git, Memory (knowledge graph), Sequential Thinking (step-by-step reasoning), Time. Brave Search has an official server.
- **Memory + RAG with small local models** (e.g. J.A.R.V.I.S.2.0: 2-5B models plus Gemini, RAG, efficient memory).
  Jarvis now has this (semantic recall) - keep growing it.
- **Self-adapting / self-learning** (M.I.L.E.S "chooses and retains long-term memories", AbuZar-Ansarii "self-evolving
  workflows"). Jarvis has this (lessons + nightly evolve).
- **Multi-agent "command center" + layered security** (PocketPaw: 7-layer security, Telegram control, Ollama support).
- **Real-time streaming voice** (CyberVerse: WebRTC; addyosmani/jarvis: Gemini Live API).
- **Home/phone integration** (Home Assistant, Spotify, Android control, WhatsApp/Telegram bots).

## Gameplan, in order
1. DONE: reminders/alarms that survive restarts, beep + speak + phone notification + text.
2. DONE: live-fact tools, semantic memory, admin-box alerting, instant routes (time, weather, install, AnyDesk).
3. Reconnect Gemini in OmniRoute (free tier) - 5 minutes, biggest win for hard questions.
4. Add 3 MCP servers through Jarvis's existing MCP support (safe, read-mostly): Fetch, Time, Sequential Thinking.
   Skip Filesystem/Git until Jarvis's approval flow is proven on the phone.
5. Calendar + contacts (Google Calendar or Outlook) so "what's my day" and "book a customer at 3" are real.
6. Home Assistant / Spotify only if you actually use them.
7. Streaming voice: speak while the model is still writing (biggest remaining speed win).
8. Telegram or WhatsApp approval bot as a second phone channel (approvals + commands), in addition to Quo SMS.
9. Weekly "review my lessons" job so self-learned rules never go bad.
10. Safety rails before real autonomy: spending cap, "undo" log of every action, daily digest text of what it did.

---

# Audit + gameplan (verified in code, 2026-11-13)

Method: read all 50 modules (13,242 lines), 2 static pages, 33 test files, launchers. Baseline: `compileall` clean,
`pytest` 251 passed in 53s via `.venv`. Nothing below was changed; this is the record of what was found.

## A. The "fixed" list, checked against the code

All twelve are genuinely present. "Works in practice" is unproven for each - the assistant was not run.

| Claim | Status | Where |
|---|---|---|
| Made-up answers caught | built | `llm.py:47` is_guard_fail, `:82` is_fake_promise, `:149` data_unbacked, `:162` unbacked_claim |
| Fake actions corrected | built | same guards; `quick.py:290` really dispatches `press_keys win+d` |
| Screen reader honest | built | `computer.py:370` -> `screen.look(want_visual=False)`; `screen.py:105` labels vision "can be wrong" |
| Screen questions ~8s | built | vision is opt-in, not loaded by default |
| Can switch its own brain | built | `quick.py:77` model list, `:99` switch, `:109` vague-switch fallback |
| Free tier via freellmapi | built | `config.py:115` `http://localhost:20128/v1` |
| Restart picks up `.env` | built | `main.py:445-460` strips dotenv keys from the child env |
| Approvals by text | built | `sms.py:215` sends the approval text, `:156` checks `YES <PIN>` |
| Phone shows active brain | built | `phone.html:89-98` brain strip + panel |
| Claude 600/month cap | built | `claude_tiers.py:117` `CLAUDE_MAX_CALLS_PER_MONTH=600`, `:121` enforced |
| Limits & usage card | built | `dashboard.html:229`, `/api/limits` every 10s |
| Stops listening sooner | built, unverified | `silence_seconds` default 1.0 - needs an ear test, not code |

## B. The "not fixed" list, checked

- **Keeps stopping** - `runlog.py` heartbeat + `describe_previous()` exists. No auto-restart task anywhere
  (`grep AUTO_RESTART` returns nothing). Still open.
- **Interrupt mid-sentence (voice)** - built: `voice.py:401` `_start_barge`, `main.py:356` `_barged_in`.
- **Interrupt with text** - NOT built. Root cause in section C.
- **Phone page vs dashboard** - phone page kept, dashboard local-only. Unchanged by design.
- **Claude Code approval box** - not addressable from this codebase.
- **iPhone testing** - untested.
- **Self-change never completed** - pipeline exists (`selfmod.py`), never ran live.
- **"texts today" date filter** - NOT built. `sms.py` only has a polling cursor (`since`), no date query.
- **Fork seal supplier** - NOT built. `grep supplier friday/*.py` returns nothing.
- **Telegram/WhatsApp** - not built.

## C. Why text cannot interrupt speech

`main.py:318` `say()` calls `tts.speak(text)`, and `voice.py:392` `speak()` blocks the calling thread until the audio
finishes. The main loop is inside that call, so typed text sits in `self.inputs` until speech ends.
`_process_queued_input` exists but is only reached when the mic is down (`main.py:813-820`).

Fix (~40 lines): while `tts.busy()`, a watcher thread drains `self.inputs`; on the first item it calls `tts.stop()`
(already implemented at `voice.py:514`), then hands the text to `handle()`. Same hook for the dashboard/phone send path.

## D. OpenJarvis research

`open-jarvis/OpenJarvis` (Stanford SAIL / Hazy Research, Apache-2.0) is a local-first agent framework, not a voice
assistant. Five primitives: Intelligence, Engine, Agents, Tools & Memory, Learning. Eight agents (`simple`,
`orchestrator`, `native_react`, `deep_research`, `morning_digest`, `monitor_operative`, `operative`,
`native_openhands`). Memory backends: SQLite/FTS5, FAISS, ColBERTv2, BM25, Hybrid RRF. Traces in SQLite feeding a
`TraceDrivenPolicy` that learns which model/agent/tool wins per query type. Docker/Podman sandbox with mount
allowlists. OpenAI-compatible server on `:8000`, React UI on `:5173`.

Worth borrowing:
1. Trace-driven routing - `data/trace.jsonl` already exists (245 KB). Score each route's latency and success, then pick
   per query type instead of the fixed `split.wants_free()` heuristic. Biggest response-quality win available.
2. Hybrid retrieval (BM25 + vector, RRF) - fixes "who's my fork seal supplier" properly instead of a regex.
3. Persona files (`SOUL.md`/`MEMORY.md`/`USER.md`) - cheap, no vector DB, shrinks the 1,151-line system prompt.
4. `morning_digest` as a scheduled agent - `routines.py` and `agenda.py` already exist; assembly, not new code.
5. Container sandbox - strictly stronger than the audit-hook cage for untrusted skills.

Not worth borrowing: the React/Node 22 frontend (trades a 54 KB static page for a build pipeline), and the full
five-primitive rewrite (`tools.py` already covers Tools & Memory adequately).

## E. Phases

### Phase 0 - stop the bleeding (~1 day)
1. DONE (text interrupt): `listening.py` `_interrupt_watch` stops the voice and re-queues the typed text; `handle()`
   and `_api_ask()` stop speech on any new request. Pinned by `tests/test_interrupt.py` (6 tests).
2. DONE (open_app injection): `tools.py` `_launch_target_ok` allows only plain names, URIs and paths; `os.startfile`
   first, `subprocess.Popen(["cmd","/c","start","",target])` as a list fallback. No `shell=True` left in the function.
3. DONE (_quote/_unquote): `settings_store.py` `_unescape` reverses the escaping in one pass; quoted values are scanned
   to the real closing quote, so `a"b`, `a\b` and `C:\Users\ibbi` round-trip.
4. DONE (set_reminder): `tools.py` `if in_minutes is not None`, and a zero/negative duration is refused instead of
   silently falling through to `at_time`.
5. DONE (is_placeholder): `config.py` markers are template-shaped (`your-`, `xxxx`, `changeme`, `placeholder`,
   `example.com`) plus an exact-match set, so `sk-real-key-here` is no longer reported as missing.
   All four pinned by `tests/test_phase0.py` (40 tests).

### Phase 1 - security and secrets (~1 day)
6. DONE (backups): `BACKUP_INCLUDE_ENV` now defaults to false in `backup.py` and in the dashboard schema, so a backup
   zip no longer carries your API keys unless you switch it on. Pruned 2026-11-13: 12 `.env.backup-*` in the project
   root and 8 `data/backups/env-*.bak` deleted, after confirming the live `.env` (62 keys) is a superset of every one.
   NOTE: `settings_store.backup()` still writes `data/backups/env-*.bak` on each dashboard save and keeps the last 20,
   so those will reappear; reducing that retention is a code change, not yet approved.
7. DONE (skill import in the cage): `skills.py` `load_skill` no longer calls `exec_module` in Friday's process. When the
   cage is on it calls `sandbox.probe_tools()`, which imports the skill in the sandbox subprocess and reports the tools
   it registers (name, description, params, dangerous, signature); the main process then registers stubs whose func is
   `_caged(...)`. Import-time code (decorators, class bodies, module-level statements) therefore never runs in Friday.
   `SKILL_SANDBOX=false` keeps the old in-process import. Pinned by `tests/test_skill_sandbox_import.py` (6 tests).
8. DONE (SSRF): `web.py` `check_url()` refuses any scheme other than http/https, blocked hostnames (localhost,
   *.localhost, *.internal, metadata), and any literal or DNS-resolved address that is loopback, private, link-local,
   CGNAT, multicast, reserved or unspecified. `fetch()` checks before the request AND re-checks the final URL after a
   redirect. `DNS_CHECK` is off in tests so they stay off the network. Pinned by `tests/test_phase1_ssrf.py` (26 tests).
9. DONE (SMS PIN): `sms.py` compares the PIN with `hmac.compare_digest`, and an approval by text is refused outright when
   no PIN is configured (an empty PIN used to approve anything). The approval text now says to set SMS_PIN.
10. DONE (shell): `ALLOW_SHELL` defaults to false in `config.py` (dataclass and loader), so `run_shell_command` is opt-in.
    Pinned by `tests/test_phase1_defaults.py` (13 tests).
11. Rotate the freellmapi and Claude keys - both appeared in chat.

### Phase 2 - correctness under load (~1 day)
12. DONE (credit-gate lock): `credits.py` `authorize()` no longer holds `self._lock` across `confirm()`. The cap check
    and ledger entry are built under the lock, the prompt runs outside it, and the append plus `_save()` re-acquire it.
    `charge()` and `status()` now run during an approval window instead of freezing for up to 180s.
13. DONE (Memory.add lock): `memory.py` holds `_lock` across the append/slice/pop and writes through `_save_unlocked()`,
    so two turns arriving together can no longer interleave and drop one.
14. DONE (atomic settings swap): `main.py` `apply_settings` builds the new values and applies them with one
    `self.s.__dict__.update(...)`, preserving object identity for ctx/brain/tts, instead of 47 per-field `setattr` calls
    a reader could observe halfway through.
    All three pinned by `tests/test_phase2_concurrency.py` (10 tests).
15. Cap `jobs._JOBS`; rate-limit `local_api` auth failures (`local_api.py:137`); cache `nvidia-smi` 30s
    (`dashboard.py:87`, currently every 4s).

### Phase 3 - response quality (~3-4 days)
16. Trace-driven router replacing `split.wants_free()`.
17. Hybrid BM25+vector retrieval over notes/lessons/knowledge.
18. Date-filtered SMS queries ("what texts today").
19. Notes-first lookup before web search (fork seal supplier).
20. Persona files to shrink the system prompt.

### Phase 4 - reliability (~2 days)
21. Auto-restart scheduled task (offered, never answered).
22. Prove the self-change pipeline end-to-end on a trivial change.
23. Verify the silence change by ear; tune `silence_seconds`.

### Phase 5 - UI (~3-5 days)
24. Streaming token render on the dashboard (the server already streams to TTS).
25. Interrupt button wired to `tts.stop()` on both pages.
26. Trace timeline panel from `data/trace.jsonl`.
27. Mobile-responsive dashboard behind the existing token, instead of a second page.

### Phase 6 - hygiene (~2 days)
28. `ruff` + `mypy` in `requirements-dev.txt`; pin versions (no lockfile today).
29. DONE: `llm.py` split into `guards.py` (honesty rules), `prompt.py` (system prompt) and `router.py` (the escalation
    ladder, as a mixin Brain inherits); `main.py` split into `approvals.py` (permission flow), `listening.py` (mic +
    listen cycle + text interrupt) and `cli.py` (command line). `llm.py` 1,151 -> 569, `main.py` 1,007 -> 604, every old
    name re-exported so no caller changed. Pinned by `tests/test_router.py` (12 tests).
30. Replace 192 broad `except Exception` with logged, narrow handlers; 62 `print()` -> logger.
31. Delete `change`, `service.conf.lock`, `system.conf.lock`.

## F. How good this can be

Ceiling: a genuinely good personal assistant, not a product.

Already rare: a credit gate at the single API chokepoint, an audit-hook sandbox, a self-modification pipeline with
rollback, 251 passing tests. Most hobby assistants have none of that. The architecture is sound; the problems are
concentrated in a handful of monoliths and a long tail of small bugs.

- After Phases 0-2: no known security holes, no silent data corruption, interruptible by voice and text. Solid daily driver.
- After Phase 3: responses stop guessing, because routing is measured rather than assumed.
- After Phase 5: it looks like something worth showing people.

Won't become: cross-platform (Windows-only by design), a multi-user product (single-owner model baked into `sms.py`
and `local_api.py`), or a research platform like OpenJarvis. Don't chase those.

Binding constraint is not ambition, it is the two 1,000-line files. Every phase gets harder until Phase 6 lands;
move item 29 earlier if building continues.
