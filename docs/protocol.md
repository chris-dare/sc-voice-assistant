# Voice session WebSocket protocol (v1)

`GET /v1/voice/session?token=<token>&mode=relay|agent` upgrades to a WebSocket. `mode` defaults to `VOICE_MODE_DEFAULT`; `agent` needs `OPENROUTER_API_KEY` and falls back to `relay` without it. Close code `4401` means the token is invalid or expired.

## Client → gateway

| Frame | Meaning |
| --- | --- |
| binary | Mic audio: 16 kHz, mono, signed 16-bit little-endian PCM. 20 ms frames (640 bytes) recommended. Send continuously while listening; stop while the assistant speaks unless the user barges in. |
| `{"type":"speech_start"}` | Client VAD heard speech. Stops the gateway from starting new speech over the user. |
| `{"type":"speech_end","hangover_ms":500}` | Client VAD heard silence. In batch STT mode this triggers transcription. `hangover_ms` corrects latency metrics. |
| `{"type":"interrupt"}` | Barge-in: the gateway cancels TTS and clears its speech queue. |
| `{"type":"text","text":"..."}` | Typed input inside voice mode. |
| `{"type":"ping"}` | Answered with `{"type":"pong"}`. |

## Gateway → client

| Frame | Meaning |
| --- | --- |
| `ready` | `conversation_id`, `stt_mode` (`realtime` or `batch`), `sample_rate` of TTS audio, `mode` in effect. |
| `state` | `listening`, `thinking` or `speaking`. |
| `transcript_partial` / `transcript_final` | Live captions and the final user utterance. |
| `user_message` / `bot_message` | Messages as stored in SunCo. `bot_message.speech` is what will be spoken (empty when not spoken; always empty in agent mode, where the agent speaks instead). In agent mode `user_message` is what the agent sent Zigi. |
| `agent_text` | Agent mode: one sentence the agent is about to speak, with `turn_id`. Not stored in SunCo. |
| `audio_start` | Audio for `message_id` follows, at `sample_rate`. `hold: true` marks the relay holding phrase; agent hold lines have a `message_id` starting `hold-`. |
| binary | TTS audio: mono PCM16 at `sample_rate`, always whole samples. Arrives faster than real time; buffer and play gaplessly. |
| `audio_end` / `audio_stop` | Audio for a message is complete / playback was cancelled (clear the buffer). |
| `metrics` | Per-turn timings in ms: `stt_ms`, `bot_ms`, `gateway_ms` (total minus bot), `total_ms`, plus `mode` and `route`. Relay adds `post_ms`, `tts_first_byte_ms`; agent adds `llm_ms` (first word or tool call) and `first_audio_ms` (hold line included). |
| `notice` / `error` | `code` + human-readable `text` (`stt_batch_fallback`, `bot_timeout`, `tts_failed`, `send_failed`). |
| `session_end` | `reason`: `idle` or `max_duration`. |
