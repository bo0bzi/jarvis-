"""Tiny helpers to read live settings from the environment (.env is loaded into it by config.load_settings,
and reloaded when the dashboard saves settings - so these values change without a restart)."""
from __future__ import annotations

import os
from pathlib import Path


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_int(key: str, default: int) -> int:
    try:
        return int(float(env(key, str(default))))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool) -> bool:
    v = env(key, "").lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on", "y")


def env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


def data_dir() -> Path:
    from .config import PROJECT_ROOT

    d = Path(env("DATA_DIR") or PROJECT_ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_local_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("localhost", "127.0.0.1", "[::1]", "://0.0.0.0", "://192.168.", "://10.",
                                "host.docker.internal"))


def is_cloud(settings) -> bool:
    """True when this brain costs credits / runs online (anything that isn't a local server)."""
    return not (getattr(settings, "provider", "") == "custom" and is_local_url(getattr(settings, "base_url", "")))


def ollama_url() -> str:
    """Base URL of the local Ollama server (without /v1)."""
    base = env("OLLAMA_URL") or env("CUSTOM_BASE_URL") or "http://localhost:11434"
    base = base.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base
