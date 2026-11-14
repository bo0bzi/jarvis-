"""The command line: `python -m friday` and its flags (--check, --ask, --text, --open-dashboard, ...).

Kept apart from main.py so the Assistant stays readable. Assistant is imported lazily inside main() because main.py
re-exports this module's entry points."""
from __future__ import annotations

import argparse
import os
import webbrowser

from . import __version__
from .config import load_settings
from .llm import Brain
from .memory import Memory
from .tools import ToolContext
from .voice import TextToSpeech, is_yes


def run_check(settings) -> int:
    from . import checks

    print(f"Friday {__version__} self-check\n")
    problems = settings.validate()
    print(f"Provider: {settings.provider} | model: {settings.model} | base URL: {settings.base_url or '(default)'}")
    print("Config: " + ("OK" if not problems else "PROBLEMS:\n  - " + "\n  - ".join(problems)))
    if not problems:
        print("Testing the AI connection...")
        brain = Brain(settings, memory=None)
        print("  AI says:", brain.chat("Reply with exactly: Systems online."))
    credits = checks.check_abacus_credits(settings)
    print(f"Abacus credits: {credits['summary']}")
    print(f"Email: {checks.check_email(settings)['summary']}")
    from . import skills
    r = skills.load_all()
    print(f"Learned skills: {', '.join(r['loaded']) or 'none'}" + (f" | broken: {', '.join(r['errors'])}" if r["errors"] else ""))
    try:
        from .voice import list_microphones

        print("Microphones:\n" + list_microphones())
    except Exception as e:
        print(f"Microphones: could not list ({e})")
    print("Testing voice output (you should hear a sentence)...")
    TextToSpeech(settings).speak("Voice check complete. All systems nominal.")
    return 0 if not problems else 1


def open_dashboard_from_cli(settings) -> int:
    from .dashboard import read_token

    token = read_token(settings.data_dir)
    url = f"http://127.0.0.1:{settings.dashboard_port}/?t={token}" if token else ""
    if not url:
        print("Start Friday first (run_friday.bat); the dashboard runs inside it.")
        return 1
    webbrowser.open(url)
    return 0


def main(argv: list[str] | None = None) -> int:
    from .main import Assistant  # lazy: main.py re-exports this function

    p = argparse.ArgumentParser(prog="friday", description="Friday - voice-activated AI assistant")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--always", action="store_true", help="always listening: wake word + follow-up conversation (default)")
    g.add_argument("--text", action="store_true", help="type instead of speaking")
    g.add_argument("--ptt", action="store_true", help="push-to-talk (press ENTER, then speak)")
    g.add_argument("--wake", action="store_true", help="wake word every time (no follow-up window)")
    p.add_argument("--no-dashboard", action="store_true", help="don't start the web dashboard")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard in the browser")
    p.add_argument("--open-dashboard", action="store_true", help="open the running Friday's dashboard and exit")
    p.add_argument("--check", action="store_true", help="test config, AI connection, credits, microphones and voice")
    p.add_argument("--list-mics", action="store_true", help="list microphones and exit")
    p.add_argument("--ask", metavar="TEXT", help="ask one question, print/speak the answer, then exit")
    args = p.parse_args(argv)

    settings = load_settings()
    if args.list_mics:
        from .voice import list_microphones

        print(list_microphones())
        return 0
    if args.open_dashboard:
        return open_dashboard_from_cli(settings)
    if args.check:
        from . import credits

        credits.GATE = credits.CreditGate(confirm=lambda prompt: is_yes(input(prompt + " (yes/no) ")))
        return run_check(settings)

    problems = settings.validate()
    if args.ask:
        if problems:
            print("Friday can't start yet:\n  - " + "\n  - ".join(problems))
            return 1
        ctx = ToolContext(settings=settings, memory=Memory(settings.data_dir, settings.memory_max_messages))
        ctx.confirm = lambda prompt: is_yes(input(prompt + " (yes/no) "))
        from . import credits

        credits.GATE = credits.CreditGate(confirm=ctx.confirm)
        from . import skills
        skills.load_all()
        reply = Brain(settings, ctx.memory, ctx).chat(args.ask)
        print(reply)
        TextToSpeech(settings).speak(reply)
        return 0
    if problems and (args.no_dashboard or not settings.dashboard):
        print("Friday can't start yet:\n  - " + "\n  - ".join(problems))
        print("\nFix the .env file (in the Friday folder), then run run_friday.bat again.")
        return 1

    mode = "text" if args.text else "ptt" if args.ptt else "wake" if args.wake else "always" if args.always else None
    try:
        assistant = Assistant(settings, mode, start_dashboard=False if args.no_dashboard else None)
    except ModuleNotFoundError as e:
        print(f"\nFriday is missing a part it needs: '{e.name}'.\n"
              "Fix: double-click repair_friday.bat in the Friday folder (needs internet), then start Friday again.")
        return 1
    assistant.ui.banner([f"Brain: {settings.provider} / {settings.model}",
                         f"Mode: {assistant.mode} | Wake words: {', '.join(settings.wake_words)}",
                         f"Dashboard: {assistant.dashboard_url or 'off'}",
                         "Type /help for commands, /quit to exit."])
    if problems:
        assistant.ui.set_status("error", "Setup needed: " + " ".join(problems) + " Open the dashboard > Settings.")
    if assistant.dashboard_url and settings.dashboard_open_browser and not args.no_browser:
        assistant.open_dashboard()
    try:
        from . import activity, runlog

        note = runlog.start(settings.data_dir)
        if note:
            activity.log("error", note)
            assistant.ui.info(note)
    except Exception:
        runlog = None
    try:
        assistant.run()
    except KeyboardInterrupt:
        if runlog:
            runlog.mark_clean("Ctrl+C in the console")
    if runlog:
        runlog.mark_clean("normal shutdown") if not runlog._STATE.get("clean") else None
    print("Friday shut down.")
    os._exit(0)  # make sure background threads (tray, hotkeys, timers, dashboard) end too
