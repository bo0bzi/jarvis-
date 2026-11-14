"""The 'it is useless' fixes from the 20:55 logs: search engines mean search, faking escalates, 'Opening X' is caught."""
from friday import llm, quick, webcontext as W


def test_search_engine_url_means_search_not_homepage():
    t = "go to google.com, check if there is any long weekends in ontario canada in november"
    assert W._ENGINE.search("google.com") and not W._ENGINE.search("integritypowersports.com")
    core = W._query_core(t)
    assert "google" not in core.lower() and "long weekends" in core and "ontario" in core.lower(), core
    assert W.wants_search(t)


def test_opening_claims_are_caught_but_opening_hours_are_not():
    assert llm.unbacked_claim("Opening Google.com in the default browser.", []) == "opened"
    assert llm.unbacked_claim("Sure. Opening the portable version now.", []) == "opened"
    assert llm.unbacked_claim("Our opening hours are 9 to 5.", []) == ""
    assert llm.unbacked_claim("Opening Google.com in the default browser.", ["browser_open"]) == ""


def test_guard_failure_replies_are_recognised():
    assert llm.is_guard_fail(llm.HONEST_FAIL)
    assert llm.is_guard_fail(llm._honest_fail("installed"))
    assert not llm.is_guard_fail("It is 6 pm.")
    assert "think hard" in llm.HONEST_FAIL and "GitHub link" not in llm.HONEST_FAIL


def test_go_to_opens_but_visit_reads():
    assert quick._GO_TO.match("go to google.com")
    assert quick._GO_TO.match("jarvis open integritypowersports.com please")
    assert not quick._GO_TO.match("go to google.com and check the weather")
    assert not quick._GO_TO.match("visit google.com")
