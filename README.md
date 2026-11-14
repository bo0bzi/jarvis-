# Friday — your voice-activated AI assistant for Windows

Friday is a Jarvis-style assistant that runs on your Windows 11 PC. Say **"Hey Friday"** (or **"Jarvis"**), ask for something, and Friday answers out loud. It can also do things on your computer: open apps, search the web, play YouTube, check the weather, change the volume, take screenshots, set timers and reminders, take notes, and find files.

- **Brain:** Abacus.AI RouteLLM by default (included with your Abacus.AI plan). It can also use OpenAI, Anthropic Claude, or a local model.
- **Ears:** offline speech recognition (Whisper), with free Google speech recognition as a backup.
- **Voice:** natural Microsoft neural voices (edge-tts), with offline Windows voices as a backup.
- **Safety:** Friday always asks you first before it deletes anything, runs commands, empties notes, or shuts down the PC.
- **Always listening:** say the wake word once, then just keep talking. Mute the mic any time.
- **Dashboard:** a control panel in your browser with every setting, the status of each part of Friday ("agents"), and checks for your API key, Abacus credits, internet, microphone, voice and PC.

---

## 1. Setup, step by step (no programming needed)

### Step 1 — Unzip
1. Right-click `friday_jarvis.zip` → **Extract All…** → pick a folder you'll keep, for example `C:\Users\<you>\Friday`.
   *Don't run it from inside the zip.*

### Step 2 — Get your Abacus.AI API key
1. Sign in at **https://abacus.ai/app/route-llm-apis** (or open ChatLLM and click the **RouteLLM API** icon in the lower-left corner).
2. Create or copy your API key. Treat it like a password.

> Claude Code note: a Claude Code / Claude.ai subscription does **not** come with an API key. To use Claude as Friday's brain you need a separate key from https://console.anthropic.com. You don't need one, because Abacus RouteLLM can route to Claude models too.

### Step 3 — Run the installer
1. Open the extracted folder and double-click **`install.bat`**.
   - If Windows shows "Windows protected your PC", click **More info → Run anyway**. This happens because the script isn't signed.
2. The installer:
   - checks for **Python 3.10+** and offers to install Python 3.12 for you (with `winget`) if it's missing
   - creates a private environment (`.venv`) and installs everything Friday needs
   - asks which AI service to use (press **1** for Abacus) and asks you to **paste your API key** (it stays hidden while you type)
   - asks what Friday should call you and which city to use for weather
   - offers to download the offline speech model (~150 MB), create a **Desktop shortcut**, and **start Friday when Windows starts**
   - finishes with an optional self-check that tests your key, microphone, and voice

### Step 4 — Start Friday
Double-click **`run_friday.bat`** (or the Desktop shortcut). After it says "Friday online", say:

> "Hey Friday, what's the weather today?"

If you just say "Friday" and pause, it answers "Yes?" and waits for your request.

The **dashboard** opens in your browser at the same time (see section 3).

### Updating from an older version
1. Close Friday. Extract the new zip **over your existing Friday folder** (choose "Replace the files"). Your `.env` (API key) and `data` folder are kept.
2. Double-click **`install.bat`** again. It adds the new settings to your `.env` and asks whether to switch to always-listening (say **Y**). When it asks to change the AI settings, say **N** to keep your key.

---

## 2. Ways to talk to Friday

### Always listening (default)
Friday listens all the time while it runs.
- Say the wake word **anywhere in a sentence**: "Hey Friday, what's the time?" or "What's the time, Jarvis?"
- After it answers you have **20 seconds** (`FOLLOWUP_SECONDS`) to keep talking **without** the wake word. The status shows *conversation* while that window is open.
- Say **"stop"**, **"that's all"** or **"never mind"** to end the conversation early. To cut Friday off while it's talking, press **Ctrl+Alt+S** or the dashboard's **Stop speaking** button (the mic is paused while Friday speaks so it doesn't hear itself).
- **Conversation mode** (dashboard switch, `/chat`, or "turn on conversation mode") makes Friday answer everything it hears, with no wake word. Good when you're alone in the shop; turn it off when other people or a radio are around.
- **Mute the microphone** with the dashboard button, the tray icon, **Ctrl+Alt+M**, `/mic`, or by saying "mute the microphone". When muted, Friday hears nothing at all.
- Friday ignores its own voice, so it won't answer itself through your speakers. If the mic stops working (for example, you unplug a headset), Friday keeps retrying and shows the problem on the dashboard.

| Launcher | Mode | How it works |
|---|---|---|
| `run_friday.bat` | **Always listening** | As described above. You can also type a message in the window. |
| `run_friday_push_to_talk.bat` | **Push-to-talk** | Press **ENTER** (or the global hotkey **Ctrl+Alt+F**) and then speak. |
| `run_friday_text.bat` | **Text** | Type your messages. Friday still speaks the replies (set `TTS_ENGINE=none` to turn that off). |
| `open_dashboard.bat` | **Dashboard** | Opens the dashboard of the Friday that's already running. |
| `check_friday.bat` | **Self-check** | Tests your API key, credits, microphones, and plays a test sentence. |

Typed commands: `/help`, `/tools`, `/mode always|wake|ptt|text`, `/mute` (voice), `/mic` (microphone), `/chat` (conversation mode), `/dashboard`, `/clear` (forget the conversation), `/quit`. You can also say "goodbye".

**Hotkeys:** Ctrl+Alt+F push-to-talk, Ctrl+Alt+M mute mic, Ctrl+Alt+S stop talking (change them in the dashboard).

**Status** appears in the console window with colours: listening (green), conversation, thinking (yellow), using a tool (purple), speaking (blue), muted. There's also a matching coloured **tray icon** near the clock. Right-click it to mute the mic or voice, open the dashboard, or quit.

### Things to try
- "What time is it?" / "What's the weather in London?"
- "Open Spotify" / "Close Chrome" / "Open YouTube dot com"
- "Play Back in Black on YouTube"
- "Search the web for the best pizza near me"
- "Turn the volume up" / "Set volume to 30" / "Mute"
- "Set a timer for 10 minutes" / "Remind me to call mom at 18:30"
- "Take a screenshot" (saved in *Pictures\Friday Screenshots*)
- "Note that the wifi password is on the router" / "Read my notes"
- "Find my PDF files in Downloads"
- "How's my PC doing?" (CPU, RAM, disk, battery)
- "Remember that my favourite team is the Raptors" (long-term memory)
- "Lock my computer" (asks for confirmation first)
- "Open the dashboard" / "Mute the microphone" / "Turn on conversation mode"

---

## 3. The dashboard

The dashboard opens in your browser when Friday starts. You can also open it from the tray icon, `open_dashboard.bat`, `/dashboard`, or by saying "open the dashboard". It only works on **this PC** (nobody on your network can reach it), and it's protected with a private key stored in `data\dashboard_token.txt`. Friday has to be running for the dashboard to work.

| Tab | What's there |
|---|---|
| **Overview** | Current status, big buttons (mute mic, mute voice, conversation mode, stop talking), a live mic level meter, chat box (type to Friday), Abacus credits left, today's usage, and Approve/Deny buttons when Friday asks permission for something risky |
| **Agents** | Each part of Friday with a live status light: microphone, speech recognition, listener, AI brain, voice, tools, timers/reminders, memory, dashboard. Restart the audio or brain from here |
| **Status checks** | One click (or automatically every 10 minutes) tests: internet, **AI API key and model** (with response time), **Abacus credits** (points left out of your monthly total), microphone, speech recognition, voice, PC health (CPU/RAM/disk/battery) and installed packages. Each check explains how to fix a problem |
| **Settings** | Every `.env` setting, grouped, with explanations. API keys are hidden. Saving makes a backup of your old `.env` in `data\backups`. Most changes apply right away; the page tells you if a restart is needed (there's a Restart button) |
| **Tools** | Switch any of Friday's tools on or off |
| **Memory & timers** | Long-term facts (add/delete), notes, running timers/reminders (cancel), clear the conversation |
| **Event log** | Everything Friday heard, said and did, with errors highlighted |

**About credits:** the credit check uses your Abacus key to read your organization's compute points (`computePointsLeft` / `totalComputePoints`), the same number shown in ChatLLM. If Abacus changes that, the check says the credits aren't available and links to your Abacus billing page. Friday never makes the number up.

The **usage** numbers (requests and tokens today/this month) are counted by Friday on your PC from what the API reports back. They're a guide, not your bill.

---

## 3b. Free local AI brain + Abacus only for hard cases (saves credits)

Friday can run its "brain" on a free AI model **on your own PC** (via [Ollama](https://ollama.com)) and only spend
Abacus credits when it really needs to.

**Setup (one time):** double-click **`setup_local_ai.bat`**. It will:
1. install Ollama (free) if you don't have it,
2. check your graphics card and recommend a model (`qwen2.5:3b` light · `qwen2.5:7b` older/reliable · **`qwen3.5:9b` newest, recommended for 8–12 GB GPUs, also reads photos** · `qwen2.5:14b` big),
3. download it and test it,
4. switch Friday to it, with Abacus as the **backup brain** (if your Abacus key is set). A copy of your old `.env` is saved first.

Then restart Friday.

**Which Qwen?** There is no "Qwen 3.8". Qwen3.6 exists but its smallest size is 27B (17 GB+), too big for a 12 GB card — it would spill into normal RAM and be very slow. `qwen3.5:9b` (6.6 GB) is the newest one that fits. Friday switches its slow "thinking" step off so voice answers stay quick; set `LOCAL_THINKING=true` in `.env` for slower, deeper answers. If the download fails, update Ollama first (ollama.com/download).

**When does Friday use Abacus credits?** Only when:
- you say a "big brain" phrase in your request, e.g. *"Friday, **think hard** about how to price my winter storage"*
  (phrases: `think hard`, `big brain`, `ask abacus`, `use abacus`, `go deep` — editable),
- the local AI is not running, errors, or gets stuck,
- the local AI itself replies that the request is too hard (turn off with `SELF_ESCALATE=false`).

There's a daily cap (`BACKUP_MAX_PER_DAY`, default 25), so credits can't run away. When Abacus answers, the console says
*"Answered by backup AI…"*, and the dashboard's **Status checks → Backup AI** card shows how many uses are left today.
Everything is in the dashboard under **Settings → Backup brain**.

**FreeLLMAPI instead of Ollama?** Settings → AI brain: provider `custom`, URL `http://localhost:3001/v1`, your
FreeLLMAPI key, model `auto`. The Abacus backup works the same way.

**Tips**
- Local models are fast on an NVIDIA GPU and slow on CPU only. If replies are too slow, pick `qwen2.5:3b`.
- If PC actions (apps, timers…) misbehave with the local model, set **Tool calling mode** to `json`.
- Ollama must be running (it starts with Windows after install). If it isn't, Friday automatically uses the backup.
- To go back to Abacus only: Settings → AI provider = `abacus`.

---

## 3c. Email skill

Friday can check, read and search your email, and send or reply (it always asks you before sending).
Checking and reading **never marks emails as read**.

**Setup (Gmail or Google Workspace business email, e.g. info@yourbusiness.com):**
1. Turn on 2-Step Verification for the Google account (myaccount.google.com → Security).
2. Go to **https://myaccount.google.com/apppasswords**, create one called "Friday", and copy the 16-letter password.
3. Dashboard → **Settings → Email**: provider `gmail`, your email address, and paste the app password → **Save**.
4. **Status checks → Email** should turn green ("Connected · N unread").

Try: *"Friday, check my email"*, *"read the one from Bob"*, *"any emails about ATV quotes this week?"*,
*"reply to it: thanks, we can do Thursday"*.

Privacy: with the local AI brain, your emails are read by the AI on your PC. With Abacus, the email text is sent to Abacus
to be answered.

---

## 3d. Teaching Friday new skills (it learns by itself)

Ask for something Friday can't do yet, or say **"Friday, learn how to …"**, e.g.
*"learn how to get the price of a stock"*, *"learn how to tell me the gas price in St. Catharines"*.

What happens:
1. Friday asks the **strongest AI** you have set up to write the new skill. That's the backup brain (Abacus), so one
   learned skill costs one backup use. With no backup set, the main AI writes it.
2. The code is safety-checked and test-loaded. If it fails, the AI gets one more try with the error.
3. Friday tells you what the skill does and **asks permission**. Say yes, and it works straight away, with no restart.

Safety:
- Skills are small Python files in the **`skills`** folder. They can't read your `.env`/keys, can't change Friday's own
  code, and can't run anything on load.
- Anything that deletes or changes files, runs programs, or sends data must ask you before **every** use.
- Unapproved skills wait in `skills/_pending`. Old versions are kept in `skills/_backup`.

Manage by voice: *"what skills have you learned?"*, *"improve the stock price skill so it also shows the % change"*,
*"switch off the stock price skill"*, *"restore it"*. The dashboard **Status checks → Learned skills** card shows them all.

Tip: for bigger skills, open the Friday folder in **Claude Code** and ask it to write a skill in `skills/`
(CLAUDE.md explains the rules), then say *"Friday, reload your skills"*. That uses your Claude plan, not Abacus credits.

---

## 4. Settings (`.env` file)

The easiest way to change settings is the dashboard's **Settings** tab. They're all stored in the `.env` file in the Friday folder, so you can also right-click it → **Open with → Notepad** and restart Friday afterwards. The most useful settings:

| Setting | What it does |
|---|---|
| `LLM_PROVIDER` | `abacus` (default), `openai`, `anthropic`, or `custom` (Ollama / LM Studio / FreeLLMAPI) |
| `CUSTOM_BASE_URL` / `CUSTOM_MODEL` | Local/custom AI server and model (Ollama: `http://localhost:11434/v1`, `qwen2.5:7b`) |
| `BACKUP_PROVIDER` | `none` (default) or `abacus` etc. — the "big brain" used only for hard cases (see 3b) |
| `BACKUP_MAX_PER_DAY` | Daily cap on backup uses (default 25; `0` = never) |
| `ESCALATE_ON_ERROR` / `SELF_ESCALATE` | Use the backup when the main AI fails / when it says a request is too hard |
| `ESCALATE_PHRASES` | Words that force the backup for one request, e.g. `think hard,big brain` |
| `ABACUS_API_KEY` / `ABACUS_MODEL` | Your key; `route-llm` picks the best model automatically, or put a specific model name from the RouteLLM page |
| `WAKE_WORDS` | Comma-separated, e.g. `hey friday,friday,jarvis,computer` |
| `INPUT_MODE` | `always` (default), `wake` (wake word every time), `ptt` or `text` |
| `FOLLOWUP_SECONDS` | How long you can keep talking without the wake word after a reply (default 20) |
| `CONVERSATION_MODE` | `true` = answer everything, no wake word |
| `DASHBOARD` / `DASHBOARD_PORT` | Turn the dashboard on/off; its address is `http://127.0.0.1:8765` |
| `DASHBOARD_OPEN_BROWSER` | `false` = don't open the browser when Friday starts |
| `AUTO_CHECK_MINUTES` | How often status checks run by themselves (`0` = only when you click) |
| `DISABLED_TOOLS` | Tools to switch off (easier in the dashboard's Tools tab) |
| `EDGE_VOICE` | `en-GB-RyanNeural` (British male, very Jarvis). For a female "Friday" voice try `en-IE-EmilyNeural` or `en-GB-SoniaNeural` |
| `TTS_ENGINE` | `edge` (natural, online), `pyttsx3` (offline), `none` |
| `STT_ENGINE` / `WHISPER_MODEL` | `whisper` (offline; `tiny.en` is fastest, `small.en` is most accurate) or `google` |
| `MIC_DEVICE` | Microphone number or part of its name (see `run_friday.bat --list-mics`) |
| `ENERGY_THRESHOLD` | `0` = automatic. Raise it (e.g. `0.03`) if noise keeps triggering Friday |
| `ALLOW_SHELL` | `false` to turn off the "run command" tool completely |

Your conversation history and long-term facts are stored in `data\memory.json`, and notes are stored in `data\notes.txt`. Delete these files to reset them.

---

## 5. Troubleshooting

**`No module named 'openai'` (or `pystray`, `numpy`...)** — some of Friday's parts didn't install in this folder.
Double-click **`repair_friday.bat`** (needs internet). It reinstalls everything and shows the real error for anything
that fails. The start-up files also run this repair automatically when core parts are missing.
Note: if you unzip Friday into a **new folder**, run `install.bat` there first.

**"I can't reach the local AI on this PC"** — Ollama isn't running. Open **Ollama** from the Start menu (or run
`setup_local_ai.bat` again). If a backup brain is set, Friday uses it automatically meanwhile.

**"Friday can't start yet: No API key set"** — Open `.env` in Notepad and paste your key after `ABACUS_API_KEY=` (no spaces or quotes), then save.

**"My API key was rejected"** — Copy the key again from https://abacus.ai/app/route-llm-apis and make sure `LLM_PROVIDER` matches the key you pasted.

**"The AI model name wasn't accepted"** — Set `ABACUS_MODEL=route-llm`, or use a model name exactly as it's listed on the RouteLLM API page.

**Friday doesn't hear me / microphone problems**
1. Windows **Settings → Privacy & security → Microphone**: turn on *Microphone access* **and** *Let desktop apps access your microphone*.
2. **Settings → System → Sound → Input**: pick the right mic and talk to check that the level bar moves.
3. Run `check_friday.bat` to see your microphones. Put the right number in `.env` as `MIC_DEVICE=2` (for example).
4. Stay quiet for the first second after Friday starts, because that's when it measures room noise.
5. If it keeps triggering on noise, set `ENERGY_THRESHOLD=0.03`. If it never triggers, try `0.005`.
6. If the wake word is unreliable, use **push-to-talk** (`run_friday_push_to_talk.bat`) or say the wake word clearly before you pause.

**PyAudio errors** — Friday does **not** use PyAudio. It uses `sounddevice`, which includes its own audio driver on Windows, so the usual "PyAudio won't install" problem doesn't apply. If another guide tells you to install PyAudio, you can skip it.

**A package FAILED during install** — Your Python version is usually too new for that package. Install **Python 3.12** from python.org (tick *Add python.exe to PATH*), delete the `.venv` folder, and run `install.bat` again. Friday still works with fallbacks: without `faster-whisper` it uses Google speech recognition, and without `edge-tts` it uses the offline Windows voice.

**No voice / silent replies** — Check your speakers and volume. Edge voices need internet. Try `TTS_ENGINE=pyttsx3` for the offline voice.

**Speech recognition is slow** — Set `WHISPER_MODEL=tiny.en`, or `STT_ENGINE=google`.

**The global hotkey doesn't work** — Some games and admin windows block it. Pressing ENTER in Friday's window always works.

**"Running scripts is disabled on this system"** — Always start the installer with `install.bat`. It runs PowerShell with a one-time bypass, so you don't need to change system settings.

**Friday answers the TV / people talking** — Turn off conversation mode, lower `FOLLOWUP_SECONDS` (e.g. `8`), or raise `ENERGY_THRESHOLD`. Muting the mic stops everything.

**The dashboard says "locked" or won't open** — Open it with `open_dashboard.bat` or the tray icon (they include the private key). If the page doesn't load, Friday isn't running, or another program uses port 8765: change `DASHBOARD_PORT` in `.env` (e.g. `8777`).

**Credits check says "not available"** — Your key works for chat but Abacus didn't return the credit numbers. Check them on your Abacus page (link in the check). Friday keeps working.

**Antivirus warning about the `keyboard` package** — It listens for the push-to-talk hotkey. To avoid it, remove the `PTT_HOTKEY` value in `.env`.

---

## 6. Extending Friday with new tools

All tools are in `friday/tools.py`. To add one, write a function and decorate it. The LLM learns about it automatically:

```python
@tool("flip_coin", "Flip a coin and say heads or tails.")
def flip_coin(ctx):
    import random
    return random.choice(["Heads", "Tails"])

@tool("open_project", "Open one of my project folders by name.",
      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def open_project(ctx, name: str):
    os.startfile(rf"C:\Users\Ibbi\Projects\{name}")
    return f"Opened {name}."
```

Rules:
- Return a short **string**. Friday reads it and turns it into a spoken reply.
- Add `dangerous=True` for anything risky. Friday will then always ask "Should I go ahead?" before it runs.
- `ctx.settings`, `ctx.memory`, and `ctx.speak("...")` are available inside tools.
- To add more app nicknames for "open …" / "close …", add them to `APP_ALIASES` and `PROCESS_ALIASES` in `tools.py`.
- Run the tests afterwards: `.venv\Scripts\python -m pip install pytest` then `.venv\Scripts\python -m pytest`.

Project layout:

```
friday/
  main.py    – the main loop (wake word / push-to-talk / text), confirmations, CLI
  llm.py     – AI brain: Abacus/OpenAI/custom (OpenAI-compatible) + Anthropic, tool-calling loop
  tools.py   – everything Friday can do on the PC
  voice.py   – microphone, speech-to-text, wake word, text-to-speech
  memory.py  – conversation + long-term facts (data/memory.json)
  ui.py      – console status + tray icon
  config.py  – reads .env
tests/       – unit tests (no microphone needed)
```

---

## 7. Customising Friday with Claude Code

You have Claude Code, so you can change Friday by just describing what you want:

1. Open a terminal in the Friday folder (in File Explorer, click the address bar, type `cmd`, and press Enter).
2. Run `claude`.
3. Ask for what you want, for example:
   - *"Add a tool that tells me my next Google Calendar event."*
   - *"Make Friday greet me with the weather and my notes when it starts."*
   - *"Add 'open discord' and 'open obs' to the app list."*
   - *"Change the personality to be more sarcastic, like Jarvis."* (this is the system prompt in `friday/llm.py`)
4. Ask Claude Code to run the tests (`.venv\Scripts\python -m pytest`) after each change.

The included `CLAUDE.md` file explains the project to Claude Code automatically. **Never paste your `.env` file or API key into a chat.**

---

## 8. Start on boot / uninstall
- Turn on: `enable_start_on_boot.bat`. Turn off: `disable_start_on_boot.bat`.
- Uninstall: turn off start on boot, delete the Desktop shortcut, and delete the Friday folder. Nothing else is installed system-wide, except Python if the installer installed it.

## Privacy
- With `STT_ENGINE=whisper`, your voice is processed **on your PC**. Only the text of your request goes to the AI service you chose.
- Edge voices send the reply **text** to Microsoft to generate the audio. Use `TTS_ENGINE=pyttsx3` to keep everything offline except the AI.
- In wake-word mode, Friday transcribes speech locally to listen for its name. Nothing is sent anywhere until it hears the wake word.

## What's new in v2.0.8 - real internet + browser/computer control

- **web_search now returns real results** (titles, links, snippets) instead of just opening a Google tab.
- **browse_web reads any live page**: title, text, headings, links, GitHub links and what the site is built
  with (WordPress, Shopify, Next.js...). Free, no install needed. Made-up names like `web_browse` also work.
- **Paste a link** (or "read integritypowersports.com") and Friday reads it right away, no AI guessing.
- **Friday's own browser** (needs setup_browser.bat once): it opens a browser window you can watch, reads the
  page, clicks buttons and fills in boxes. Uses Chromium, or your Chrome/Edge if Chromium isn't there.
  Logins you do in that window are remembered.
- **Look at the screen**: "look at my screen" / "what's on the page?" uses the local vision model (free).
- **Mouse + keyboard**: click, type, press shortcuts like ctrl+s.
- **Permission**: reading is free; every click/keystroke shows a permission card. Say "you can use my computer
  for 10 minutes" to turn on control mode (it asks once). Passwords, card numbers and buy/pay buttons ALWAYS
  ask. Emergency stop: shove the mouse into a screen corner, or say "stop computer control".
- Friday is told it HAS internet and gets nudged if it makes "I can't browse" excuses.

**Setup:** close Friday, double-click `setup_browser.bat`, then start Friday again.

## What's new in v2.0.7

- **Direct commands (no AI guessing).** "install https://github.com/owner/repo" installs right away. If it's a skill pack (instructions only), you aren't asked a second time. MCP servers, which run code, still show the permission card. "list skills" and "status" / "update?" are also answered directly. This works from the PC, the dashboard, the phone page and texts.
- **Big installs run in the background.** A pack with thousands of skills downloads while you keep chatting. "status" shows the real progress, and Friday tells you when it's done.
- **Fewer tools at a time for the local brain.** It sees about 10–25 tools that match your message instead of all 66, and can load more itself. `TOOL_PICKER=off` in `.env` turns this off.
- **No fake promises.** If the AI says "installing…" without actually running anything, Friday makes it try again once. If it still runs nothing, she says plainly that nothing happened.
- **Bigger memory per request.** `setup_local_ai.bat` sets Ollama's context to 16K tokens on 8 GB+ graphics cards and 32K on 16 GB+ cards.
- **Fixes.** Huge GitHub file lists (9,000+ skills) no longer fail with "download too big". Placeholder repo names the AI made up are rejected. A pack can now keep up to 12,000 skills.
- **Note:** hosted MCP services, like Quo's at quo.com/docs/mcp, are not GitHub projects, so "find skills" can't install them yet.

## What's new in v2.0 (plain English)

**Update:** unzip over your Friday folder (keep `.env`, `data`, `.venv`), run `repair_friday.bat`, then `setup_local_ai.bat` (choose the 14b model and say yes to the helper models and GPU speech).

- **Runs on your graphics card.** The main brain, coding helper, photo reader and manual search all run free in Ollama. Speech recognition uses the GPU and falls back to the CPU on its own.
- **Credit gate.** Before Friday uses Abacus or any other cloud AI, she tells you the estimated credits and waits for your yes (by voice, the dashboard, or a text with your PIN). Daily and monthly caps live in Settings > Credits.
- **SMS (Quo).** Settings > SMS (Quo): turn it on and enter Friday's own Quo API key, her Quo number, your cell number and a PIN. She only answers your number. Send her a photo and the local photo model reads it.
- **Autonomy dial.** Settings > Autonomy: 0 = asks for everything, 1 = asks for risky things, 2 = mostly works on her own. Sending, deleting and spending money **always** ask.
- **Shop helpers.** "Add a job for John, 2017 ZX6R, chain and sprockets", "Mark Smith waiting for parts", "Text John his bike is ready", "Remind me about the sprocket in 3 days", "Draft a quote". The board is on the dashboard tab **Shop & credits**.
- **Manuals.** Put PDF service manuals in the `knowledge` folder and say "update the knowledge base". Then ask "what's the valve clearance on the Streetfighter?" and Friday answers with the manual and page number.
- **Memory search.** "What did I say about the Ducati last week?"
- **Morning briefing (7:30) and nightly check (2:00).** Overnight Friday makes a backup, looks at the day's errors and repairs broken skills using the local coder model only. Ask "what did you do last night?"
- **Backups.** "Back up now" or "restore the latest backup". Backups are kept in the `backups` folder.
- **GitHub skills.** "Find a skill for ...". Friday shows you what it does and asks before installing. Each one is pinned to an exact version. Big packs (1,000+ skills, like agentic-awesome-skills) work too: Friday downloads only the SKILL.md instruction files, skips duplicate copies, and searches them (search_installed_skills) instead of loading them all. A pack of about 3,000 skills takes roughly 1 to 3 minutes to install.
- **Phone access (optional).** Set a Local API key (16+ characters) in Settings > API. To reach Friday from your phone away from the shop, install **Tailscale** on the PC and the phone (same account), set "Allow other devices" on, and use `http://<your-pc-name>:8766`. Never port-forward on your router.
  - **Easiest phone chat (no Shortcut needed):** open `http://<PC Tailscale IP>:8766/` in Safari (iPhone) or Chrome (Android), type your API key once, then Share > **Add to Home Screen**. Tap the 🎤 on the keyboard to talk; tick **Talk back** to hear replies.
  - **Talk to Friday out loud from your phone (big 🎤 button, Friday's own voice).** The phone needs the secure **https** address for its microphone, and Tailscale gives you one for free:
    1. One time only: open the Tailscale admin page (login.tailscale.com) → **DNS** → turn on **HTTPS Certificates**.
    2. On the PC, double-click **phone_access.bat** in the Friday folder. It turns on `tailscale serve`, shows the `https://...ts.net` address (and copies it to the clipboard), and puts a **Friday Phone Access** shortcut on your Desktop for next time. (Manual way: `tailscale serve --bg 8766`.)
    3. Copy the `https://...ts.net` address it shows. To turn phone access off, run `phone_access.bat off` from a Command Prompt in the Friday folder.
    4. Open that address in Safari on the iPhone and type your API key once.
    5. Share → **Add to Home Screen**.
    6. Tap the big **🎤**, allow the microphone, and talk. It stops by itself when you pause (or tap it again).
    - Tick **Talk back** to hear replies in Friday's voice on the phone; tick **Hands-free** and the mic opens again after each reply, for a back-and-forth conversation. Tap 🎤 while she's talking to interrupt.
    - Your voice is turned into text by Whisper **on your PC** (nothing is sent to a cloud speech service). The reply voice is made on the PC: the "edge" voice is a free Microsoft online voice; "pyttsx3" is fully offline. If no voice is available, the phone's own voice is used.
    - Without the https address the page still works for typing (and the 🎤 on the iPhone keyboard for dictation).
