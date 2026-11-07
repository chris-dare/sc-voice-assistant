"""Speech Renderer: turns SunCo message content into text that sounds right when spoken.

Telco rules: USSD codes, phone numbers and number masks, data units and Ghana cedi amounts.
"""

from __future__ import annotations

import re
from typing import Any

_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_USSD_RE = re.compile(r"(?<![\w*])\*\d+(?:\*\d+)*#")
_MASK_RE = re.compile(r"\b(\d{2,6})([Xx]{3,})\b")
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d ]{8,15}\d(?![\w.])")
_DATA_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(GB|MB|KB|TB)\b", re.IGNORECASE)
_CEDI_RE = re.compile(r"(?:GH[S₵C¢]|GH\s?₵|₵)\s?(\d+(?:,\d{3})*(?:\.\d{1,2})?)", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+?(?=[.,;:!?)\]]*(?:\s|$))")
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff️]")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_DATA_UNITS = {"KB": "kilobytes", "MB": "megabytes", "GB": "gigabytes", "TB": "terabytes"}

MAX_SPOKEN_OPTIONS = 3


def _spell_digits(digits: str) -> str:
    return " ".join(_DIGIT_WORDS[int(d)] for d in digits if d.isdigit())


def _ussd(match: re.Match[str]) -> str:
    out: list[str] = []
    for ch in match.group(0):
        if ch == "*":
            out.append("star")
        elif ch == "#":
            out.append("hash")
        else:
            out.append(_DIGIT_WORDS[int(ch)])
    return " ".join(out)


def _mask(match: re.Match[str]) -> str:
    return f"{_spell_digits(match.group(1))} followed by {len(match.group(2))} digits"


def _phone(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 9:
        return raw
    spoken = _spell_digits(digits)
    return ("plus " + spoken) if raw.startswith("+") else spoken


def _data(match: re.Match[str]) -> str:
    amount, unit = match.group(1), match.group(2).upper()
    return f"{amount} {_DATA_UNITS[unit]}"


def _cedi(match: re.Match[str]) -> str:
    amount = match.group(1).replace(",", "")
    if "." in amount:
        whole, frac = amount.split(".")
        frac = frac.ljust(2, "0")
        if int(frac):
            return f"{int(whole)} cedis {int(frac)} pesewas"
        return f"{int(whole)} cedis"
    return f"{int(amount)} cedis"


def normalize(text: str) -> str:
    """Clean one block of bot text for TTS."""
    t = _MD_LINK_RE.sub(lambda m: f"{m.group(1)}, linked on screen", text)
    t = _URL_RE.sub("the link on screen", t)
    t = _EMOJI_RE.sub("", t)
    # Markdown emphasis, headings, code ticks, list bullets.
    t = re.sub(r"(\*\*|__|`)", "", t)
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", t)
    # Telco rules: order matters (USSD before phone numbers, masks before digits).
    t = _USSD_RE.sub(_ussd, t)
    t = _MASK_RE.sub(_mask, t)
    t = _CEDI_RE.sub(_cedi, t)
    t = _DATA_RE.sub(_data, t)
    t = _PHONE_RE.sub(_phone, t)
    # Line breaks become sentence breaks; collapse whitespace.
    lines = [ln.strip() for ln in t.splitlines()]
    parts: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if parts and not re.search(r"[.!?:;,]$", parts[-1]):
            parts[-1] += "."
        parts.append(ln)
    t = " ".join(parts)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    t = re.sub(r"\.{2,}", ".", t)
    return t


def _join_options(labels: list[str]) -> str:
    labels = [lbl for lbl in labels if lbl][:MAX_SPOKEN_OPTIONS]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " or " + labels[-1]


def _actions(actions: list[dict[str, Any]] | None) -> str:
    labels = [normalize(a.get("text", "")) for a in actions or [] if a.get("type") in {"reply", "postback"}]
    if not labels:
        return ""
    extra = " More options are on screen." if len(labels) > MAX_SPOKEN_OPTIONS else ""
    return f"You can say {_join_options(labels)}.{extra}"


def render(content: dict[str, Any]) -> str:
    """Speakable text for any SunCo message content type. Empty string = nothing to say."""
    ctype = content.get("type", "text")
    if ctype == "text":
        spoken = normalize(content.get("text", ""))
        options = _actions(content.get("actions"))
        if spoken and options and not re.search(r"[.!?]$", spoken):
            spoken += "."
        return " ".join(s for s in (spoken, options) if s)
    if ctype in {"carousel", "list"}:
        items = content.get("items") or []
        titles = [normalize(i.get("title", "")) for i in items]
        if not titles:
            return ""
        lead = f"I found {len(titles)} option{'s' if len(titles) != 1 else ''}: {_join_options(titles)}."
        more = " The rest are on screen." if len(titles) > MAX_SPOKEN_OPTIONS else ""
        return lead + more
    if ctype in {"image", "file"}:
        caption = normalize(content.get("text", "") or content.get("altText", ""))
        what = "an image" if ctype == "image" else "a file"
        return f"{caption} I've sent {what} to your screen.".strip()
    if ctype == "location":
        return "I've sent a location to your screen."
    if ctype == "form":
        return "I've sent a short form to your screen."
    return normalize(content.get("text", ""))


def split_sentences(text: str, max_chars: int = 240) -> list[str]:
    """Chunk text into sentence groups for sentence-by-sentence TTS (AD-06)."""
    out: list[str] = []
    for sentence in _SENTENCE_RE.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if out and len(out[-1]) + len(sentence) + 1 <= max_chars and len(out[-1]) < 60:
            out[-1] = f"{out[-1]} {sentence}"
        else:
            out.append(sentence)
    return out
