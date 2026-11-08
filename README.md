# SC Voice Assistant POC

Voice mode, voice notes and read-aloud replies for the existing Zigi chatbot behind Sunshine Conversations (SunCo), using ElevenLabs for speech. The bot is unchanged: the gateway posts the customer's words to SunCo as text and speaks the bot's replies back.

Design: [Solution Architecture: SC Voice Assistant POC](https://claude.ai/artifact/6eZNRr7wcy8dHQnX5zLnfR).

## Run it

```bash
uv sync
```

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. The page needs a secure context for the microphone: `localhost` or HTTPS.

Required environment: `ZENDESK_APP_ID`, `ZENDESK_KEY_ID`, `ZENDESK_SECRET`, `ELEVENLABS_API_KEY`. Optional ones are in `.env.example` (copy it to `.env`). With no keys or network, run with `MOCK_PROVIDERS=true` for a fake telco bot, STT and TTS.

Logs, traces and per-turn metrics go to Pydantic Logfire when `LOGFIRE_TOKEN` (or `LOGFIRE_API_KEY`) is set. Transcripts and reply text are never logged.

## What it does

| Mode | Flow |
| --- | --- |
| Voice | Mic PCM streams over a WebSocket to ElevenLabs Scribe realtime (live captions, server VAD). The final transcript goes to SunCo with `metadata.channel=voice`. Bot replies come back by polling or webhook, pass through the telco Speech Renderer, and stream back as ElevenLabs Flash TTS, sentence by sentence. Talk over the assistant to interrupt it. |
| Chat | Text messages, quick-reply chips, a voice-note button (Scribe batch STT; the transcript is shown for review before sending) and a speaker button on each bot reply (cached TTS). |
| Attachments | The + button opens Camera, Photos and Files. Photos are resized in the browser (2048 px, JPEG); files up to 4 MB (PDF, Office, text, CSV, images). The gateway uploads to SunCo and posts an `image` or `file` message. |

The Speech Renderer reads `*138#` as "star one three eight hash", `024XXXXXXX` as "zero two four followed by 7 digits", `GH₵10.50` as "10 cedis 50 pesewas" and `5GB` as "5 gigabytes", and replaces links with "linked on screen". The dev bot's debug lines (`zigi 2 preprod`, `PRODUCTION`) are shown dimmed and not spoken (`SPEECH_SKIP_PATTERNS`).

When the bot is slow, the gateway speaks a holding phrase after 2.5 s (`HOLD_PROMPT`, `HOLD_AFTER_S`) and gives up after 45 s (`BOT_TIMEOUT_S`).

## Voice modes: Chatbot relay and Agentic

The voice screen has a switch, shown when `AGENT_MODE_ENABLED=true` (the feature flag) and the LLM credential `OPENROUTER_API_KEY` is set. The choice is kept on the device.

- **Chatbot relay** (`mode=relay`): every turn goes to Zigi as said, and Zigi's replies are spoken. Unchanged from before.
- **Agentic** (`mode=agent`, design 5.8): the Zigi agent service talks to the caller and uses Zigi as a tool. It answers small talk itself, rewrites Zigi's replies for speech, sends Zigi input in the exact form it expects (an OTP said in a sentence reaches Zigi as digits), and opens links Zigi sent (MTN hosts only) instead of reading URLs. A slow or failed agent turn falls back to chatbot relay. The agent's own replies are never posted to SunCo (they would reach Botpress).

### The agent service (`zigi_agent/`)

A separate service built on Pydantic AI. The gateway sends each turn with the call's state (history, Zigi state) and gets back an NDJSON stream of sentences to speak (`POST /v1/turn`, see `zigi_agent/turn.py`). Deployed as its own Vercel project, `sc-voice-agent` (https://sc-voice-agent-eosin.vercel.app), called with a bearer token (`AGENT_SERVICE_TOKEN` on both projects). With `AGENT_SERVICE_URL` empty the gateway runs the same code in-process (local dev, tests).

| Capability | What it does |
| --- | --- |
| `zigi` (Capability) | `ask_zigi`, `choose_option`; ZIGI STATE added to the instructions every step |
| `WebFetch` | reads pages Zigi linked to; local, SSRF-safe, `AGENT_LINK_HOSTS` only |
| `ZigiGuardrails` | before each tool call: OTP, phone and amount reduced to what Zigi expects, no "yes" the caller didn't give, only Zigi's links; refusals go back to the model as `ModelRetry` |
| `ProcessHistory` | keeps the last `AGENT_HISTORY_MESSAGES` messages, cut on turn boundaries |
| `ModelFallback` | `AGENT_FALLBACK_MODELS` when the main model errors or gives no first token within `AGENT_FIRST_EVENT_TIMEOUT_S` |

Spoken sentences are checked as they stream: a number not said by Zigi or the caller is dropped (and, before any Zigi call, the agent is sent back to ask Zigi). Logfire traces every model and tool call with `include_content=False`, so call content stays out of logs. Persona: `zigi_agent/persona.py`.

Evals (`pydantic_evals`, the real model against the scripted Zigi in `app/mocks.py`):

```bash
uv run python -m evals.agent_evals --repeat 3
```

Each turn's metrics carry `mode` and `route` (`relay`, `direct`, `zigi`, `fallback`, `push`), in Logfire and in PostHog `voice_turn`.

Background noise: while nobody is speaking, the client sends silence to STT instead of mic audio (`NOISE_GATE`), the gateway commits the transcript 1 s after the client hears the end of speech if STT has not (`STT_FORCE_COMMIT_S`), and filler-only transcripts ("uh, uh") are ignored.

## Attachments and privacy

This SunCo app has private attachments: a `mediaUrl` needs the app key. `GET /v1/media?url=…` checks that the attachment is in the caller's own conversation, then fetches a short-lived signed URL with the app key and redirects the browser to it. The key never reaches the client, and it is only ever sent to `*.zendesk.com` or `*.smooch.io`. The 4 MB limit (`ATTACHMENT_MAX_BYTES`) sits under Vercel's 4.5 MB request-body cap.

## Layout

| Path | Component in the design |
| --- | --- |
| `app/session.py` | Session Manager and Turn Coordinator (voice WebSocket), both modes |
| `app/agent_client.py` | Client for the agent service (HTTP or in-process) |
| `zigi_agent/` | The agent service (Pydantic AI) |
| `evals/` | Agent evals |
| `app/sunco.py` | Conversation Client (SunCo v2) |
| `app/delivery.py` | Webhook Receiver and polling fallback |
| `app/stt.py`, `app/tts.py` | STT and TTS Adapters, read-aloud cache |
| `app/speech.py` | Speech Renderer |
| `app/auth.py` | POC user tokens (replace with the mobile app's auth) |
| `app/bus.py` | In-memory or Redis pub/sub for multi-replica routing |
| `app/telemetry.py` | Logfire setup |
| `web/` | Web POC client (no build step) |
| `docs/protocol.md` | Voice WebSocket protocol |

## Deploying

Every deploy runs `scripts/audit.sh` first: `uv audit` for the Python dependencies (gateway and agent service share `uv.lock`) and `npm audit` for the web client's only third-party code, the posthog-js version PostHog's CDN serves.

```bash
scripts/deploy_gateway.sh
```

```bash
scripts/deploy_agent.sh
```

## Tests

```bash
uv run pytest
```

The suite runs on mock providers, including an end-to-end voice turn over the WebSocket.

## Webhook delivery (production)

Polling is the default, so no public URL is needed. For webhooks, create a custom integration on the SunCo app with a `conversation:message` webhook pointing at `https://<host>/webhooks/sunco` (`scripts/sunco_webhook.py <gateway-url>` prints the request; `--apply` creates it and prints the secret once). Then set `SUNCO_DELIVERY=webhook` and `SUNCO_WEBHOOK_SECRET` to the webhook's secret, and `REDIS_URL` on both services so every instance hears every webhook. The agent service listens on the same Redis channel and falls back to polling if it hears nothing for 8 s.

## Scale and performance

- **Shared store (`app/store.py`).** With `REDIS_URL` set, call state, the user lookup, rate limits, call slots, the TTS cache and the semantic cache are shared by every instance; without it they live in memory (one instance). Call state is kept per conversation, so a reconnect (Vercel ends WebSockets at 300 s) resumes the agent's memory and Zigi's state.
- **Per-user limits.** Per-minute limits on each action, at most `LIMIT_CONCURRENT_CALLS` calls per user (slots renewed by a 30 s heartbeat, so a dropped call frees itself), and `LIMIT_DAILY_VOICE_MINUTES` of voice a day. Over a limit: HTTP 429 with `Retry-After`, or WebSocket close 4429.
- **Semantic cache (`zigi_agent/cache.py`).** A general question close in meaning (cosine ≥ 0.9 on 256-dimension embeddings) to one Zigi answered in the last 6 h is answered from that answer, without the 5 to 17 s Zigi round trip. Never while Zigi waits for input, never personal, account or transactional questions, never answers that ask for input or greet by name. Logged as `semantic cache hit` with `route: cache`.
- **TTS sentence cache.** Sentences up to 120 characters (lead-ins, holds, common replies) are cached as PCM by voice, model and text.
- **Load test.** `uv run python scripts/loadtest.py --calls 10 50 100 200 [--mode agent]` starts a gateway on fakes with vendor delays (Zigi 3 s, TTS 0.25 s) and drives concurrent typed voice calls.
