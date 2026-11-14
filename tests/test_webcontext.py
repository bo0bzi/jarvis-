from friday import webcontext as W


def test_urls_found_and_github_skipped():
    assert W.urls_in("go to integritypowersports.com and tell me") == ["integritypowersports.com"]
    assert W.urls_in("install github.com/a/b") == []
    assert W.urls_in("edit main.py please") == []


def test_search_intent():
    assert W.wants_search("what is the latest news on the Honda CBR600RR")
    assert not W.wants_search("what's the weather")
    assert not W.wants_search("set a timer for 5 minutes")
