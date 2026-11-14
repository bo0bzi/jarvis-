from types import SimpleNamespace

from friday import recall

FACTS = [f"filler fact number {i} about nothing" for i in range(14)] + [
    "The user rides a Ducati Monster", "The user's shop is called Integrity Powersports", "He likes strong coffee"]


def test_few_facts_are_all_kept():
    assert recall.relevant_facts(FACTS[:5], "anything") == FACTS[:5]


def test_embeddings_pick_the_matching_fact_in_original_order():
    def fake_embed(texts):  # a 2-D "meaning": [motorcycle-ness, coffee-ness]
        return [[1.0 if ("ducati" in t.lower() or "bike" in t.lower()) else 0.0,
                 1.0 if "coffee" in t.lower() else 0.0] for t in texts]
    recall._VEC_CACHE.clear()
    out = recall.relevant_facts(FACTS, "what bike do I ride", k=2, embed_fn=fake_embed)
    assert "The user rides a Ducati Monster" in out and len(out) == 2
    assert out == [f for f in FACTS if f in out]


def test_keyword_fallback_when_embeddings_fail():
    recall._VEC_CACHE.clear()
    out = recall.relevant_facts(FACTS, "tell me about the Ducati", k=3, embed_fn=lambda t: None)
    assert "The user rides a Ducati Monster" in out


def test_prompt_section_notes_the_cut(tmp_path):
    recall._VEC_CACHE.clear()
    mem = SimpleNamespace(facts=FACTS, path=tmp_path / "memory.json")
    import friday.knowledge as K
    orig, K.embed = K.embed, (lambda t: None)
    try:
        s = recall.prompt_section(mem, "ducati")
    finally:
        K.embed = orig
    assert "most relevant of 17" in s and "Ducati" in s
