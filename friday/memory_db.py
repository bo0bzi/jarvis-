"""Searchable memory: everything said, every note, fact and shop job goes into a local SQLite full-text index
(data/friday.db), so "what did that customer say about his Ducati last month?" can be found. Nothing leaves the PC."""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .env import data_dir
from .tools import tool

_LOCK = threading.RLock()


def db_path(base: Path | None = None) -> Path:
    return Path(base or data_dir()) / "friday.db"


def connect(base: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path(base)), timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(kind, text, time UNINDEXED, ref UNINDEXED)")
    return con


def index(kind: str, text: str, ref: str = "", base: Path | None = None) -> None:
    text = (text or "").strip()
    if not text:
        return
    with _LOCK:
        try:
            con = connect(base)
            with con:
                if ref:  # one row per ref (e.g. a job) - replace older text
                    con.execute("DELETE FROM mem WHERE kind=? AND ref=?", (kind, ref))
                con.execute("INSERT INTO mem(kind, text, time, ref) VALUES (?,?,?,?)",
                            (kind, text[:4000], datetime.now().isoformat(timespec="seconds"), ref))
            con.close()
        except sqlite3.Error as e:
            print(f"[memory] index failed: {e}")


def forget(kind: str, ref_prefix: str, base: Path | None = None) -> None:
    """Remove rows of one kind whose ref starts with ref_prefix (e.g. an old manual)."""
    with _LOCK:
        try:
            con = connect(base)
            with con:
                con.execute("DELETE FROM mem WHERE kind=? AND ref LIKE ?", (kind, ref_prefix + "%"))
            con.close()
        except sqlite3.Error as e:
            print(f"[memory] forget failed: {e}")


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w']+", q.lower()) if len(w) > 1]
    return " OR ".join(f'"{w}"*' if len(w) > 2 else f'"{w}"' for w in words[:12])


def search(query: str, limit: int = 8, kind: str = "", base: Path | None = None) -> list[dict]:
    fq = _fts_query(query)
    if not fq:
        return []
    with _LOCK:
        con = connect(base)
        try:
            sql = "SELECT kind, text, time, ref FROM mem WHERE mem MATCH ?" + (" AND kind = ?" if kind else "")
            rows = con.execute(sql + " ORDER BY bm25(mem) LIMIT ?",
                               (fq, kind, limit) if kind else (fq, limit)).fetchall()
        except sqlite3.Error:
            rows = []
        con.close()
    return [dict(r) for r in rows]


def import_existing(base: Path | None = None) -> int:
    """First run: index the old notes file so it's searchable too."""
    base = Path(base or data_dir())
    marker = base / ".memory_indexed"
    if marker.exists():
        return 0
    n = 0
    notes = base / "notes.txt"
    if notes.exists():
        for line in notes.read_text(encoding="utf-8", errors="replace").splitlines():
            index("note", line, base=base)
            n += 1
    marker.write_text("1", encoding="utf-8")
    return n


@tool("search_memory", "Search everything Friday remembers: past conversations (voice and text messages), notes, "
      "facts and shop jobs. Use it when the user asks what was said / noted / done before.",
      {"type": "object", "properties": {"query": {"type": "string"},
                                        "kind": {"type": "string", "description": "optional: chat, note, fact, job"}},
       "required": ["query"]})
def search_memory(ctx, query: str, kind: str = ""):
    base = getattr(getattr(ctx, "settings", None), "data_dir", None)
    import_existing(base)
    hits = search(query, kind=kind, base=base)
    if not hits:
        return f"I found nothing in memory about '{query}'."
    return "\n".join(f"[{h['time'][:16].replace('T', ' ')}] ({h['kind']}) {h['text'][:300]}" for h in hits)
