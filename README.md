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

## Voice modes: Direct and Smart

The voice screen has a switch (shown when `OPENROUTER_API_KEY` is set; the choice is kept on the device):

- **Direct** (`mode=relay`): every turn goes to Zigi as said, and Zigi's replies are spoken. Unchanged from before.
- **Smart** (`mode=agent`, design 5.8): an LLM (`AGENT_MODEL`, default `openai/gpt-6-luna` through OpenRouter) talks to the user and calls Zigi as a tool (`ask_zigi`, `choose_option`). It answers small talk itself, rewrites Zigi's replies for speech, and sends Zigi input in the exact form it expects (an OTP said in a sentence reaches Zigi as digits). Gateway code enforces the guardrails: spoken numbers must come from Zigi or the user, a yes is only forwarded when the user said yes, OTPs and phone numbers are normalised, and a slow or failed LLM turn falls back to Direct. The agent's own replies are never posted to SunCo (they would reach Botpress).

Each turn's metrics carry `mode` and `route` (`relay`, `direct`, `zigi`, `fallback`, `push`), in Logfire and in PostHog `voice_turn`.

## Attachments and privacy

This SunCo app has private attachments: a `mediaUrl` needs the app key. `GET /v1/media?url=…` checks that the attachment is in the caller's own conversation, then fetches a short-lived signed URL with the app key and redirects the browser to it. The key never reaches the client, and it is only ever sent to `*.zendesk.com` or `*.smooch.io`. The 4 MB limit (`ATTACHMENT_MAX_BYTES`) sits under Vercel's 4.5 MB request-body cap.

## Layout

| Path | Component in the design |
| --- | --- |
| `app/session.py` | Session Manager and Turn Coordinator (voice WebSocket), both modes |
| `app/agent.py` | Agent mode: LLM client, instructions and tools, Zigi state, guardrails |
| `app/sunco.py` | Conversation Client (SunCo v2) |
| `app/delivery.py` | Webhook Receiver and polling fallback |
| `app/stt.py`, `app/tts.py` | STT and TTS Adapters, read-aloud cache |
| `app/speech.py` | Speech Renderer |
| `app/auth.py` | POC user tokens (replace with the mobile app's auth) |
| `app/bus.py` | In-memory or Redis pub/sub for multi-replica routing |
| `app/telemetry.py` | Logfire setup |
| `web/` | Web POC client (no build step) |
| `docs/protocol.md` | Voice WebSocket protocol |

## Tests

```bash
uv run pytest
```

The suite runs on mock providers, including an end-to-end voice turn over the WebSocket.

## Webhook delivery (production)

Polling is the default, so no public URL is needed. For webhooks, create a custom integration on the SunCo app with a `conversation:message` webhook pointing at `https://<host>/webhooks/sunco`. Then set `SUNCO_DELIVERY=webhook` and `SUNCO_WEBHOOK_SECRET` to the webhook's secret.
