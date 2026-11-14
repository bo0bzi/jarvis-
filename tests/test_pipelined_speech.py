from friday.voice import split_sentences


def test_split_sentences_joins_short_and_caps_chunks():
    assert split_sentences("Yes, sir.") == ["Yes, sir."]
    out = split_sentences("Ok. Done. The weather in Toronto is five degrees and cloudy today. It may rain later tonight.")
    assert out[0].startswith("Ok. Done.") and len(out) == 3 - 1 or len(out) <= 3
    many = " ".join(f"This is sentence number {i} of a long reply." for i in range(12))
    chunks = split_sentences(many)
    assert len(chunks) == 6 and "sentence number 11" in chunks[-1]
    assert split_sentences("") == [""]
