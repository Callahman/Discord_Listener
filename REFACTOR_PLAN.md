# Discord Listener Refactor Plan

> **Status:** Verified against the repo on 2026-09-10. This plan reflects the actual files, dependencies, and config in `D:\LLM\Discord_Listener`. Discrepancies found during verification are called out inline and corrected below.

## 1. Current State

### Repo Layout (verified)

```
Discord_Listener/
├── DISCORD_BOT_OUTLINE.md        # original design doc (Pi voice bot)
├── REFACTOR_PLAN.md              # this file
├── Pi_Listener/
│   ├── __init__.py
│   ├── bot.py                    # Pi voice bot (931 lines)
│   ├── README.md
│   ├── requirements.txt          # py-cord[voice]>=2.5.0, python-dotenv>=1.0.0
│   └── .env.example
├── Server_Listener/
│   ├── __init__.py
│   ├── server.py                 # tower file-watcher + Whisper + LLM + Discord push (471 lines)
│   ├── README.md
│   ├── requirements.txt          # py-cord>=2.5.0, python-dotenv, openai, openai-whisper
│   └── .env.example
└── .venv-pi/                     # Pi virtualenv (not part of the project code)
```

### Pi Bot (`Pi_Listener/bot.py`) — verified components

- `discord.Client` with `intents.default()` + `intents.members = True` + `intents.presences = True`
- `CustomVoiceClient(discord.voice.VoiceClient)` — overrides `self.loop = asyncio.get_running_loop()` (fixes a py-cord loop-mismatch on Python 3.13) and wraps `start_listening()`
- `on_ready` → `_do_voice_connect(channel)` (guarded by `_voice_connecting` flag)
- `vc.start_listening()` + `WaveSink` (records incoming audio into `sink.audio_data[user_id]`, a `BytesIO` of decoded PCM)
- `on_member_speaking_state_update(member, ssrc, state)` — voice-gateway speaking op (op 5); `is_speaking = bool(int(state) & 1)`
- `on_voice_state_update(member, before, after)` — leave/move/mute/deaf; includes a safety-net `start_recording` on join
- `start_recording(member)` / `stop_recording(member)` — track `speaking_users` dict; `stop_recording` reads `sink.audio_data[member.id].file.getvalue()` and writes a WAV (RIFF header) to `RECORDINGS_DIR` **synchronously on the event loop**
- `_flush_user_buffer(user_id)` — same WAV write for users not in cache
- `get_next_wav_path()` — atomic `O_CREAT|O_EXCL` claim of `recording_N.wav`
- `send_wav_file(wav_file, username)` — `ssh_reachable` / `send_wol` / `wait_for_ssh` (up to 300 s) / `scp_to_tower`
- `scp_to_tower(wav_file, upload_dir)` — atomic: scp to `.part` → ssh `mv` to `.wav`
- `_spawn_send(wav_file, username)` — `asyncio.to_thread(send_wav_file)` with `_inflight_sends` dedup
- `retry_unsent_wavs()` — every 60 s re-sends `.wav` older than 5 min
- `diagnostic_audio_report()` — every 10 s logs `len(entry.file.getvalue())` per user (full buffer copy on the loop)
- `ssh_session_manager()` — long-lived `ssh sleep infinity` keepalive (ACTIVE/GRACE/CLOSED state machine)
- `voice_reconnect_loop()` — every 30 s rejoin if `channel.voice_client is None`
- `cleanup_inactive_users()` — every 5 s stops recording for users idle > 10 s
- DAVE detection via `vc.is_dave_connection()` after connect

### Tower (`Server_Listener/server.py`) — verified components

- `discord.Client` with `intents.default()` (text only; `message_content` deliberately NOT enabled)
- `watch_incoming_directory()` — polls `INCOMING_DIR` every 5 s for `.wav` files
- `transcribe_audio(wav_file)` — Whisper (`fp16=False`), via `asyncio.to_thread`
- `process_with_llm(transcription)` — Ollama via `openai.OpenAI(api_key="ollama", base_url=f"{OLLAMA_BASE_URL}/v1")`, via `asyncio.to_thread`
- `push_to_discord(llm_response)` — chunked `channel.send()` (2000-char limit)
- `_enforce_audio_retention()` — prunes `AUDIO_ARCHIVE_DIR` to `AUDIO_RETENTION_GB`
- Retry tracking (`retry_counts`, `seen_filenames`, `_prune_tracking`)
- Activity log (`log_activity` → `LOG_DIR/activity.jsonl`, rotating 5 MB × 5)
- Preloads the Whisper model on a worker thread at startup

### Data Flow (current)

1. User enters VC on the Discord server
2. Pi bot is already in the VC (connected on `on_ready`)
3. User speaks → voice-gateway speaking op → `on_member_speaking_state_update`
4. `start_recording(member)` → adds to `speaking_users`
5. `WaveSink` records audio into `sink.audio_data[user_id]`
6. User stops → `stop_recording(member)` → reads `sink.audio_data[user_id].file.getvalue()`
7. Writes WAV (RIFF header) to `RECORDINGS_DIR` on the SD card (**blocking disk I/O on the event loop**)
8. `_spawn_send` → `asyncio.to_thread(send_wav_file)`
9. `send_wav_file` → `ssh_reachable` / `send_wol` / `wait_for_ssh` / `scp_to_tower`
10. Tower's `watch_incoming_directory` polls every 5 s, finds the new `.wav`
11. `transcribe_audio` (Whisper) → `process_with_llm` (Ollama) → `push_to_discord`
12. Tower archives the WAV, removes from incoming, enforces retention

### Known Issues (verified)

1. **Pi event-loop starvation (root cause of the crash).** The Pi's OS does not schedule the asyncio loop thread promptly under load/throttling, so Discord heartbeats are delayed ~47 s. This trips py-cord's "heartbeat blocked" warnings and makes the 60 s voice-connect timeout fire, dropping the bot. It reproduces even when the bot is **alone** in the VC (no audio pipeline running), proving it is a Pi scheduling problem, not a code problem. The crash traceback shows the loop in `selectors.poll()` (its idle wait), not inside a CPU-bound call.
2. **WAV disk writes block the event loop.** `stop_recording()` and `_flush_user_buffer()` call `entry.file.getvalue()` (full in-memory copy) and write a multi-MB file to the SD card, all synchronously inside async handlers.
3. **Diagnostic buffer copies.** `diagnostic_audio_report()` calls `len(entry.file.getvalue())` every 10 s, re-copying the whole `BytesIO` each cycle.
4. **The Pi runs a demanding voice pipeline.** The voice pipeline demands a responsive event loop for the 60 s handshake, continuous UDP socket management, and tight heartbeat-ACK windows. A throttling Pi will eventually drop it.
5. **Stale docs.** `DISCORD_BOT_OUTLINE.md` and both READMEs still describe the Pi as the voice recorder and reference `get_user_audio(user_id)` (which is broken in this py-cord version) and `CustomVoiceClient._handle_speaking` (which was removed in py-cord 2.8.1). These docs need updating as part of this refactor.

---

## 2. Future State

### Architecture

```
[Pi]  Pi_Listener/bot.py  (text-only, lightweight)
  - discord.Client (intents.default(), no voice, no members/presences)
  - on_voice_state_update → detect user entering target VC
  - on_message → detect user sending message in target text channel
  - On either trigger:
      - send_wol(WOL_MAC_ADDRESS)  (wake the tower)
      - Optionally post a system message in the text channel
        ("Server is waking up, joining voice in a few seconds")
      - Optionally open a lightweight SSH keepalive so the tower
        knows activity is ongoing (reuse ssh_session_manager)
  - No CustomVoiceClient, no WaveSink, no start_listening
  - No SCP, no WAV packaging, no retry_unsent_wavs
  - No diagnostic_audio_report, no voice_reconnect_loop, no cleanup_inactive_users

[Tower]  Server_Listener/server.py  (voice + text, full pipeline)
  - discord.Client (intents.default() + members + presences)
  - Voice bot (moved from Pi's bot.py):
      - CustomVoiceClient (subclass of discord.voice.VoiceClient)
      - Joins voice channel on on_ready (or on trigger)
      - start_listening() + WaveSink
      - on_member_speaking_state_update (per-user start/stop)
      - on_voice_state_update (leave/move/mute/deaf)
      - stop_recording() → reads sink.audio_data → writes WAV (via asyncio.to_thread)
      - voice_reconnect_loop()
      - diagnostic_audio_report()
      - cleanup_inactive_users()
  - Text channel reader (new):
      - on_message → read user's message in target text channel
      - Pass text directly to LLM (no transcription needed)
  - Existing pipeline (unchanged):
      - transcribe_audio() → Whisper (for voice recordings)
      - process_with_llm() → Ollama via OpenAI client
      - push_to_discord() → chunked channel.send()
      - _enforce_audio_retention()
```

### Data Flow (future)

**Voice path:**

1. User enters VC on the Discord server
2. Pi bot (text-only) detects `on_voice_state_update` → user entered target VC
3. Pi bot WOLs the tower
4. Tower wakes, logs into Discord, joins the VC (voice bot)
5. User speaks → voice-gateway speaking op → `on_member_speaking_state_update`
6. `start_recording(member)` → adds to `speaking_users`
7. `WaveSink` records audio into `sink.audio_data[user_id]`
8. User stops → `stop_recording(member)`
9. `stop_recording` reads `sink.audio_data[user_id].file.getvalue()`
10. Writes WAV to local tower disk (fast storage, via `asyncio.to_thread`)
11. `transcribe_audio` (Whisper) → `process_with_llm` (Ollama) → `push_to_discord`
12. Tower archives the WAV, enforces retention

**Text path (no voice needed):**

1. User sends a message in the target text channel
2. Pi bot (text-only) detects `on_message` → user sent message in target channel
3. Pi bot WOLs the tower
4. Tower wakes, logs into Discord
5. Tower reads the user's message from the text channel (no voice join needed)
6. `process_with_llm` (Ollama) → `push_to_discord`
7. No transcription, no VC join, no audio pipeline

### What Changes

| | Current | Proposed |
|---|---|---|
| Pi Discord bot | Voice + text (heavy) | Text only (light) |
| Pi voice pipeline | Yes (the thing that breaks) | **No** |
| Pi role | Record, package, WOL, send | Watch for trigger, WOL server |
| Server Discord bot | Text only (send response) | Voice + text (join VC, record, read chat) |
| Server role | Wake, accept, transcribe, LLM, respond | Wake, join VC, record/transcribe or pull text, LLM, respond |
| Text channel input | Not supported | **Yes** (user types → server reads → LLM → respond, no voice needed) |

### What Stays the Same

- The voice recording code (`CustomVoiceClient`, `WaveSink`, speaking handlers, DAVE detection) — moves from Pi to tower, not rewritten
- The transcription → LLM → response pipeline on the tower — unchanged
- The WOL / SSH / wait_for_ssh logic — the Pi keeps the WOL trigger; the tower handles its own session lifecycle
- The archive / retention logic on the tower — unchanged

---

## 3. Reason / Need for Future State

### The Pi Cannot Sustain a Voice Connection

The Pi's problem is that it cannot sustain a **voice** connection. The voice pipeline demands a responsive event loop for the 60 s handshake, continuous UDP socket management, and tight heartbeat-ACK windows. A Pi that throttles or has I/O wait will eventually drop it, no matter how carefully you write the code. The crash traceback shows the loop sitting in `selectors.poll()` (its idle wait) for ~47 s — the OS is not handing it CPU time. This is a Pi scheduling problem, not a code problem.

### A Text-Only Gateway Is Trivially Light

A text-only Discord gateway sends one heartbeat every ~41 s and tolerates significant delay before dropping. The Pi can maintain that even under the load that kills the voice pipeline. So the Pi's role shrinks to "watch for activity → WOL server," which is trivially light.

### The Tower Has the Headroom

The tower is the machine with the CPU headroom, thermal margin, and fast storage to run the voice pipeline without the scheduling starvation seen on the Pi. Moving the voice pipeline to the tower eliminates the root cause entirely.

### Text Channel Fallback Is a Reliability Win

If the user just types a message, the tower doesn't need to do any voice recording at all. It reads the text, passes it to the LLM, responds. No VC join, no audio pipeline, no boot-window problem. This makes the system more robust against the tower's boot time and the Pi's hardware limitations.

### The Tradeoff

The tower bot joins the VC **after** boot. The tower takes some time to wake, log into Discord, and join the voice channel. If the user enters the VC and immediately starts talking, the first few seconds of audio are lost. In practice this is probably fine if the user's pattern is "enter VC → talk in discrete utterances → wait for response." The tower bot is in the VC within a few seconds of the user entering, so it catches the bulk of the conversation. If the very first words matter, the Pi can post a system message in the text channel ("Server is waking up, joining voice in a few seconds") so the user knows to pause briefly.

---

## 4. How to Implement

### Phase 1: Pi Bot — Strip to Text-Only Trigger

**File: `Pi_Listener/bot.py`**

1. **Remove voice imports and classes:**
   - Remove `import discord.voice`
   - Remove `from discord.sinks import WaveSink`
   - Remove `import wave`
   - Remove `class CustomVoiceClient(discord.voice.VoiceClient)` (lines 266–305)

2. **Remove voice-related state and functions:**
   - Remove `speaking_users = {}` (line 74)
   - Remove `get_next_wav_path()` (lines 80–95)
   - Remove `scp_to_tower()` (lines 133–174)
   - Remove `send_wav_file()` (lines 183–213)
   - Remove `_spawn_send()` (lines 216–240)
   - Remove `_inflight_sends` (line 180)
   - Remove `_flush_user_buffer()` (lines 308–376)
   - Remove `start_recording()` (lines 499–512)
   - Remove `stop_recording()` (lines 514–599)
   - Remove `on_member_speaking_state_update` (lines 453–497)
   - Remove `_do_voice_connect()` (lines 608–654)
   - Remove `voice_reconnect_loop()` (lines 657–671)
   - Remove `diagnostic_audio_report()` (lines 688–740)
   - Remove `retry_unsent_wavs()` (lines 743–774)
   - Remove `cleanup_inactive_users()` (lines 674–685)
   - Remove `_voice_connecting` (line 606)

3. **Simplify intents:**
   - Remove `intents.members = True` and `intents.presences = True`
   - Keep `intents = discord.Intents.default()`

4. **Rewrite `on_ready`:**
   - Remove voice connect logic
   - Just log "Logged in as {bot.user}"

5. **Rewrite `on_voice_state_update`:**
   - Detect user entering target VC
   - On entry: `send_wol(WOL_MAC_ADDRESS)`, optionally post system message
   - No recording logic

6. **Add `on_message`:**
   - Detect user sending message in target text channel (`TEXT_CHANNEL_ID`)
   - On message: `send_wol(WOL_MAC_ADDRESS)`

7. **Keep:**
   - `send_wol()` (lines 98–111)
   - `ssh_reachable()` (lines 114–120)
   - `wait_for_ssh()` (lines 123–130)
   - `ssh_session_manager()` (lines 865–899) — optional, for keeping tower awake during activity

8. **Simplify `run_bot`:**
   - Remove `cleanup_task`, `reconnect_task`, `retry_task`, `diagnostic_task`
   - Keep `session_task` (optional)

9. **Update config:**
   - Remove `RECORDINGS_DIR` (line 59)
   - Remove `TOWER_UPLOAD_DIR` (line 58)
   - Keep `TOWER_HOST`, `WOL_MAC_ADDRESS`, `TOWER_SSH_USER`, `TOWER_SSH_KEY`
   - Add `TEXT_CHANNEL_ID` (new, for the text channel trigger)

10. **Update `Pi_Listener/requirements.txt`:**
    - Remove the `[voice]` extra: change `py-cord[voice]>=2.5.0` to `py-cord>=2.5.0`
    - The Pi no longer needs the voice dependencies (opus, etc.)

### Phase 2: Tower Bot — Add Voice + Text Channel Reader

**File: `Server_Listener/server.py`**

1. **Add voice imports:**
   - `import discord.voice`
   - `from discord.sinks import WaveSink`
   - `import wave`

2. **Add voice intents:**
   - `intents.members = True`
   - `intents.presences = True`

3. **Move voice code from Pi's `bot.py` to tower's `server.py`:**
   - `CustomVoiceClient` class
   - `speaking_users` dict
   - `get_next_wav_path()`
   - `start_recording()` / `stop_recording()`
   - `on_member_speaking_state_update`
   - `on_voice_state_update` (voice-specific parts)
   - `_do_voice_connect()`
   - `voice_reconnect_loop()`
   - `diagnostic_audio_report()`
   - `cleanup_inactive_users()`

4. **Fix WAV writes to use `asyncio.to_thread`:**
   - In `stop_recording()`: wrap the WAV write in `asyncio.to_thread(_write_wav, ...)`
   - In `_flush_user_buffer()`: same
   - Create `_write_wav(audio_data, username)` function that does the RIFF header logic + file write

5. **Fix diagnostic buffer copies:**
   - In `diagnostic_audio_report()`: track byte counts incrementally or use `entry.file.tell()` instead of `getvalue()`

6. **Add text channel reader:**
   - Add `on_message` handler
   - On message in target text channel (`TEXT_CHANNEL_ID`): read the text, pass to LLM, respond
   - No voice join needed for text path

7. **Add config:**
   - `VOICE_CHANNEL_ID` (new, for the voice channel the tower joins)
   - `TEXT_CHANNEL_ID` (new, for the text channel the tower reads)
   - `RECORDINGS_DIR` (new, local to tower for voice recordings)

8. **Simplify `run_server`:**
   - Add voice-related tasks: `reconnect_task`, `diagnostic_task`, `cleanup_task`
   - The existing `watch_incoming_directory` task can be kept (for voice recordings written to local disk) or replaced with direct processing (no SCP needed, WAV is written locally)

9. **Remove SCP logic:**
   - The tower no longer receives WAV files via SCP. The voice bot writes WAV files locally.
   - The tower's `watch_incoming_directory` can be simplified to just process local WAV files (or removed entirely if the voice bot processes them directly)

10. **Update `Server_Listener/requirements.txt`:**
    - Add the `[voice]` extra: change `py-cord>=2.5.0` to `py-cord[voice]>=2.5.0`
    - The tower now needs the voice dependencies (opus, etc.)

### Phase 3: Update `.env` Files

**Pi `.env` (`Pi_Listener/.env.example`):**
- Remove: `TOWER_UPLOAD_DIR` (line 25)
- Add: `TEXT_CHANNEL_ID`
- Keep: `DISCORD_BOT_TOKEN`, `DISCORD_VOICE_CHANNEL_ID`, `TOWER_HOST`, `WOL_MAC_ADDRESS`, `TOWER_SSH_USER`, `TOWER_SSH_KEY`

**Tower `.env` (`Server_Listener/.env.example`):**
- Add: `DISCORD_VOICE_CHANNEL_ID`, `TEXT_CHANNEL_ID`, `RECORDINGS_DIR`
- Keep: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `WHISPER_MODEL_SIZE`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `LLM_SYSTEM_PROMPT`, `MAX_RETRIES`, `INCOMING_DIR`, `FAILED_DIR`, `LOG_DIR`, `AUDIO_ARCHIVE_DIR`, `AUDIO_RETENTION_GB`

### Phase 4: Update Docs

The following docs are stale and need updating to reflect the new architecture:

- `DISCORD_BOT_OUTLINE.md` — still describes the Pi as the voice recorder. Update to describe the Pi as a text-only trigger and the tower as the voice recorder.
- `Pi_Listener/README.md` — still describes the Pi voice bot. Update to describe the Pi text-only trigger. Remove references to `libopus0`, `WaveSink`, `start_listening`, `get_user_audio`, `CustomVoiceClient._handle_speaking`, and the SCP transfer logic.
- `Server_Listener/README.md` — still describes the tower as a file-watcher only. Update to describe the tower as the voice recorder + text channel reader + transcription/LLM pipeline. Add references to `libopus0`, `WaveSink`, `start_listening`, and the voice intents.

### Phase 5: Test

1. **Pi text-only test:**
   - Start Pi bot
   - Verify it logs in and stays connected (no voice)
   - Enter VC → verify Pi WOLs the tower
   - Send text message → verify Pi WOLs the tower

2. **Tower voice test:**
   - Start tower bot
   - Verify it joins the VC
   - Speak → verify WAV files are written locally
   - Verify transcription → LLM → Discord response

3. **Tower text test:**
   - Start tower bot
   - Send text message in target channel
   - Verify LLM response (no voice join needed)

4. **End-to-end test:**
   - Pi bot detects VC entry → WOL tower
   - Tower wakes, joins VC, records, transcribes, responds
   - Pi bot detects text message → WOL tower
   - Tower wakes, reads text, responds

### Implementation Order

1. **Phase 1** (Pi) — do first, since it's a deletion/simplification. The Pi bot becomes a lightweight text-only trigger.
2. **Phase 2** (Tower) — do second, since it's a move + enhancement. The voice code moves from Pi to tower, and the text channel reader is added.
3. **Phase 3** (`.env`) — do alongside Phases 1 and 2.
4. **Phase 4** (Docs) — do alongside Phases 1 and 2.
5. **Phase 5** (Test) — do last, after both bots are updated.

### Risk Mitigation

- **Boot window:** The tower takes time to wake and join the VC. The Pi can post a system message in the text channel ("Server is waking up, joining voice in a few seconds") so the user knows to pause briefly.
- **DAVE/E2EE:** The tower's voice bot should keep the DAVE detection from the Pi's `bot.py`. If the channel is a DAVE call, log a warning.
- **Tower load:** The tower now runs both the voice pipeline and the transcription/LLM pipeline. If the tower is under heavy load, the voice pipeline may still starve. Monitor with `uptime` / `top` on the tower.
- **SD card on Pi:** The Pi no longer writes WAV files to the SD card, so the I/O wait problem is eliminated.
- **Stale docs:** The docs reference `get_user_audio(user_id)` (broken in this py-cord version) and `CustomVoiceClient._handle_speaking` (removed in py-cord 2.8.1). Update them to reflect the actual code (direct `sink.audio_data` access and `on_member_speaking_state_update`).

### Files to Modify

| File | Changes |
|---|---|
| `Pi_Listener/bot.py` | Strip to text-only trigger (remove ~60% of code) |
| `Pi_Listener/requirements.txt` | Remove `[voice]` extra |
| `Pi_Listener/.env.example` | Remove `TOWER_UPLOAD_DIR`, add `TEXT_CHANNEL_ID` |
| `Pi_Listener/README.md` | Update to describe text-only trigger |
| `Server_Listener/server.py` | Add voice bot + text channel reader (move voice code from Pi, add text reader) |
| `Server_Listener/requirements.txt` | Add `[voice]` extra |
| `Server_Listener/.env.example` | Add `DISCORD_VOICE_CHANNEL_ID`, `TEXT_CHANNEL_ID`, `RECORDINGS_DIR` |
| `Server_Listener/README.md` | Update to describe voice recorder + text reader |
| `DISCORD_BOT_OUTLINE.md` | Update to describe new architecture |

### Files to Create

| File | Purpose |
|---|---|
| `Server_Listener/voice_bot.py` (optional) | Separate module for the voice bot code, to keep `server.py` clean. Import into `server.py`. |

### Estimated Effort

- **Phase 1 (Pi):** ~2 hours (mostly deletion)
- **Phase 2 (Tower):** ~4 hours (move + enhance + fix WAV writes)
- **Phase 3 (.env):** ~30 minutes
- **Phase 4 (Docs):** ~1 hour
- **Phase 5 (Test):** ~2 hours
- **Total:** ~9.5 hours
