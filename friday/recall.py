"""Semantic recall: instead of pasting EVERY remembered fact into every prompt (slow and distracting as the list
grows), pick only the few facts that match what the user just said, by meaning (local Ollama embeddings) with a
keyword fallback when the embedding model isn't available. Nothing leaves the PC."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

ALWAYS_ALL = 10      # up to this many facts: just include them all (cheap, and keeps the prompt stable)
TOP_K = 8
_STOP = {"the", "and", "for", "you", "your", "are", "was", "that", "this", "with", "what", "have", "has", "can",
         "how", "about", "from", "when", "where", "who", "why", "please", "tell", "give", "show", "does", "did"}

_VEC_CACHE: dict[str, list[float]] = {}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", (text or "").lower()) if w not in _STOP}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _load_cache(path: Path | None) -> None:
    if path and not _VEC_CACHE and path.exists():
        try:
            _VEC_CACHE.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass


def _save_cache(path: Path | None) -> None:
    if path:
        try:
            path.write_text(json.dumps(_VEC_CACHE), encoding="utf-8")
        except OSError:
            pass


def _keyword_rank(facts: list[str], query: str, k: int) -> list[str]:
    q = _words(query)
    scored = [(len(q & _words(f)), i, f) for i, f in enumerate(facts)]
    hits = [s for s in scored if s[0] > 0]
    hits.sort(key=lambda s: (-s[0], -s[1]))  # more shared words first, then newer facts
    chosen = [f for _, _, f in hits[:k]]
    if len(chosen) < k:  # pad with the newest facts so something useful is always there
        for f in reversed(facts):
            if f not in chosen:
                chosen.append(f)
            if len(chosen) >= min(k, 3):
                break
    return chosen


def relevant_facts(facts: list[str], query: str, k: int = TOP_K, embed_fn=None, cache_path: Path | None = None) -> list[str]:
    """The facts worth showing for this message. Few facts -> all of them. Order follows the original list."""
    if len(facts) <= ALWAYS_ALL or not (query or "").strip():
        return list(facts[-ALWAYS_ALL:]) if len(facts) > ALWAYS_ALL else list(facts)
    if embed_fn is None:
        from .knowledge import embed as embed_fn  # noqa: PLC0415
    chosen: list[str] | None = None
    try:
        _load_cache(cache_path)
        missing = [f for f in facts if f not in _VEC_CACHE]
        for i in range(0, len(missing), 32):
            vecs = embed_fn(missing[i:i + 32])
            if not vecs:
                raise RuntimeError("no embeddings")
            _VEC_CACHE.update(dict(zip(missing[i:i + 32], vecs, strict=False)))
        if missing:
            _save_cache(cache_path)
        qv = embed_fn([query])
        if qv:
            ranked = sorted(facts, key=lambda f: _cosine(_VEC_CACHE[f], qv[0]), reverse=True)
            chosen = ranked[:k]
    except Exception:
        chosen = None
    if chosen is None:
        chosen = _keyword_rank(facts, query, k)
    keep = set(chosen)
    return [f for f in facts if f in keep]  # original order, no duplicates


def prompt_section(memory, query: str) -> str:
    if memory is None or not memory.facts:
        return ""
    facts = relevant_facts(memory.facts, query, cache_path=Path(memory.path).parent / "fact_vectors.json")
    note = "" if len(facts) == len(memory.facts) else f" (the {len(facts)} most relevant of {len(memory.facts)})"
    return f"\nThings you know about the user{note}:\n" + "\n".join(f"- {f}" for f in facts)
