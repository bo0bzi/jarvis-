"""Phase 1 defaults: backups must not carry keys, the SMS PIN must be compared safely, and shell is opt-in."""
from __future__ import annotations

import hmac
import zipfile

import pytest

from friday import backup, config, sms
from friday.config import load_settings


# ---------------------------------------------------------------- 6. backups exclude .env by default
@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    from friday import skills

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("ABACUS_API_KEY=secret-key-value\n")
    monkeypatch.setattr(skills, "skills_dir", lambda: tmp_path / "skills")
    (tmp_path / "skills").mkdir(exist_ok=True)
    monkeypatch.delenv("BACKUP_INCLUDE_ENV", raising=False)
    return tmp_path


def test_backup_leaves_keys_out_by_default(dirs):
    (dirs / "data" / "notes.txt").write_text("a note\n")
    p = backup.make_backup()
    names = zipfile.ZipFile(p).namelist()
    assert "data/notes.txt" in names
    assert ".env" not in names, "a backup zip must not carry the API keys unless asked"


def test_backup_includes_keys_only_when_asked(dirs):
    p = backup.make_backup(include_env=True)
    assert ".env" in zipfile.ZipFile(p).namelist()


def test_backup_env_can_still_be_enabled_from_settings(dirs, monkeypatch):
    monkeypatch.setenv("BACKUP_INCLUDE_ENV", "true")
    p = backup.make_backup()
    assert ".env" in zipfile.ZipFile(p).namelist()


def test_the_dashboard_switch_defaults_to_off():
    from friday.settings_store import SCHEMA_BY_KEY

    assert SCHEMA_BY_KEY["BACKUP_INCLUDE_ENV"]["default"] == "false"


# ---------------------------------------------------------------- 9. SMS PIN
class FakeLink(sms.SmsLink):
    """A link with no network: records what it would text and answers with a canned reply."""

    def __init__(self, pin: str = ""):
        super().__init__()
        self.sent: list[str] = []
        self.reply: str | None = None

    def configured(self) -> bool:
        return True

    def text_owner(self, text: str) -> bool:
        self.sent.append(text)
        return True


@pytest.fixture()
def link(monkeypatch):
    monkeypatch.setenv("OWNER_PHONE", "+15550001111")
    monkeypatch.setenv("QUO_API_KEY", "test-key")
    monkeypatch.delenv("SMS_PIN", raising=False)
    return FakeLink()


def _incoming(text: str) -> dict:
    return {"from": "+15550001111", "text": text}


def _pending(link):
    import threading

    link.pending = {"prompt": "delete the file?", "answer": None, "event": threading.Event()}
    return link.pending


def test_yes_without_a_pin_configured_is_refused(link):
    p = _pending(link)
    out = link.handle_incoming(_incoming("YES"))
    assert "PIN" in out and p["answer"] is None, "an empty PIN must never approve anything"


def test_yes_with_the_wrong_pin_is_refused(link, monkeypatch):
    monkeypatch.setenv("SMS_PIN", "1234")
    p = _pending(link)
    out = link.handle_incoming(_incoming("YES 9999"))
    assert "Wrong or missing PIN" in out
    assert p["answer"] is None


def test_yes_with_the_right_pin_approves(link, monkeypatch):
    monkeypatch.setenv("SMS_PIN", "1234")
    p = _pending(link)
    assert link.handle_incoming(_incoming("YES 1234")) == "Approved."
    assert p["answer"] is True


def test_no_cancels_without_a_pin(link):
    p = _pending(link)
    assert link.handle_incoming(_incoming("NO")) == "Okay, cancelled."
    assert p["answer"] is False


def test_the_pin_is_compared_in_constant_time(link, monkeypatch):
    monkeypatch.setenv("SMS_PIN", "1234")
    calls: list[tuple] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(sms.hmac, "compare_digest", spy)
    _pending(link)
    link.handle_incoming(_incoming("YES 1234"))
    assert calls == [("1234", "1234")], "the PIN must go through hmac.compare_digest"


def test_the_approval_text_asks_for_a_pin_when_none_is_set(link):
    link.text_owner("Friday needs your OK: x\nReply YES <PIN - set SMS_PIN in .env first> to approve or NO to cancel.")
    assert "SMS_PIN" in link.sent[-1]


# ---------------------------------------------------------------- 10. shell is opt-in
def test_shell_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("ABACUS_API_KEY=k\n")
    monkeypatch.delenv("ALLOW_SHELL", raising=False)
    assert load_settings(override=True).allow_shell is False


def test_shell_can_be_enabled_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("ABACUS_API_KEY=k\nALLOW_SHELL=true\n")
    monkeypatch.delenv("ALLOW_SHELL", raising=False)
    assert load_settings(override=True).allow_shell is True


def test_run_shell_command_refuses_when_off(tmp_path, monkeypatch):
    from friday import tools as T

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = config.Settings(allow_shell=False)
    ctx = T.ToolContext(settings=s, confirm=lambda p: True)
    out = T.dispatch("run_shell_command", {"command": "echo hi"}, ctx)
    assert "disabled" in out
