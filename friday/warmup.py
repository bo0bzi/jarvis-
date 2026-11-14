"""Load the local AI model into memory at startup and keep it there.

A big local model takes 30-90 seconds to load the first time. Without this, the first message after starting
Jarvis (or after 5 idle minutes, Ollama's default) was slow, and a slow first answer used to make Jarvis give up and
call the paid cloud backup. Free: it only asks Ollama to load the model and stay loaded.
Turn off with WARMUP=false. How long to stay loaded: OLLAMA_KEEP_ALIVE (default 24h)."""
from __future__ import annotations

import json
import time
import urllib.request

from .env import env, env_bool, is_local_url, ollama_url


def warm_up(settings, log=print) -> bool:
    if not env_bool("WARMUP", True):
        return False
    if getattr(settings, "provider", "") != "custom" or not is_local_url(getattr(settings, "base_url", "")):
        return False  # cloud brains have nothing to load
    model = settings.model
    body = {"model": model, "prompt": "", "stream": False, "keep_alive": env("OLLAMA_KEEP_ALIVE", "24h")}
    req = urllib.request.Request(ollama_url() + "/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
    except Exception as e:  # Ollama not running yet, or model missing: Jarvis still works, just slower at first
        log(f"[warmup] couldn't preload {model}: {type(e).__name__}: {str(e)[:100]}")
        return False
    log(f"[warmup] {model} is loaded and ready ({time.perf_counter() - t:.0f}s)")
    return True
