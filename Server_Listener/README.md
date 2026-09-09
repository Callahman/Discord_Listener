# Server Listener

Remote server component for the Discord voice recording pipeline. Receives WAV files from the Raspberry Pi, transcribes them using Whisper, processes the transcription with an LLM, and pushes the LLM response to a Discord channel for human review.

## Setup

### 1. Install system packages

The following terminal commands install everything the Server Listener needs on
Ubuntu Server Lite (the tower). Run them in an SSH session:

```bash
# Update the package index and upgrade installed packages
sudo apt update && sudo apt upgrade -y

# Install Python, the venv module, git, and ffmpeg (required by openai-whisper
to load audio files; without it every transcription fails)
sudo apt install -y python3 python3-pip python3-venv git ffmpeg

# Verify the versions
python3 --version   # expect 3.9+
ffmpeg -version     # expect an FFmpeg version
```

> **Note:** `ffmpeg` is a hard dependency of `openai-whisper` — Whisper shells
> out to it to decode WAV files. If it is not installed, `transcribe_audio()`
> will raise on every file.

### 2. Clone the repository

The repo is shared by the Pi and the tower. Clone it to `~/discord_listener`
so the repo-relative directory defaults in `server.py` resolve correctly:

```bash
mkdir -p ~/discord_listener
cd ~/discord_listener
git clone <your-repo-url> .

# Create the runtime directories the server uses (the code also creates these
# on startup, but creating them up front makes the layout explicit).
mkdir -p ~/discord_listener/Server_Listener/incoming
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
- `OPENAI_API_KEY` — your OpenAI API key

Optional values:
- `WHISPER_MODEL_SIZE` — the size of the Whisper model to use (defaults to `base`)
- `OPENAI_MODEL` — the OpenAI model to use for LLM processing (defaults to `gpt-4o-mini`)
- `LLM_SYSTEM_PROMPT` — the system prompt sent to the LLM (defaults to a summarization prompt)
- `MAX_RETRIES` — maximum retries before a file is quarantined (defaults to `3`)
- `INCOMING_DIR` — the directory where incoming WAV files are placed (defaults to `discord_listener/Server_Listener/incoming`, repo-relative)
- `FAILED_DIR` — the directory where files that failed after MAX_RETRIES are moved to (defaults to `discord_listener/Server_Listener/failed`, repo-relative)
- `LOG_DIR` — the directory where the JSONL activity log is written (defaults to `discord_listener/Server_Listener/logs`, repo-relative)
- `AUDIO_ARCHIVE_DIR` — the directory where a copy of each processed WAV is archived (defaults to `discord_listener/Server_Listener/archive`, repo-relative)
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
Description=Discord Voice Transcription & LLM Processing Server
After=network.target

[Service]
Type=simple
# Use the actual tower user and paths that match your deployment layout.
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

1. **File Watcher** — The server monitors the `INCOMING_DIR` directory for new WAV files (5-second polling).
2. **Transcription** — When a new WAV file is detected, the server transcribes it using Whisper (model loaded once at startup, `fp16=False` for CPU-only towers).
3. **LLM Processing** — The transcription is sent to an LLM for processing using a configurable system prompt.
4. **Push to Discord** — The LLM response is pushed into the specified Discord channel using a persistent Discord client (no per-message login/logout). The Discord client is built on **py-cord** (the actively-maintained fork of discord.py), aliased as `discord` in the code.
5. **Retry & Quarantine** — If any step fails, the file remains in `INCOMING_DIR` for retry. After `MAX_RETRIES` failures, the file is moved to `FAILED_DIR` to prevent infinite retry loops.
6. **Cleanup** — After the full pipeline succeeds, a copy of the WAV file is archived to `AUDIO_ARCHIVE_DIR` with a timestamp suffix, the original is removed from `INCOMING_DIR`, and the archive is pruned to stay under `AUDIO_RETENTION_GB` (oldest files deleted first).

> **Note:** The `PROCESSED_DIR` directory is no longer used. Processed WAV files are archived to `AUDIO_ARCHIVE_DIR` instead. If you have an existing deployment with files in `PROCESSED_DIR`, you can leave that directory in place — it will simply not receive new files.
7. **Activity Log** — Every pipeline event (success or failure) is appended to `LOG_DIR/activity.jsonl` for review and debugging. The log is rotated automatically (5 MB × 5 backups) so it does not grow unbounded.

## Troubleshooting

- **No WAV files detected** — Verify the `INCOMING_DIR` path in `.env` is correct and that the Pi is successfully transferring WAV files to this directory. The default is repo-relative (`discord_listener/Server_Listener/incoming`); ensure the repo is cloned to `~/discord_listener` on the tower so the path resolves to the directory the Pi SCPs into. The Pi uses atomic SCP (writes a `.part` file, then renames to `.wav`), so only complete `.wav` files are processed.
- **Transcription failing** — Verify the Whisper model is installed and that the WAV files are in the correct format (48kHz, 16-bit, mono). Check `FAILED_DIR` for quarantined files.
- **LLM processing failing** — Verify the `OPENAI_API_KEY` is correct and that you have sufficient API credits.
- **Discord push failing** — Verify the `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are correct and that the bot has `Send Messages` permission in the target channel.
- **Files stuck in retry loop** — Files are quarantined to `FAILED_DIR` after `MAX_RETRIES` failures. Inspect the `activity.jsonl` log to diagnose the root cause.
- **Archive growing too large** — The archive is capped at `AUDIO_RETENTION_GB` (default 5 GB). If you need more or less retention, change that value in `.env` and restart the service. A 1-minute 48 kHz 16-bit mono WAV is ~5.7 MB, so 5 GB retains roughly 14.5 hours of audio.
- **Tower not waking (Wake-on-LAN)** — WoL must be enabled in the tower's BIOS/UEFI for the NIC, and the NIC must stay powered in sleep. Verify with `ethtool -s <iface> wol g` after boot. The Pi sends a broadcast WoL packet, so both devices must be on the same L2 segment (same switch/VLAN).
