"""Knowledge base: service manuals and documents you put in the knowledge folder (PDF, TXT, MD).
Friday splits them into pieces, makes local embeddings with Ollama (EMBED_MODEL, default nomic-embed-text) and
searches them by meaning. If the embedding model isn't installed it still works with keyword search.
Everything stays on this PC."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import urllib.request
from pathlib import Path

from . import activity
from .env import data_dir, env, ollama_url
from .memory_db import forget as fts_forget, index as fts_index, search as fts_search
from .tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXTS = {".pdf", ".txt", ".md"}
CHUNK = 1200
_LOCK = threading.RLock()


def knowledge_dir() -> Path:
    d = Path(env("KNOWLEDGE_DIR") or PROJECT_ROOT / "knowledge")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store() -> Path:
    d = data_dir() / "knowledge_index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_document(path: Path) -> list[tuple[int, str]]:
    """[(page, text)]"""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [(i + 1, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]
    return [(1, path.read_text(encoding="utf-8", errors="replace"))]


def chunks(pages: list[tuple[int, str]], size: int = CHUNK) -> list[tuple[int, str]]:
    out = []
    for page, text in pages:
        text = re.sub(r"[ \t]+", " ", text).strip()
        while text:
            cut = text[:size]
            if len(text) > size:
                k = max(cut.rfind("\n"), cut.rfind(". "))
                cut = text[:k + 1] if k > size // 2 else cut
            out.append((page, cut.strip()))
            text = text[len(cut):].strip()
    return [c for c in out if len(c[1]) > 30]


def embed(texts: list[str]) -> list[list[float]] | None:
    """Local embeddings via Ollama. None if Ollama / the model isn't available."""
    model = env("EMBED_MODEL", "nomic-embed-text")
    try:
        req = urllib.request.Request(ollama_url() + "/api/embed", data=json.dumps({"model": model, "input": texts}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            vecs = json.loads(r.read()).get("embeddings")
        return vecs if vecs and len(vecs) == len(texts) else None
    except Exception:
        return None


def _manifest() -> dict:
    try:
        return json.loads((_store() / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_index(embed_fn=embed) -> str:
    """Index new or changed documents. Returns a short report."""
    import numpy as np

    with _LOCK:
        man = _manifest()
        files = {str(p.relative_to(knowledge_dir())): p for p in knowledge_dir().rglob("*")
                 if p.is_file() and p.suffix.lower() in EXTS and p.name != "README.txt"}
        added, failed, semantic = [], [], True
        for rel, p in files.items():
            digest = hashlib.sha1(p.read_bytes()).hexdigest()
            if man.get(rel, {}).get("sha1") == digest:
                continue
            try:
                parts = chunks(read_document(p))
            except Exception as e:
                failed.append(f"{rel} ({type(e).__name__})")
                continue
            if not parts:
                failed.append(f"{rel} (no text - maybe a scanned PDF)")
                continue
            vecs = []
            for i in range(0, len(parts), 32):
                v = embed_fn([t for _, t in parts[i:i + 32]])
                if v is None:
                    vecs = None
                    break
                vecs.extend(v)
            key = hashlib.sha1(rel.encode()).hexdigest()[:12]
            (_store() / f"{key}.json").write_text(json.dumps([{"page": pg, "text": t} for pg, t in parts]), encoding="utf-8")
            if vecs:
                arr = np.asarray(vecs, dtype="float32")
                arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
                np.save(_store() / f"{key}.npy", arr)
            else:
                semantic = False
                (_store() / f"{key}.npy").unlink(missing_ok=True)
            fts_forget("manual", key + ":")
            for i, (pg, t) in enumerate(parts):
                fts_index("manual", f"{rel} p.{pg}: {t}", ref=f"{key}:{i}")
            man[rel] = {"sha1": digest, "key": key, "chunks": len(parts), "semantic": bool(vecs)}
            added.append(f"{rel} ({len(parts)} pieces)")
        for rel in [r for r in man if r not in files]:
            key = man.pop(rel)["key"]
            fts_forget("manual", key + ":")
            for ext in (".json", ".npy"):
                (_store() / f"{key}{ext}").unlink(missing_ok=True)
        (_store() / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    report = (f"Knowledge base: {len(man)} document(s). " + (f"New/updated: {', '.join(added)}. " if added else "No changes. ")
              + (f"Couldn't read: {', '.join(failed)}. " if failed else "")
              + ("" if semantic else "Embedding model not available - using keyword search (run setup_local_ai).")).strip()
    activity.log("knowledge", report)
    return report


def search(query: str, k: int = 4, embed_fn=embed) -> list[dict]:
    import numpy as np

    man = _manifest()
    hits = []
    q = embed_fn([query]) if any(m.get("semantic") for m in man.values()) else None
    if q:
        qv = np.asarray(q[0], dtype="float32")
        qv /= np.linalg.norm(qv) + 1e-9
        for rel, m in man.items():
            npy = _store() / f"{m['key']}.npy"
            if not m.get("semantic") or not npy.exists():
                continue
            arr = np.load(npy)
            texts = json.loads((_store() / f"{m['key']}.json").read_text(encoding="utf-8"))
            scores = arr @ qv
            for i in np.argsort(-scores)[:k]:
                hits.append({"doc": rel, "page": texts[i]["page"], "text": texts[i]["text"], "score": float(scores[i])})
        hits.sort(key=lambda h: -h["score"])
        hits = hits[:k]
    if not hits:  # keyword fallback
        for h in fts_search(query, limit=k, kind="manual"):
            doc, _, rest = h["text"].partition(": ")
            hits.append({"doc": doc.rsplit(" p.", 1)[0], "page": doc.rsplit(" p.", 1)[-1], "text": rest, "score": 0})
    return hits


@tool("search_manuals", "Search the shop's service manuals and documents (knowledge folder) for specs, torque values, "
      "procedures, part numbers, wiring. Answer from the returned text and say which manual/page it came from.",
      {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
def search_manuals(ctx, query: str):
    if not _manifest():
        update_index()
    hits = search(query)
    if not hits:
        return "Nothing found in the manuals. (Put PDF manuals in the knowledge folder, then say 'update the knowledge base'.)"
    return "\n\n".join(f"[{h['doc']}, page {h['page']}] {h['text'][:900]}" for h in hits)


@tool("update_knowledge_base", "Read any new or changed manuals in the knowledge folder so they can be searched.")
def update_knowledge_base(ctx):
    return update_index()


@tool("add_to_knowledge", "Copy a PDF/TXT file into the knowledge base and index it.",
      {"type": "object", "properties": {"path": {"type": "string", "description": "full path of the file"}},
       "required": ["path"]})
def add_to_knowledge(ctx, path: str):
    src = Path(path.strip().strip('"'))
    if not src.is_file() or src.suffix.lower() not in EXTS:
        return f"Error: '{path}' isn't a PDF, TXT or MD file I can find."
    shutil.copy2(src, knowledge_dir() / src.name)
    return update_index()
