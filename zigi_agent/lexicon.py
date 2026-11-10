"""Words speech recognition gets wrong on these calls, and what was probably meant.

Ghanaian place names and MTN product names are rare in the transcriber's training data. Seen on 09 and 10 Nov and
in scripts/wer_eval.py: "North Ligon" for North Legon, "Ashong Mak" for Ashongman, "Faba" for fibre, "pesos" for
pesewas. We never rewrite what the caller said; we give the agent a hint ("'Ashong Mak' is probably Ashongman") and
it decides, using the conversation. The same terms go to the transcriber as keyterms (app/config.py).
"""

from __future__ import annotations

import difflib
import re

PLACES = [
    # Greater Accra
    "Accra Central", "Adabraka", "Abeka", "Abelemkpe", "Abossey Okai", "Achimota", "Adenta", "Agbogba", "Airport Residential",
    "Ashaiman", "Ashongman", "Asylum Down", "Awoshie", "Bubuashie", "Cantonments", "Dansoman", "Darkuman", "Dome",
    "Dzorwulu", "East Legon", "Haatso", "Kanda", "Kaneshie", "Kasoa", "Kokomlemle", "Korle Bu", "Kotobabi", "Kwabenya",
    "Labadi", "Labone", "Lapaz", "Legon", "Madina", "Mallam", "McCarthy Hill", "Nima", "North Kaneshie", "North Legon",
    "Nungua", "Ogbojo", "Osu", "Oyarifa", "Pokuase", "Roman Ridge", "Sakaman", "Sakumono", "Santa Maria", "Spintex",
    "Taifa", "Teshie", "Tema", "Tema Community 25", "Tesano", "Trasacco", "West Legon", "Weija", "Abokobi", "Amasaman",
    # Other cities and towns
    "Kumasi", "Adum", "Asokwa", "Bantama", "Ejisu", "Takoradi", "Sekondi", "Cape Coast", "Tamale", "Ho", "Koforidua",
    "Sunyani", "Techiman", "Bolgatanga", "Wa", "Obuasi", "Tarkwa", "Winneba", "Nkawkaw", "Aflao", "Hohoe",
]  # fmt: skip

PRODUCTS = [
    "fibre", "broadband", "Just4U", "MoMo", "Mobile Money", "Mashup", "Turbonet", "Qwik Loan", "Zone", "eSIM", "PUK",
    "Ghana Card", "myMTN", "airtime", "data bundle", "pesewas", "cedis", "USSD", "roaming", "SIM",
]  # fmt: skip

# For the transcriber: ElevenLabs Scribe v2 realtime takes at most 50 keyterms (batch takes 1,000), and keyterm
# prompting is billed extra. Products first, then the places seen in calls and most likely in fibre checks; the full
# list still feeds hints() below.
_TOP_PLACES = [
    "Legon", "East Legon", "North Legon", "West Legon", "Ashongman", "Sakaman", "Dansoman", "Kasoa", "Adenta", "Madina",
    "Spintex", "Tema", "Teshie", "Nungua", "Achimota", "Haatso", "Dome", "Kwabenya", "Lapaz", "Kaneshie", "Osu",
    "Labone", "Cantonments", "Kumasi", "Adum", "Takoradi", "Tamale", "Weija", "Ashaiman", "Sakumono",
]  # fmt: skip
KEYTERMS = (PRODUCTS + _TOP_PLACES)[:50]

# Mishearings seen often enough to name outright.
KNOWN = {
    "ligon": "Legon", "faba": "fibre", "fiba": "fibre", "fever": "fibre", "pesos": "pesewas", "peso": "pesewas",
    "just for you": "Just4U", "mo mo": "MoMo", "turbo net": "Turbonet", "quick loan": "Qwik Loan", "e sim": "eSIM",
}  # fmt: skip

_CANDIDATES = {t.lower(): t for t in [*PLACES, *PRODUCTS]}
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


_EDGE = {"in", "at", "near", "the", "a", "to", "for", "my", "and", "or", "of", "is", "i", "im", "i'm", "from", "on"}
_SPELLING = {"fiber": "fibre"}  # variants, not mishearings


def hints(text: str) -> list[tuple[str, str]]:
    """(heard, probably meant) pairs for words or short phrases in a transcript that look like misheard terms."""
    words = _WORD.findall(text)
    low = [w.lower() for w in words]
    best: dict[str, tuple[float, str]] = {}  # meant -> (score, heard)
    for heard, meant in KNOWN.items():
        if re.search(rf"\b{re.escape(heard)}\b", text, re.I) and meant.lower() not in text.lower():
            best[meant] = (2.0, heard)
    # one to three word spans compared with known names ("Ashong Mak" ~ "Ashongman", "Osaka map" ~ "Sakaman")
    for n in (1, 2, 3):
        for i in range(len(low) - n + 1):
            span_words = low[i : i + n]
            if span_words[0] in _EDGE or span_words[-1] in _EDGE or any(w in _SPELLING for w in span_words):
                continue
            span = " ".join(span_words)
            if len(span) < 4 or span in _CANDIDATES:
                continue
            squashed = span.replace(" ", "")
            for cand_low, cand in _CANDIDATES.items():
                if cand_low in text.lower() or cand in best and best[cand][0] >= 2:
                    continue
                score = difflib.SequenceMatcher(None, squashed, cand_low.replace(" ", "")).ratio()
                if score >= 0.8 and score > best.get(cand, (0.0, ""))[0]:
                    best[cand] = (score, " ".join(words[i : i + n]))
    # a span already explained by a longer, better match is dropped ("Ligon" inside "North Ligon")
    out = sorted(((h, m) for m, (_, h) in best.items()), key=lambda x: -len(x[0]))
    kept: list[tuple[str, str]] = []
    for h, m in out:
        if not any(h.lower() in k.lower() for k, _ in kept):
            kept.append((h, m))
    return kept[:4]


def hint_note(text: str) -> str:
    pairs = hints(text)
    if not pairs:
        return ""
    said = "; ".join(f"'{h}' is probably {m}" for h, m in pairs)
    return (
        f"Speech recognition may have misheard: {said}. Use the likely meaning if it fits the conversation; if a name "
        "matters for a check or a purchase and you're unsure, confirm it with the caller."
    )
