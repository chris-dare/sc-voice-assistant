# Changelog

Release notes for the SC voice assistant POC: a voice gateway and web client in front of MTN Ghana's Zigi chatbot
(Botpress behind Zendesk Sunshine Conversations), with an agentic mode on Pydantic AI. Each release is a git tag
(`git tag -l`; the notes are on the tag and on the repository's Releases/Tags page). Newest first.

Deployed to Vercel: gateway and web client at sc-voice-assistant.vercel.app, agent service at
sc-voice-agent-eosin.vercel.app. The design is in the solution architecture document (section 5).

## v0.5.0 (10 Nov 2026): guardrails by risk, structured tasks, speech accuracy

### Changed
- **Guardrails by risk tier**, following current practice for agent guardrails (OpenAI's agent guide, Anthropic's
  "Building effective agents", recent work showing rigid rules over-block). High-impact actions are enforced in
  code: a yes only when the caller said yes, the caller's own choice at a purchase step, codes and phone numbers
  complete and said by the caller, links only from Zigi, no invented numbers. Low-impact messages are transformed,
  never refused. Three refusals in a turn, or Zigi repeating itself, make the agent stop and explain.
- **A model reads each Zigi reply** (what it waits for, its options, the step, whether answering commits the
  caller), with the earlier regex rules as fallback. It gets Zigi's previous messages as context.
- **Persona rewritten as principles** instead of one-off rules.
- **Read-back then yes counts as consent**: a yes right after a single option was offered is a choice. Spoken choices
  map to Zigi's numbers ("the first one", "number two", "50 pesewas" = GHc0.5).

### Added
- **Structured task records** for known use cases (bundle and airtime purchases, fibre check, PUK, balance, MoMo
  statement): kind, status and slots, updated by a small model after each turn at no audible cost. Used to answer
  Zigi without asking again, to read back before purchases, for a task card in the app, and for PostHog
  `task_status` events (completion and drop-off per use case).
- **Speech-recognition accuracy measurement**: `scripts/wer_eval.py` and `evals/stt_set.json` (30 utterances in the
  calls' vocabulary, four African-accented voices, clean and with a second voice 12 dB below). Production proxy:
  `asked_repeat` on each `voice_turn` (the repair rate).
- **Speech mitigations**: 50 keyterms for the transcriber (its realtime limit) and mishearing hints to the agent
  ("North Ligon" is probably North Legon), never rewriting the caller's words. Measured on 10 Nov: key terms right
  82% to 91% clean, 76% to 87% with a second voice; WER 6.1% to 4.9% clean; digits 100%.
- Evals: refusals per call (over-blocking), generalisation cases (a question mid-step, a change of mind, an unseen PUK
  flow), task checks. Turn results carry refusal reasons.

### Fixed
- The "caller must choose" check refused the right bundle eight times after the caller said yes to Zigi's offer.
- Numbered how-to steps were taken for a menu, so every later question was refused.
- Dependency audit: works when PostHog's CDN serves a posthog-js version not yet on npm.

### Measured, not shipped
- A second voice-focus stage (turning down quieter audio while the mic is open) made no measurable difference in
  the WER test, so it was taken out. The test condition remains for real recordings.

## v0.4.0 (9 Nov 2026): fixes from tester calls

### Added
- **Voice focus**: the app learns the caller's speaking level and ignores quieter voices (others in the room);
  browser gain control is off during calls.
- The agent stays silent for speech not meant for it (`[skip]`); the app says "Sounded like that wasn't for me".
- Echo filter: transcripts matching what the assistant just said are dropped.
- Zigi's numbered menus are answered with the number; the agent never chooses for the caller.
- `LOG_CONVERSATION_TEXT` (off by default) for test sessions.

### Fixed
- The agent stopped hearing Zigi after the first quiet half-second (timing out on the subscription closed it); it
  now listens through a queue, with a SunCo poll as backup.
- New customers: Zigi's greeting comes before its answer; the agent now waits for the answer.
- MTN facts only from Zigi or its cached answer, checked in code.
- A call that starts without saved state reads where Zigi left off from SunCo.
- Half-dictated numbers, stray yeses, and talking over answers (the gateway waits for the rest of a number and drops
  an answer the caller spoke over).

## v0.3.0 (9 Nov 2026): scale and performance

### Added
- Shared store (Upstash Redis in production): call state across reconnects, per-user limits (per-minute actions,
  two concurrent calls, 60 voice minutes a day), user lookup cache.
- Semantic cache of Zigi's general answers (embeddings, 6 h) and a TTS sentence cache.
- Load test with fake vendors (`scripts/loadtest.py`): 200 concurrent calls, no errors.
- Redis: TLS for Upstash, reconnect and retry, fall back to memory for 30 s on failure.
- Both services pinned to London (lhr1).
- Agentic persona rewritten to sound like a contact-centre agent.

## v0.2.0 (8 Nov 2026): agentic mode

### Added
- **Agentic voice mode**: an LLM agent (Pydantic AI, gpt-6-luna on OpenRouter) in front of Zigi, which it uses as
  a tool. Chatbot relay mode remains, and the caller switches between them.
- The agent as its own service (`zigi_agent/`, its own Vercel project), with capabilities, guardrails and evals.
- Caller memory, lead-ins, link reading (web_fetch on MTN hosts), voice Alex Oke.
- Dependency audits (`uv audit`, `npm audit`) before every deploy.

## v0.1.0 (8 Nov 2026): chatbot relay POC

### Added
- Python voice gateway (FastAPI) and web client: hands-free voice (ElevenLabs Scribe realtime STT and TTS,
  barge-in), text chat with voice notes and read-aloud, attachments.
- SunCo integration by polling (webhook ready), Logfire tracing, PostHog analytics, access code.
- Deployed on Vercel.
