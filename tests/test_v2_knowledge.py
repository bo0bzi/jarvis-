"""v2.0: knowledge base (manuals) - local only, fake embeddings (no Ollama needed)."""
import re

import pytest

from friday import knowledge
from friday import tools as T

VOCAB = ["torque", "brake", "caliper", "valve", "clearance", "chain", "slack", "oil", "spark", "plug", "coolant"]


def fake_embed(texts):
    out = []
    for t in texts:
        words = re.findall(r"[a-z]+", t.lower())
        out.append([float(words.count(v)) + 0.01 for v in VOCAB])
    return out


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    d = tmp_path / "knowledge"
    d.mkdir()
    monkeypatch.setenv("KNOWLEDGE_DIR", str(d))
    (d / "README.txt").write_text("ignore me " * 20)
    (d / "zx6r.txt").write_text("Front brake caliper bolt torque is 25 N-m. Check the brake caliper pads every service. " * 3)
    (d / "ducati.md").write_text("Valve clearance intake 0.10-0.15 mm. Valve clearance exhaust 0.20-0.25 mm. " * 3)
    return d


def test_semantic_search_and_reindex(kdir):
    rep = knowledge.update_index(embed_fn=fake_embed)
    assert "2 document(s)" in rep and "README" not in rep and "keyword" not in rep
    hits = knowledge.search("what is the valve clearance", embed_fn=fake_embed)
    assert hits[0]["doc"] == "ducati.md" and hits[0]["page"] == 1
    assert knowledge.search("caliper torque", embed_fn=fake_embed)[0]["doc"] == "zx6r.txt"
    assert "No changes" in knowledge.update_index(embed_fn=fake_embed)
    (kdir / "ducati.md").unlink()
    knowledge.update_index(embed_fn=fake_embed)
    assert all(h["doc"] != "ducati.md" for h in knowledge.search("valve clearance", embed_fn=fake_embed))
    assert knowledge.search("valve clearance", embed_fn=lambda t: None) == []  # keyword rows removed too


def test_keyword_fallback_without_embeddings(kdir):
    rep = knowledge.update_index(embed_fn=lambda t: None)
    assert "keyword search" in rep
    hits = knowledge.search("caliper", embed_fn=lambda t: None)
    assert hits and hits[0]["doc"] == "zx6r.txt" and "25 N-m" in hits[0]["text"]


def test_changed_file_replaces_old_text(kdir):
    knowledge.update_index(embed_fn=lambda t: None)
    (kdir / "zx6r.txt").write_text("Chain slack should be 25-35 mm measured at the middle of the lower run. " * 3)
    knowledge.update_index(embed_fn=lambda t: None)
    assert knowledge.search("caliper", embed_fn=lambda t: None) == []
    assert knowledge.search("chain slack", embed_fn=lambda t: None)


def test_tools(kdir, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "embed", lambda t: None)
    ctx = T.ToolContext()
    out = T.dispatch("search_manuals", {"query": "caliper torque"}, ctx)
    assert "zx6r.txt" in out and "25 N-m" in out
    extra = tmp_path / "coolant.txt"
    extra.write_text("Coolant capacity is 2.1 litres, use 50/50 ethylene glycol mix for the coolant. " * 2)
    assert "coolant.txt" in T.dispatch("add_to_knowledge", {"path": str(extra)}, ctx)
    assert "coolant.txt" in T.dispatch("search_manuals", {"query": "coolant capacity"}, ctx)
    assert "Error" in T.dispatch("add_to_knowledge", {"path": str(tmp_path / "nope.exe")}, ctx)
