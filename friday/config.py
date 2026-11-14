"""Configuration loading from the .env file (never hardcode keys)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"  # tests may point this elsewhere

PLACEHOLDER_MARKERS = ("your-", "xxxx", "changeme", "placeholder", "example.com")
_PLACEHOLDER_EXACT = {"here", "your-key", "your-key-here", "key-here", "api-key-here", "none", "null"}
INPUT_MODES = ("always", "wake", "ptt", "text")
PROVIDERS = ("abacus", "openai", "anthropic", "custom")
DEFAULT_ESCALATE_PHRASES = "think hard,big brain,use the big brain,ask abacus,use abacus,go deep"


def _load_dotenv(path: Path, override: bool = False) -> None:
    """Load KEY=VALUE pairs into os.environ (python-dotenv if present, else a tiny parser).
    override=True is used when the dashboard saves new settings and they must replace the old ones."""
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=override)
        return
    except ImportError:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key.strip()] = value
        else:
            os.environ.setdefault(key.strip(), value)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    val = _get(name, str(default)).lower()
    return val in ("1", "true", "yes", "on", "y")


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def is_placeholder(key: str) -> bool:
    """True for an unset or template value ('.env.example' text), not for a real key that merely contains 'here'."""
    k = (key or "").strip().lower()
    if not k or k in _PLACEHOLDER_EXACT:
        return True
    return any(m in k for m in PLACEHOLDER_MARKERS)


@dataclass
class Settings:
    provider: str = "abacus"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    tool_mode: str = "auto"
    assistant_name: str = "Friday"
    user_name: str = ""
    wake_words: list[str] = field(default_factory=lambda: ["hey friday", "friday", "jarvis"])
    input_mode: str = "always"
    ptt_hotkey: str = ""
    followup_seconds: float = 20.0
    conversation_mode: bool = False
    mute_hotkey: str = ""
    stop_hotkey: str = ""
    barge_in: bool = True        # talk over Jarvis to interrupt it
    barge_in_level: float = 0.06  # how loud you must be to interrupt (0.02-0.3); echo of its own voice is auto-ignored
    stt_engine: str = "whisper"
    whisper_model: str = "base.en"
    mic_device: str = ""
    energy_threshold: float = 0.0
    silence_seconds: float = 1.0
    max_record_seconds: float = 15.0
    tts_engine: str = "edge"
    edge_voice: str = "en-GB-RyanNeural"
    edge_rate: str = "+5%"
    pyttsx3_rate: int = 185
    default_city: str = "Toronto"
    tray_icon: bool = True
    memory_max_messages: int = 40
    allow_shell: bool = False
    disabled_tools: list[str] = field(default_factory=list)
    dashboard: bool = True
    dashboard_port: int = 8765
    dashboard_open_browser: bool = True
    auto_check_minutes: float = 10.0
    data_dir: Path = PROJECT_ROOT / "data"
    # Backup ("big") brain: used only when the main (local/free) brain fails, when you ask for it,
    # or (optionally) when the main brain says the request is too hard. Capped per day.
    backup_provider: str = "none"
    backup_api_key: str = ""
    backup_base_url: str = ""
    backup_model: str = ""
    backup_max_per_day: int = 25
    # Free cloud tier (OmniRoute): tried BEFORE the paid backup when the local AI can't handle something.
    free_router_url: str = "http://localhost:20128/v1"
    free_router_key: str = ""
    free_router_model: str = "auto/cheap"
    free_router_max_per_day: int = 300
    escalate_on_error: bool = True
    self_escalate: bool = True
    escalate_phrases: list[str] = field(default_factory=lambda: DEFAULT_ESCALATE_PHRASES.split(","))

    @property
    def has_backup(self) -> bool:
        return self.backup_provider in PROVIDERS

    @property
    def has_free_router(self) -> bool:
        return bool(self.free_router_url and self.free_router_key and not is_placeholder(self.free_router_key))

    def free_router_settings(self) -> "Settings":
        """A copy of these settings that talks to the free cloud router (OmniRoute) instead."""
        return replace(self, provider="custom", api_key=self.free_router_key, base_url=self.free_router_url,
                       model=self.free_router_model, backup_provider="none", free_router_key="")

    def backup_settings(self) -> "Settings":
        """A copy of these settings that talks to the backup provider instead."""
        return replace(self, provider=self.backup_provider, api_key=self.backup_api_key,
                       base_url=self.backup_base_url, model=self.backup_model, backup_provider="none",
                       free_router_key="")  # the backup must never escalate onward to the free tier

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty = OK)."""
        problems = []
        if self.provider not in PROVIDERS:
            problems.append(f"LLM_PROVIDER '{self.provider}' is not one of abacus/openai/anthropic/custom.")
        if self.provider != "custom" and is_placeholder(self.api_key):
            problems.append(
                f"No API key set for provider '{self.provider}'. Open the .env file and paste your key "
                "(Abacus key: https://abacus.ai/app/route-llm-apis)."
            )
        if self.has_backup and self.backup_provider == self.provider and self.backup_model == self.model:
            problems.append("BACKUP_PROVIDER is the same as the main AI, so the backup would never help.")
        if self.has_backup and self.backup_provider != "custom" and is_placeholder(self.backup_api_key):
            problems.append(f"Backup AI '{self.backup_provider}' has no API key; the backup will not work.")
        if self.input_mode not in INPUT_MODES:
            problems.append(f"INPUT_MODE '{self.input_mode}' must be one of: {', '.join(INPUT_MODES)}.")
        return problems


def provider_credentials(provider: str) -> tuple[str, str, str]:
    """(api_key, base_url, model) for a provider, read from the environment / .env."""
    if provider == "openai":
        return _get("OPENAI_API_KEY"), _get("OPENAI_BASE_URL", "https://api.openai.com/v1"), _get("OPENAI_MODEL", "gpt-4o-mini")
    if provider == "anthropic":
        return _get("ANTHROPIC_API_KEY"), "", _get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    if provider == "custom":
        # Defaults match Ollama running on this PC.
        return (_get("CUSTOM_API_KEY", "not-needed") or "not-needed", _get("CUSTOM_BASE_URL", "http://localhost:11434/v1"),
                _get("CUSTOM_MODEL", "qwen2.5:7b"))
    return _get("ABACUS_API_KEY"), _get("ABACUS_BASE_URL", "https://routellm.abacus.ai/v1"), _get("ABACUS_MODEL", "route-llm")


def load_settings(env_path: Path | None = None, override: bool = False) -> Settings:
    _load_dotenv(env_path or ENV_PATH, override=override)
    provider = _get("LLM_PROVIDER", "abacus").lower()
    api_key, base_url, model = provider_credentials(provider)
    backup = _get("BACKUP_PROVIDER", "none").lower()
    b_key, b_url, b_model = provider_credentials(backup) if backup in PROVIDERS else ("", "", "")

    wake = [w.strip().lower() for w in _get("WAKE_WORDS", "hey friday,friday,jarvis").split(",") if w.strip()]
    data_dir = Path(_get("DATA_DIR") or PROJECT_ROOT / "data")
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        tool_mode=_get("TOOL_MODE", "auto").lower(),
        assistant_name=_get("ASSISTANT_NAME", "Friday"),
        user_name=_get("USER_NAME", ""),
        wake_words=wake,
        input_mode=_get("INPUT_MODE", "always").lower(),
        ptt_hotkey=_get("PTT_HOTKEY", ""),
        followup_seconds=_float("FOLLOWUP_SECONDS", 20.0),
        conversation_mode=_bool("CONVERSATION_MODE", False),
        mute_hotkey=_get("MUTE_HOTKEY", ""),
        stop_hotkey=_get("STOP_HOTKEY", ""),
        barge_in=_bool("BARGE_IN", True),
        barge_in_level=_float("BARGE_IN_LEVEL", 0.06),
        stt_engine=_get("STT_ENGINE", "whisper").lower(),
        whisper_model=_get("WHISPER_MODEL", "base.en"),
        mic_device=_get("MIC_DEVICE", ""),
        energy_threshold=_float("ENERGY_THRESHOLD", 0.0),
        silence_seconds=_float("SILENCE_SECONDS", 1.0),
        max_record_seconds=_float("MAX_RECORD_SECONDS", 15.0),
        tts_engine=_get("TTS_ENGINE", "edge").lower(),
        edge_voice=_get("EDGE_VOICE", "en-GB-RyanNeural"),
        edge_rate=_get("EDGE_RATE", "+5%"),
        pyttsx3_rate=_int("PYTTSX3_RATE", 185),
        default_city=_get("DEFAULT_CITY", "Toronto"),
        tray_icon=_bool("TRAY_ICON", True),
        memory_max_messages=_int("MEMORY_MAX_MESSAGES", 40),
        allow_shell=_bool("ALLOW_SHELL", False),
        disabled_tools=[t.strip() for t in _get("DISABLED_TOOLS", "").split(",") if t.strip()],
        dashboard=_bool("DASHBOARD", True),
        dashboard_port=_int("DASHBOARD_PORT", 8765),
        dashboard_open_browser=_bool("DASHBOARD_OPEN_BROWSER", True),
        auto_check_minutes=_float("AUTO_CHECK_MINUTES", 10.0),
        data_dir=data_dir,
        backup_provider=backup if backup in PROVIDERS else "none",
        backup_api_key=b_key,
        backup_base_url=b_url,
        backup_model=b_model,
        backup_max_per_day=_int("BACKUP_MAX_PER_DAY", 25),
        free_router_url=_get("FREE_ROUTER_URL", "http://localhost:20128/v1"),
        free_router_key=_get("FREE_ROUTER_KEY", ""),
        free_router_model=_get("FREE_ROUTER_MODEL", "auto/cheap"),
        free_router_max_per_day=_int("FREE_ROUTER_MAX_PER_DAY", 300),
        escalate_on_error=_bool("ESCALATE_ON_ERROR", True),
        self_escalate=_bool("SELF_ESCALATE", True),
        escalate_phrases=[p.strip().lower() for p in _get("ESCALATE_PHRASES", DEFAULT_ESCALATE_PHRASES).split(",") if p.strip()],
    )
