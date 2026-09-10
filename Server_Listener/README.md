# Server Listener

Tower component for the Discord voice recording pipeline. Joins the target voice channel, records speakers using py-cord's `start_listening()` API with a `WaveSink`, reads user messages from the target text channel, transcribes voice recordings using Whisper, processes the transcription (or text) with an LLM, and pushes the LLM response to a Discord channel for human review.

## Setup

### 1. Install system packages

The following terminal commands install everything the Server Listener needs on Ubuntu Server Lite (the tower). Run them in an SSH session:

```bash
# Update the package index and upgrade installed packages
sudo apt update && sudo apt upgrade -y

# Install Python, the venv module, git, ffmpeg (required by openai-whisper
to load audio files), and libopus0 (required by py-cord's WaveSink for Opus
decoding)
sudo apt install -y python3 python3-pip python3-venv git ffmpeg libopus0

# Verify the versions
python3 --version   # expect 3.9+
ffmpeg -version     # expect an FFmpeg version
```

> **Note:** `ffmpeg` is a hard dependency of `openai-whisper` — Whisper shells out to it to decode WAV files. `libopus0` is required for py-cord's `WaveSink` Opus decoding; without it audio capture will fail.

### 2. Clone the repository

The repo is shared by the Pi and the tower. Clone it to `~/discord_listener`:

```bash
mkdir -p ~/discord_listener
cd ~/discord_listener
git clone <your-repo-url> .

# Create the runtime directories the server uses (the code also creates these
# on startup, but creating them up front makes the layout explicit).
mkdir -p ~/discord_listener/Server_Listener/recordings
mkdir -p ~/discord_listener/Server_Listener/failed
mkdir -p ~/discord_listener/Server_Listener/logs
mkdir -p ~/discord_listener/Server_Listener/archive
```

### 3. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install Python dependencies

**Important:** On a CPU-only tower, install the CPU-only build of PyTorch *before* installing the rest of the requirements, or you will pull multi-GB CUDA wheels you cannot use. The commands below install the CPU-only PyTorch first, then the project requirements:

```bash
# Install the CPU-only PyTorch build first (avoids multi-GB CUDA wheels)
pip install torch --index-url https://download.pytorch.org/whl/cpu

# Install the rest of the project dependencies
pip install -r requirements.txt

# Verify the key packages
pip show py-cord openai openai-whisper torch | grep -E '^(Name|Version)'
```

### 5. Configure environment variables

Copy the example `.env` file and fill in the values:

```bash
cp .env.example .env
nano .env
```

Required values:
- `DISCORD_BOT_TOKEN` — your Discord bot token from the Developer Portal
- `DISCORD_CHANNEL_ID` — the ID of the Discord channel where LLM responses will be pushed
- `DISCORD_VOICE_CHANNEL_ID` — the ID of the voice channel the tower bot joins to record audio
- `TEXT_CHANNEL_ID` — the ID of the text channel the tower bot reads user messages from

Optional values:
- `RECORDINGS_DIR` — the directory where the voice bot writes WAV files (defaults to `~/discord_listener/Server_Listener/recordings`)
- `WHISPER_MODEL_SIZE` — the size of the Whisper model to use (defaults to `base`)
- `OLLAMA_BASE_URL` — base URL of the Ollama instance (defaults to `http://localhost:11434`)
- `OLLAMA_MODEL` — the model tag exactly as shown by `ollama list` (defaults to `qwen3.5:latest`)
- `LLM_SYSTEM_PROMPT` — the system prompt sent to the LLM (defaults to a summarization prompt)
- `MAX_RETRIES` — maximum retries before a file is quarantined (defaults to `3`)
- `INCOMING_DIR` — the directory where incoming WAV files are placed (defaults to `~/discord_listener/Server_Listener/recordings`)
- `FAILED_DIR` — the directory where files that failed after MAX_RETRIES are moved to (defaults to `~/discord_listener/failed`)
- `LOG_DIR` — the directory where the JSONL activity log is written (defaults to `~/discord_listener/logs`)
- `AUDIO_ARCHIVE_DIR` — the directory where a copy of each processed WAV is archived (defaults to `~/discord_listener/Server_Listener/archive`)
- `AUDIO_RETENTION_GB` — maximum size in GB to retain in the archive; oldest files are deleted first when exceeded (defaults to `5`)

## Running the Server

### Foreground (for testing)

```bash
source venv/bin/activate
python server.py
```

### As a systemd service (for deployment)

Create a file at `/etc/systemd/system/server_listener.service`:

```ini
[Unit]
Description=Discord Voice Recording & LLM Processing Server
After=network.target

[Service]
Type=simple
User=toweruser
WorkingDirectory=/home/toweruser/discord_listener/Server_Listener
ExecStart=/home/toweruser/discord_listener/venv/bin/python /home/toweruser/discord_listener/Server_Listener/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable server_listener
sudo systemctl start server_listener
```

Check status:

```bash
sudo systemctl status server_listener
journalctl -u server_listener -f
```

## How It Works

### Voice Path

1. **Voice Connection** — The tower bot joins the target voice channel on `on_ready` using a `CustomVoiceClient` subclass of `discord.voice.VoiceClient`.
2. **Listening & Detection** — The bot uses py-cord's documented `start_listening()` API with a `WaveSink` to record incoming audio. The `WaveSink` handles DTLS/SRTP decryption and Opus decoding internally, producing 16-bit 48kHz WAV data per user. The bot monitors the voice-gateway speaking hook (op 5) via `on_member_speaking_state_update` for true per-user start/stop transitions.
3. **Recording** — When a user starts speaking, the bot tracks them in `speaking_users`. When they stop speaking (or leave the channel, or are muted/deafened), the bot retrieves their audio from the `WaveSink` via `sink.audio_data` and writes it to a WAV file in `RECORDINGS_DIR` via `asyncio.to_thread` (so disk I/O never blocks the event loop).
4. **File Watcher** — A background coroutine monitors `INCOMING_DIR` (which can be set to `RECORDINGS_DIR`) for new WAV files (5-second polling).
5. **Transcription** — When a new WAV file is detected, the server transcribes it using Whisper (model loaded once at startup, `fp16=False` for CPU-only towers).
6. **LLM Processing** — The transcription is sent to an LLM (Ollama via the OpenAI client) for processing using a configurable system prompt.
7. **Push to Discord** — The LLM response is pushed into the specified Discord channel using a persistent Discord client.
8. **Retry & Quarantine** — If any step fails, the file remains in `INCOMING_DIR` for retry. After `MAX_RETRIES` failures, the file is moved to `FAILED_DIR` to prevent infinite retry loops.
9. **Cleanup** — After the full pipeline succeeds, a copy of the WAV file is archived to `AUDIO_ARCHIVE_DIR` with a timestamp suffix, the original is removed from `INCOMING_DIR`, and the archive is pruned to stay under `AUDIO_RETENTION_GB` (oldest files deleted first).

### Text Path (No Voice Needed)

1. **Text Channel Detection** — The tower bot monitors `on_message` to detect when a user sends a message in the target text channel.
2. **LLM Processing** — The text is sent directly to the LLM (no transcription needed).
3. **Push to Discord** — The LLM response is pushed into the specified Discord channel.

### Voice Reconnection Loop

A background coroutine periodically checks whether the bot is still connected to the voice channel. If the connection is lost (e.g., channel renamed/deleted, network blip, or gateway reconnect), the loop rejoins the channel automatically.

### Diagnostic Audio Report

Every 10 seconds, the bot logs the per-user byte counts in the `WaveSink` while users are speaking. Uses `entry.file.tell()` instead of `getvalue()` to avoid full buffer copies on the event loop.

### Inactivity Cleanup

A background coroutine stops recording for users who have been inactive for more than 10 seconds.

### DAVE (End-to-End Encryption) Caveat

py-cord's `start_listening()` may not capture audio if the voice channel is subject to DAVE (Discord's End-to-End Encryption for voice calls). DAVE is a platform-level feature that cannot be disabled by server owners. The bot detects DAVE at runtime via `vc.is_dave_connection()` and logs a warning after connecting. Test in your specific channel before deployment to confirm that audio capture works.

### Activity Log

Every pipeline event (success or failure) is appended to `LOG_DIR/activity.jsonl` for review and debugging. The log is rotated automatically (5 MB × 5 backups) so it does not grow unbounded.

## Troubleshooting

- **Bot not joining voice channel** — Verify the bot has `Connect`, `Speak`, and `View Channels` permissions in the target voice channel. Verify `DISCORD_VOICE_CHANNEL_ID` in `.env` matches the actual voice channel ID.
- **No audio recorded** — Verify `py-cord[voice]` is installed (`pip install py-cord[voice]`) and the system `libopus0` package is present (`sudo apt install libopus0`). The audio capture is implemented in `voice_bot.py` using py-cord's documented `start_listening()` API with a `WaveSink`. If the voice channel is subject to DAVE (End-to-End Encryption), `start_listening()` may not capture audio — the bot logs a warning via `vc.is_dave_connection()` after connecting.
- **No WAV files detected** — Verify the `INCOMING_DIR` path in `.env` is correct and matches the directory where the voice bot writes WAV files (`RECORDINGS_DIR`). The default is `~/discord_listener/Server_Listener/recordings`.
- **Transcription failing** — Verify the Whisper model is installed and that the WAV files are in the correct format (48kHz, 16-bit, mono). Check `FAILED_DIR` for quarantined files.
- **LLM processing failing** — Verify `OLLAMA_BASE_URL` and `OLLAMA_MODEL` in `.env` are correct and that Ollama is running.
- **Discord push failing** — Verify the `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are correct and that the bot has `Send Messages` permission in the target channel.
- **Files stuck in retry loop** — Files are quarantined to `FAILED_DIR` after `MAX_RETRIES` failures. Inspect the `activity.jsonl` log to diagnose the root cause.
- **Archive growing too large** — The archive is capped at `AUDIO_RETENTION_GB` (default 5 GB). If you need more or less retention, change that value in `.env` and restart the service. A 1-minute 48 kHz 16-bit mono WAV is ~5.7 MB, so 5 GB retains roughly 14.5 hours of audio.
- **No trigger on text message** — Verify the `TEXT_CHANNEL_ID` in `.env` matches the actual text channel ID. The bot only processes messages in that specific channel.
