"""Speech-to-text accuracy on the call's own vocabulary: WER, key-term accuracy and digit accuracy.

Production calls have no reference text, so WER is measured offline: each line of evals/stt_set.json is spoken by
several African-accented ElevenLabs voices, clean and with a second voice talking underneath, then transcribed
through the same realtime path calls use (ElevenLabs Scribe realtime, with our keyterms). Synthetic speech is
cleaner than real callers, so treat the numbers as a floor; real recordings (with consent) are the next step.

    uv run python scripts/wer_eval.py                       # all voices, clean and noisy
    uv run python scripts/wer_eval.py --keyterms none       # compare without keyterm hints
    uv run python scripts/wer_eval.py --voices 1 --limit 5  # quick check

Needs ELEVENLABS_API_KEY. Audio is cached under --cache (default: .wer_cache, git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import struct
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.stt import ElevenLabsSTT  # noqa: E402
from zigi_agent.lexicon import hints  # noqa: E402
from zigi_agent.rules import spoken_digits  # noqa: E402

VOICES = {
    "alex_oke_ng_m": "JvYVKHK9a3ohjGBudnKo",
    "chidinma_ng_f": "D7WRyUKRrywbxrCbDjDU",
    "layo_af_f": "13SMxgZX6Y3GVlBXWTE8",
    "tre_us_m": "zm9E2qHSmW6HZXdIYeV9",
}
LEGACY_KEYTERMS = ["data bundle", "airtime", "MoMo", "roaming", "USSD", "SIM", "PUK"]  # before 10 Nov
BABBLE = "So I told her we'd meet at the station after work, but the traffic on that road is always terrible."

_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "hundred": 100,
}  # fmt: skip


def _number_run(words: list[str]) -> list[str]:
    """Number words to digits: "zero two four" -> ["0", "2", "4"]; "eight hundred and thirty seven" -> ["837"];
    "fifty" -> ["50"]."""
    if "hundred" in words:
        val, cur = 0, 0
        for w in words:
            if w == "hundred":
                cur = max(cur, 1) * 100
            elif w != "and":
                cur += _NUM[w]
        return [str(val + cur)]
    out: list[str] = []
    for w in words:
        n = _NUM[w]
        if out and n < 10 and out[-1].endswith("0") and len(out[-1]) == 2 and _NUM.get(w) is not None and out[-1] != "10":
            out[-1] = str(int(out[-1]) + n)  # "thirty seven" -> 37
        else:
            out.append(str(n))
    return out


def normalise(text: str) -> list[str]:
    """Lower-case words with numbers as digits and money and codes in one form, so formatting isn't an error."""
    t = text.lower().replace("gh₵", " cedis ").replace("ghc", " cedis ").replace("ghs", " cedis ")
    t = re.sub(r"\bstar\b", " * ", t)
    t = re.sub(r"\bhash\b", " # ", t).replace("just 4 u", "just4u").replace("just for you", "just4u")
    t = re.sub(r"(\d),(\d)", r"\1\2", t)
    t = re.sub(r"[^a-z0-9.*#' ]+", " ", t).replace("*", " * ").replace("#", " # ")
    words = [w.strip(".'") for w in t.split() if w.strip(".'")]
    out: list[str] = []
    i = 0
    while i < len(words):
        if words[i] in _NUM:
            j = i
            while j < len(words) and (words[j] in _NUM or (words[j] == "and" and j + 1 < len(words) and words[j + 1] in _NUM)):
                j += 1
            out += _number_run(words[i:j])
            i = j
            continue
        w = words[i]
        out.append({"cedi": "cedis", "pesewa": "pesewas", "pesos": "pesewas", "peso": "pesewas", "fiber": "fibre"}.get(w, w))
        i += 1
    # One token for digit strings said digit by digit ("0 2 4 4" -> "0244") and for codes ("* 138 #" -> "*138#").
    joined: list[str] = []
    for w in out:
        if joined and w.isdigit() and joined[-1].isdigit():
            joined[-1] += w  # "0 2 4 4" or "0244 123 456" -> one number
        elif joined and (w == "#" or (w.isdigit() and joined[-1] == "*")) and re.fullmatch(r"\*[\d*]*|\*", joined[-1]):
            joined[-1] += w
        elif joined and w.isdigit() and joined[-1].startswith("*") and not joined[-1].endswith("#"):
            joined[-1] += w
        else:
            joined.append(w)
    return joined


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    """(word edits, reference length)"""
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)], len(ref)


def key_ok(key: str, hyp_text: str) -> bool:
    return " ".join(normalise(key)) in " ".join(normalise(hyp_text))


def mix(clean: bytes, babble: bytes, snr_db: float = 12.0) -> bytes:
    """Another voice talking underneath, 12 dB below the caller: a side conversation a metre or two away, roughly what
    reaches the mic once voice focus (web/app.js) has opened it for the caller (09 Nov calls)."""
    a = struct.unpack(f"<{len(clean) // 2}h", clean)
    b = struct.unpack(f"<{len(babble) // 2}h", babble) or (0,)
    pa = sum(x * x for x in a) / max(1, len(a))
    pb = sum(x * x for x in b) / max(1, len(b)) or 1
    gain = math.sqrt(pa / pb / (10 ** (snr_db / 10)))
    out = [max(-32768, min(32767, int(x + gain * b[i % len(b)]))) for i, x in enumerate(a)]
    return struct.pack(f"<{len(out)}h", *out)


def focus(audio: bytes) -> bytes:
    """A candidate second stage of voice focus, measured but not shipped: while the mic is open, 20 ms frames well
    below the caller's level turned down by 12 dB. On 10 Nov it made no measurable difference (WER 12.0% vs 11.1%
    with a voice 12 dB below, within run-to-run noise), so the app does not do it. Kept to re-test on real calls."""
    n = 320
    frames = [audio[i : i + 2 * n] for i in range(0, len(audio), 2 * n)]

    def rms(f: bytes) -> float:
        xs = struct.unpack(f"<{len(f) // 2}h", f) if len(f) >= 2 else (0,)
        return math.sqrt(sum(x * x for x in xs) / len(xs)) / 32768

    levels = [rms(f) for f in frames]
    voiced = sorted(v for v in levels if v > 0.012)
    if not voiced:
        return audio
    voice = voiced[int(len(voiced) * 0.75)]
    out = []
    for f, lv in zip(frames, levels, strict=True):
        if lv < voice * 0.35:
            xs = struct.unpack(f"<{len(f) // 2}h", f)
            f = struct.pack(f"<{len(xs)}h", *(x >> 2 for x in xs))
        out.append(f)
    return b"".join(out)


async def tts(http: httpx.AsyncClient, key: str, voice: str, text: str, cache: Path) -> bytes:
    path = cache / f"{voice}_{abs(hash(text)) % 10**10}.pcm"
    if path.exists():
        return path.read_bytes()
    resp = await http.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
        headers={"xi-api-key": key},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=60,
    )
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return resp.content


async def transcribe(stt: ElevenLabsSTT, pcm: bytes) -> str:
    """The realtime path calls use: 20 ms frames at about real time, a commit, then the final transcripts."""
    session = await stt.open_realtime()
    finals: list[str] = []

    async def collect() -> None:
        async for ev in session.events():
            if ev.kind == "final" and ev.text.strip():
                finals.append(ev.text.strip())

    reader = asyncio.create_task(collect())
    silence = b"\0" * 640 * 50  # 1 s
    for i in range(0, len(pcm) + len(silence), 640):
        frame = (pcm + silence)[i : i + 640]
        await session.send_pcm(frame)
        await asyncio.sleep(0.012)
    await session.commit()
    await asyncio.sleep(2.5)
    await session.close()
    reader.cancel()
    return " ".join(finals)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", type=int, default=len(VOICES))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keyterms", choices=["config", "legacy", "none"], default="config")
    ap.add_argument("--conditions", default="clean,babble,focus")
    ap.add_argument("--cache", default=".wer_cache")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    s = Settings()
    key = s.elevenlabs_api_key or os.environ.get("ELEVENLABS_API_KEY", "")
    cache = Path(args.cache)
    cache.mkdir(exist_ok=True)
    items = json.loads(Path(__file__).resolve().parents[1].joinpath("evals/stt_set.json").read_text())
    if args.limit:
        items = items[: args.limit]
    voices = dict(list(VOICES.items())[: args.voices])
    conditions = args.conditions.split(",")
    rows = []
    async with httpx.AsyncClient() as http:
        stt = ElevenLabsSTT(
            key, s.elevenlabs_base_url, s.stt_realtime_model, s.stt_batch_model, s.stt_language, s.stt_vad_silence_s,
            {"config": s.stt_keyterms, "legacy": LEGACY_KEYTERMS, "none": []}[args.keyterms], http,
        )  # fmt: skip
        babble = await tts(http, key, VOICES["tre_us_m"], BABBLE, cache)
        for vname, vid in voices.items():
            for item in items:
                clean = await tts(http, key, vid, item["text"], cache)
                for cond in conditions:
                    audio = {"clean": clean, "babble": mix(clean, babble), "focus": focus(mix(clean, babble))}[cond]
                    t0 = time.monotonic()
                    hyp = await transcribe(stt, audio)
                    edits, n = wer(normalise(item["text"]), normalise(hyp))
                    keys = [key_ok(k, hyp) for k in item["key"]]
                    # What reaches the agent: the transcript plus the mishearing hints it gets (zigi_agent/lexicon.py)
                    hinted = hyp + " " + " ".join(m for _, m in hints(hyp))
                    keys_hinted = [key_ok(k, hinted) for k in item["key"]]
                    row = {
                        "voice": vname, "condition": cond, "ref": item["text"], "hyp": hyp, "edits": edits, "words": n,
                        "keys_ok": sum(keys), "keys_hinted": sum(keys_hinted), "keys": len(keys), "secs": round(time.monotonic() - t0, 1),
                    }  # fmt: skip
                    if "digits" in item:
                        row["digits_ok"] = spoken_digits(hyp) == item["digits"] or item["digits"] in re.sub(r"\D", "", hyp)
                    rows.append(row)
                    flag = "" if edits == 0 and all(keys) else "  <-"
                    print(f"{vname:14} {cond:6} WER {edits}/{n}  keys {sum(keys)}/{len(keys)}  {hyp!r}{flag}", flush=True)

    def summary(sel: list[dict]) -> str:
        e = sum(r["edits"] for r in sel)
        n = sum(r["words"] for r in sel)
        k = sum(r["keys_ok"] for r in sel)
        kh = sum(r["keys_hinted"] for r in sel)
        kn = sum(r["keys"] for r in sel)
        d = [r["digits_ok"] for r in sel if "digits_ok" in r]
        digits = 100 * sum(d) / max(1, len(d))
        return (
            f"WER {100 * e / max(1, n):5.1f}%   key terms {100 * k / max(1, kn):5.1f}% (with hints {100 * kh / max(1, kn):5.1f}%)"
            f"   digits {digits:5.1f}% ({len(sel)} clips)"
        )

    print("\n== Summary")
    for cond in conditions:
        print(f"{cond:7} {summary([r for r in rows if r['condition'] == cond])}")
    for vname in voices:
        print(f"{vname:15} {summary([r for r in rows if r['voice'] == vname])}")
    misses: dict[str, int] = {}
    for r in rows:
        item = next(i for i in items if i["text"] == r["ref"])
        for k in item["key"]:
            if not key_ok(k, r["hyp"]):
                misses[k] = misses.get(k, 0) + 1
    print("Most-missed key terms:", ", ".join(f"{k} ({v})" for k, v in sorted(misses.items(), key=lambda x: -x[1])[:12]))
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
