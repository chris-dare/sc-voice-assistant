"""Load test the voice gateway with fake vendors (no SunCo, ElevenLabs or OpenRouter calls).

Starts one gateway (uvicorn, one worker) with MOCK_PROVIDERS and realistic vendor delays, then opens N
concurrent voice calls. Each call types a few turns over the WebSocket, as the web client does, and we measure
time to first audio and time to the end of the reply.

    uv run python scripts/loadtest.py --calls 10 50 100 200 --mode relay
    uv run python scripts/loadtest.py --calls 50 --mode agent

Vendor delays (seconds) are set with --zigi (bot reply, default 3.0) and --tts (first audio chunk, default 0.25).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import uuid

QUESTIONS = ["How does roaming work?", "Tell me about MoMo", "What data bundles do you have?"]


def serve(port: int, zigi: float, tts: float) -> None:
    """The gateway with slower fakes, so queues and timers behave as they do with the real vendors."""
    os.environ.update(MOCK_PROVIDERS="true", LIMITS_ENABLED="false", AGENT_MODE_ENABLED="true", LOGFIRE_TOKEN="")
    import uvicorn

    from app import mocks

    chunk = b"\x00\x00" * 4096

    async def stream(self, text, model_id, output_format):  # noqa: ANN001, ANN202
        await asyncio.sleep(tts)
        for _ in range(max(1, min(12, len(text) // 10))):
            await asyncio.sleep(0.02)
            yield chunk

    mocks.MockTTS.stream = stream
    init = mocks.MockSunCo.__init__
    mocks.MockSunCo.__init__ = lambda self, reply_delay_s=zigi: init(self, reply_delay_s)
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning", ws_max_size=2**20)


async def call(base: str, mode: str, turns: int, out: dict[str, list[float]]) -> None:
    import httpx
    import websockets

    async with httpx.AsyncClient() as http:
        token = (await http.post(f"{base}/v1/session", json={})).json()["token"]
    ws_url = base.replace("http", "ws") + f"/v1/voice/session?token={token}&mode={mode}&call={uuid.uuid4().hex}"
    try:
        async with websockets.connect(ws_url, max_size=2**22, open_timeout=30) as ws:
            async for raw in ws:
                if isinstance(raw, str) and json.loads(raw).get("type") == "ready":
                    break
            for i in range(turns):
                t0 = time.monotonic()
                first = None
                await ws.send(json.dumps({"type": "text", "text": QUESTIONS[i % len(QUESTIONS)]}))
                spoke = False
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    if isinstance(raw, bytes):
                        first = first or time.monotonic()
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "error" and msg.get("code", "").startswith("limit"):
                        raise RuntimeError(msg["code"])
                    if msg.get("type") == "state":
                        spoke = spoke or msg["state"] == "speaking"
                        if spoke and msg["state"] == "listening":
                            break
                out["first_audio"].append((first or time.monotonic()) - t0)
                out["turn"].append(time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(type(exc).__name__)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


async def drive(base: str, n: int, mode: str, turns: int) -> dict[str, object]:
    out: dict[str, list] = {"first_audio": [], "turn": [], "errors": []}
    t0 = time.monotonic()
    # Calls arrive over two seconds rather than all in the same millisecond.
    tasks = []
    for _ in range(n):
        tasks.append(asyncio.create_task(call(base, mode, turns, out)))
        await asyncio.sleep(2.0 / n)
    await asyncio.gather(*tasks)
    return {
        "calls": n,
        "mode": mode,
        "turns": len(out["turn"]),
        "errors": len(out["errors"]),
        "error_kinds": sorted(set(out["errors"])),
        "first_audio_p50_s": round(statistics.median(out["first_audio"]), 2) if out["first_audio"] else None,
        "first_audio_p95_s": round(pct(out["first_audio"], 0.95), 2),
        "turn_p95_s": round(pct(out["turn"], 0.95), 2),
        "wall_s": round(time.monotonic() - t0, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, nargs="+", default=[10, 50, 100])
    ap.add_argument("--mode", choices=["relay", "agent"], default="relay")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--zigi", type=float, default=3.0)
    ap.add_argument("--tts", type=float, default=0.25)
    ap.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.serve:
        serve(args.serve, args.zigi, args.tts)
        return
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, __file__, "--serve", str(port), "--zigi", str(args.zigi), "--tts", str(args.tts)],
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    try:
        import httpx

        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        for n in args.calls:
            print(json.dumps(asyncio.run(drive(f"http://127.0.0.1:{port}", n, args.mode, args.turns))), flush=True)
    finally:
        server.terminate()
        server.wait(10)


if __name__ == "__main__":
    main()
