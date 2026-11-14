"""Phase 0 regressions: the four small bugs that were live in the code.

1. open_app built a shell command out of model text (command injection).
2. _quote/_unquote did not round-trip a value containing a quote or a backslash.
3. set_reminder treated in_minutes=0 as "not given" and fell through to at_time.
4. is_placeholder rejected a real key that merely contained the word "here"."""
from __future__ import annotations

import inspect

import pytest

from friday import tools
from friday.config import is_placeholder
from friday.settings_store import _quote, _unquote
from friday.tools import ToolContext, _launch_target_ok, open_app, set_reminder


# ---------------------------------------------------------------- 1. open_app injection
@pytest.mark.parametrize("bad", ['x" & calc & "', "a|b", "a&b", "a>b", "a<b", "a;b", "a%b", "a^b", "$(calc)",
                                 "`calc`", "a\nb", "..\\evil", ""])
def test_launch_target_rejects_shell_metacharacters(bad):
    assert _launch_target_ok(bad) is False


@pytest.mark.parametrize("good", ["notepad", "chrome", "spotify:", "ms-settings:", "C:\\Program Files\\App\\a.exe",
                                  "winword", "code insiders"])
def test_launch_target_allows_real_apps(good):
    assert _launch_target_ok(good) is True


def test_open_app_refuses_an_injected_name_without_launching(monkeypatch):
    launched: list = []
    monkeypatch.setattr(tools.os, "startfile", lambda t: launched.append(t), raising=False)
    monkeypatch.setattr(tools.subprocess, "Popen", lambda *a, **k: launched.append(a))
    out = open_app(None, 'x" & calc & "')
    assert "won't open" in out
    assert launched == [], "nothing may be launched for a name with shell metacharacters"


def test_open_app_never_uses_a_shell():
    src = inspect.getsource(open_app)
    assert "shell=True" not in src, "open_app must not hand model text to a shell"
    assert 'f\'start ""' not in src


# ---------------------------------------------------------------- 2. settings round-trip
@pytest.mark.parametrize("value", ['abc', 'a b', 'a#b', 'a"b', "+5%", ' x ', 'a\\b', 'C:\\Users\\ibbi',
                                   'say "hi" #1', 'en-GB-RyanNeural', "it's", 'a\\\\b'])
def test_settings_values_survive_a_save_and_reload(value):
    assert _unquote(_quote(value)) == value


def test_unquote_still_handles_plain_and_commented_lines():
    assert _unquote("plain") == "plain"
    assert _unquote("value # a comment") == "value"
    assert _unquote('"quoted # not a comment"') == "quoted # not a comment"


# ---------------------------------------------------------------- 3. set_reminder zero minutes
@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c = ToolContext(settings=None)
    c.timers.clear()
    yield c
    for t in list(c.timers):
        t["timer"].cancel()
    c.timers.clear()


def test_zero_minutes_is_not_silently_ignored(ctx):
    out = set_reminder(ctx, "stretch", in_minutes=0)
    assert "Tell me when" in out
    assert ctx.timers == [], "a zero-minute reminder must not be scheduled"


def test_positive_minutes_still_schedules(ctx):
    out = set_reminder(ctx, "stretch", in_minutes=5)
    assert "remind you" in out
    assert len(ctx.timers) == 1


def test_at_time_still_works(ctx):
    out = set_reminder(ctx, "call", at_time="17:30")
    assert "remind you" in out
    assert len(ctx.timers) == 1


# ---------------------------------------------------------------- 4. is_placeholder
def test_a_real_key_containing_here_is_not_a_placeholder():
    assert is_placeholder("sk-real-key-here") is False
    assert is_placeholder("sk-ant-api03-abc") is False
    assert is_placeholder("s2_abcdef") is False


def test_template_values_are_still_placeholders():
    for template in ("", "   ", "your-abacus-routellm-key-here", "xxxx", "here", "sk-xxxx", "changeme",
                     "your-openai-key"):
        assert is_placeholder(template) is True, template
