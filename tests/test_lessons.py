from pathlib import Path

from friday import lessons as L


def test_teach_match_and_forget(tmp_path: Path, monkeypatch=None):
    L.DATA_DIR = tmp_path
    try:
        a = L.add("Prices", "Give the number first, then the source.", "price, cost")
        b = L.add("", "Always be brief.")
        assert [x["title"] for x in L.load()] == ["Prices", "Always be brief."]
        assert [x["id"] for x in L.matching("what is the price of a bike")] == [a["id"], b["id"]]
        assert [x["id"] for x in L.matching("hello")] == [b["id"]]  # keyworded lesson skipped, always-on kept
        assert "Give the number first" in L.prompt_section("price check")
        assert L.remove("prices") == 1 and len(L.load()) == 1
    finally:
        L.DATA_DIR = None
