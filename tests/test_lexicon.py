from zigi_agent.lexicon import KEYTERMS, hint_note, hints


def test_mishearings_seen_in_calls_get_hints():
    assert hints("Check fiber availability in North Ligon") == [("North Ligon", "North Legon")]
    assert hints("I live in Ashong Mak, near the junction.") == [("Ashong Mak", "Ashongman")]
    assert ("pesos", "pesewas") in hints("Which just for you bundle costs 50 pesos?")
    assert ("faba", "fibre") in hints("Can I get Faba in Dansoman?")


def test_ordinary_sentences_get_no_hints():
    for text in ["How does roaming work when I travel to Lagos?", "Show me the data bundles", "Yes go ahead", "My number is 0244123456"]:
        assert hints(text) == [], text
    assert hint_note("Show me the data bundles") == ""


def test_keyterms_fit_the_realtime_limit():
    assert len(KEYTERMS) <= 50 and all(len(k) <= 50 for k in KEYTERMS)
