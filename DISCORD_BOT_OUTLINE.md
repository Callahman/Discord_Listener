# Discord Voice Recording Bot for Raspberry Pi 3 (Raspberry Pi OS)

This document outlines the steps and technical pipeline for creating a Discord bot that joins a pre-specified voice channel, records speakers when they talk, saves the audio as WAV files, and forwards them to a more powerful server for transcription and LLM processing.

## 1. Prerequisites & Setup

### 1.1 Discord Application Setup
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name.
3. Under **Bot**:
    - Click **Reset Token** and copy the token securely (you will need it for the bot).
    - Enable **Privileged Gateway Intents**:
      - `SERVER MEMBERS INTENT` (required for voice state updates)
      - `PRESENCE INTENT` (optional, but helpful for detecting speaking state)
4. Under **OAuth2**:
    - Go to **Generate URL**.
    - Select scope: `bot`.
    - Select bot permissions: `View Channels`, `Connect`, `Speak`, `Use Voice Activity`.
    - Copy the generated URL, open it in your browser, and invite the bot to **your** Discord server.

### 1.2 Bot Permissions Reference

This section provides a reference for the permissions required by this bot. Use this as a checklist when configuring your bot in the Developer Portal.

#### Required Permissions
- [x] View Channels
- [x] Connect
- [x] Speak
- [x] Use Voice Activity

These are the minimum permissions needed for the bot to join a voice channel and record audio. Do not grant additional permissions unless required.

### 1.3 Raspberry Pi OS Preparation
1. Ensure the Pi is running **Raspberry Pi OS** (64-bit recommended for better performance).
2. Update the system:
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```
3. Install required system packages:
    ```bash
    sudo apt install -y python3 python3-pip python3-venv git
    ```
4. Create a project directory:
    ```bash
    mkdir -p ~/discord_listener
    cd ~/discord_listener
    ```

## 2. Python Environment & Dependencies

### 2.1 Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### 2.2 Install Python Packages
```bash
pip install -r requirements.txt
```

Required packages (see `Pi_Listener/requirements.txt`):
- **py-cord** (v2.x): The actively-maintained fork of discord.py. The main library for interacting with the Discord gateway and handling voice. Aliased as `discord` in the code.
- **python-dotenv**: For loading configuration from a `.env` file.

Note: py-cord's `WaveSink` handles DTLS/SRTP decryption and Opus decoding internally, so `opuslib` is no longer a required dependency for the receive path.

## 3. Technical Pipeline Outline

The bot operates in a continuous loop, listening for voice state changes. The full pipeline is as follows:

### 3.1 Connection Phase
1. **Initialize Bot**: Create a `discord.Client` instance with the required intents.
2. **Login & Connect**: Use the bot token to connect to the Discord gateway.
3. **Join Voice Channel**:
    - On `on_ready`, look up the pre-specified voice channel by ID.
    - Use `channel.connect()` to join the channel.
    - The bot must have `Connect` and `Speak` permissions in that channel.
4. **Voice Reconnection Loop**:
    - A background coroutine periodically checks whether the bot is still connected to the voice channel.
    - If the connection is lost (e.g., channel renamed/deleted, network blip, or gateway reconnect), the loop rejoins the channel automatically.
    - This prevents the bot from silently going deaf after a transient failure.

### 3.2 Listening & Detection Phase
1. **Voice State Update Handler**:
    - Implement the `on_voice_state_update` event.
    - This event fires whenever a member joins, leaves, or changes their speaking state in a voice channel.
2. **Speaking Detection**:
    - The bot uses the voice-gateway speaking hook (op 5) via `CustomVoiceClient._handle_speaking` for true per-user start/stop transitions. Discord sends this with the user id and a speaking flag whenever a user's speaking state changes.
    - The `on_voice_state_update` handler covers the cases the speaking hook does not: channel leave, channel-to-channel move, and mute/deaf transitions.
    - **Key Logic**: Track which users are currently speaking. When a user transitions from *not speaking* to *speaking*, start recording. When they transition from *speaking* to *not speaking*, stop recording.
3. **Channel-Leave Handling**:
    - When a user **leaves** the target voice channel, recording stops **immediately** (not just via the idle timer). The handler checks `before.channel` vs `after.channel` before the early-return guard, so the buffered audio is flushed to disk right away.

### 3.3 Recording Phase
1. **Audio Stream Access**:
    - py-cord's `VoiceClient` exposes a documented `start_listening()` API that records incoming audio into a `Sink` object.
    - The bot uses the documented `WaveSink` class, which handles DTLS/SRTP decryption and Opus decoding internally, producing 16-bit 48kHz WAV data per user.
2. **Decoding**:
    - The incoming audio is **Opus**-encoded.
    - py-cord's `WaveSink` handles DTLS/SRTP decryption and Opus decoding internally, so no external decoder is needed.
3. **Buffering**:
    - The `WaveSink` accumulates decoded audio per user in an internal `audio_data` dict.
    - When a user stops speaking, the bot retrieves their audio via `get_user_audio(user_id)`.

### 3.4 Saving Phase
1. **WAV File Creation**:
    - The `WaveSink` produces 16-bit 48kHz WAV data internally.
    - The bot writes the retrieved audio to a WAV file on disk.
2. **File Naming**:
    - Use an incrementing filename so an unsent recording is never overwritten:
      ```
      .../recordings/recording_{N}.wav
      ```
      N starts at 1 and increments while a file with that name already exists. Once a recording is successfully transferred to the tower, the local file is deleted, so the next recording reuses N=1 (or the next free N if a previous transfer is still pending).
3. **Write to Disk**:
    - Save the file to the Raspberry Pi's filesystem (e.g., `~/discord_listener/recordings/`).
    - **Note**: The WAV file is only held for a few seconds before being sent to the remote server. Ensure the disk space is sufficient for temporary storage.
4. **Buffer Flush When User Is Not in Cache**:
    - The idle-cleanup task may need to flush a recording for a user whose object is no longer in the bot's cache (e.g., after a gateway reconnect). In that case, the buffer is flushed directly using the username already stored in the state dict, so no audio is lost.

### 3.5 Sending Phase

The "remote server" is a fixed local-network tower (sometimes on) that the Pi talks to over SSH. The bot keeps a persistent SSH session open while Discord voice activity warrants it (see **Persistent SSH Session Manager** below), so most transfers happen without waking the tower. Wake-on-LAN + wait-for-reachable is the fallback for when the tower is asleep.

1. **Transfer (Primary: SSH/SCP)**:
    - Copy the WAV file to a fixed incoming directory on the tower using `scp` (invoked via `subprocess`).
    - One-time setup: generate an SSH keypair on the Pi and add the public key to the tower's `authorized_keys` so the transfer is non-interactive.
    - The SSH session itself keeps the tower awake during the transfer (the tower stays awake while an SSH session is active and sleeps after ~20 min idle).
    - Example:
      ```python
      import subprocess
      
      def scp_to_tower(wav_file, upload_dir):
          cmd = [
              'scp',
              '-i', TOWER_SSH_KEY,            # Pi's private key
              '-o', 'BatchMode=yes',
              wav_file,
              f"{TOWER_SSH_USER}@{TOWER_HOST}:{upload_dir}/",
          ]
          result = subprocess.run(cmd, timeout=60)
          return result.returncode == 0
      ```
    - **Alternative (documented, not primary): Pi-side Whisper → text to LLM service.** The Pi could transcribe locally and send only the text to a waiting LLM service on the tower. This is impractical on a Raspberry Pi 3 (Whisper `tiny` runs ~10–30× real-time; usable models won't fit), so it's a future option only if the Pi is upgraded.

2. **Wake-on-LAN (Required when tower is asleep)**:
    - The tower is a "sometimes-on" server, so the bot sends a Wake-on-LAN packet (using the tower's MAC) whenever the tower is not already reachable.
    - Example:
      ```python
      import socket
      
      def send_wol(mac_address):
          mac_bytes = bytes.fromhex(mac_address.replace(':', ''))
          packet = b'\xff' * 6 + (mac_bytes * 16)
          sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
          sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
          sock.sendto(packet, ('<broadcast>', 9))
          sock.close()
      ```

3. **Wait for Tower to Be Reachable**:
    - After sending WoL, poll a TCP connect to **SSH port 22** on the tower with retries (e.g., every 5 s, up to ~5 min to cover boot time) before attempting the transfer.
    - Example:
      ```python
      import socket
      import time
      
      def wait_for_ssh(host, timeout=300, interval=5):
          deadline = time.time() + timeout
          while time.time() < deadline:
              try:
                  with socket.create_connection((host, 22), timeout=3):
                      return True
              except OSError:
                  time.sleep(interval)
          return False
      ```

4. **Persistent SSH Session Manager**:
    - A background coroutine maintains the SSH session to the tower, keeping it awake while Discord voice activity warrants it, and applies defined persistence rules after the last activity.
    - **State machine**:
      - **ACTIVE** — ≥1 user in the monitored Discord voice channel. Keep the session alive. If the user is present but **inactive/silent for 20 min** → CLOSED.
      - **GRACE** — No user in the voice channel. Keep the session alive for **5 min** (reconnection buffer). If a user re-enters within 5 min → ACTIVE. If 5 min elapse with no user → CLOSED.
      - **CLOSED** — Terminate the session. The tower sleeps on its own 20-min inactivity rule. Re-awakened via WoL when the next transfer/entry needs it.
    - Because the session manager keeps the tower awake during active sessions, **transfers usually skip WoL/wait** — the tower is already reachable. The WoL→wait path is only the fallback for when the tower is asleep.
    - Example transfer flow:
      ```python
      def transfer(wav_file, upload_dir):
          if ssh_reachable():
              ok = scp_to_tower(wav_file, upload_dir)
          else:
              send_wol(WOL_MAC_ADDRESS)
              if not wait_for_ssh(TOWER_HOST):
                  return False
              ok = scp_to_tower(wav_file, upload_dir)
          return ok
      ```

5. **Cleanup (Delete WAV After Successful Transfer)**:
    - After the WAV file has been successfully transferred to the tower, delete the local file to free up disk space.
    - Example:
      ```python
      import os
      
      if transfer(wav_file, TOWER_UPLOAD_DIR):
          os.remove(wav_file)
          print(f"Deleted local WAV file: {wav_file}")
      ```
    - **Note**: Only delete the file if the transfer was successful (SCP exit code 0). If the transfer fails, keep the file for potential retry.

### 3.6 Transcription & LLM Processing (Remote Server)

The remote server component lives in `Server_Listener/` and runs as a persistent service on the tower. It polls the incoming directory for WAV files, transcribes them, processes the transcription with an LLM, and pushes the result to a Discord channel.

1. **File Watcher**:
    - A background coroutine polls `INCOMING_DIR` every 5 seconds for new WAV files.
    - Each file is processed through the full pipeline (transcription → LLM → Discord push) before being moved to `PROCESSED_DIR`.
    - If any step fails, the file remains in `INCOMING_DIR` for retry on the next poll cycle.
    - After `MAX_RETRIES` (default 3) failures, the file is moved to `FAILED_DIR` to prevent infinite retry loops.
2. **Transcription**:
    - The Whisper model is loaded **once** at startup and reused for all transcriptions (avoids reloading the model per file).
    - `fp16=False` is passed to `model.transcribe()` to avoid floating-point warnings on CPU-only towers.
    - Example:
      ```python
      import whisper
      
      model = whisper.load_model('base')
      result = model.transcribe(wav_file, fp16=False)
      transcription = result['text'].strip()
      ```
3. **LLM Processing**:
    - The transcription is sent to an LLM for processing using a **configurable system prompt** (set via `LLM_SYSTEM_PROMPT` in `.env`).
    - Example:
      ```python
      from openai import OpenAI
      
      client = OpenAI(api_key=OPENAI_API_KEY)
      response = client.chat.completions.create(
          model=OPENAI_MODEL,
          messages=[
              {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
              {'role': 'user', 'content': transcription}
          ]
      )
      llm_response = response.choices[0].message.content.strip()
      ```
4. **Push to Discord**:
    - A **persistent Discord client** is maintained (no per-message login/logout), making pushes faster and rate-limit friendly.
    - The client connects on first use and stays connected for the lifetime of the service.
    - The LLM response is pushed into the Discord server's chat room for human review.
    - Example:
      ```python
      client = get_discord_client()  # persistent, already connected
      channel = client.get_channel(DISCORD_CHANNEL_ID)
      await channel.send(llm_response)
      ```
5. **Activity Log**:
    - Every pipeline event (success or failure) is appended to `LOG_DIR/activity.jsonl` as a structured JSON record.
    - Each record includes the filename, pipeline stage, status, retry count, and (when available) the transcription and LLM response.
    - This log is the primary tool for debugging and reviewing what the pipeline has processed.

**Directory layout on the tower**:
```
~/discord_listener/
├── incoming/    # WAV files arriving from the Pi (via SCP)
├── processed/   # WAV files that completed the full pipeline
├── failed/      # WAV files quarantined after MAX_RETRIES failures
└── logs/
    └── activity.jsonl  # structured pipeline event log
```

## 4. Core Bot Code Structure (Pseudocode)

```python
import py_cord as discord
from py_cord.sinks import WaveSink
import wave
import time
from datetime import datetime

intents = discord.Intents.default()
intents.members = True
intents.presence = True

bot = discord.Client(intents=intents)

# State tracking
speaking_users = {}  # {user_id: {'buffer': bytearray, 'start_time': datetime, 'last_activity': datetime, 'username': str}}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    # Join the pre-specified voice channel
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    await channel.connect()

@bot.event
async def on_voice_state_update(member, before, after):
    # If the user left the target channel, stop recording immediately
    if before.channel and before.channel.id == VOICE_CHANNEL_ID and not after.channel:
        if member.id in speaking_users:
            stop_recording(member)
        return

    # Check if the user is in the target voice channel
    if not after.channel or after.channel.id != VOICE_CHANNEL_ID:
        return

    # Check mute/deaf status
    if after.self_mute or after.deaf:
        if member.id in speaking_users:
            stop_recording(member)
        return

    # Start or update recording
    if member.id not in speaking_users:
        start_recording(member)
    else:
        speaking_users[member.id]['last_activity'] = datetime.now()

    update_user_activity()
    update_user_presence()
```

## 5. Running the Bot as a Service

### 5.1 Create a Systemd Service
Create a file at `/etc/systemd/system/discord_listener.service`:

```ini
[Unit]
Description=Discord Voice Recording Bot
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/discord_listener
ExecStart=/home/pi/discord_listener/venv/bin/python /home/pi/discord_listener/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 5.2 Enable and Start the Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable discord_listener
sudo systemctl start discord_listener
```

### 5.3 Check Status
```bash
sudo systemctl status discord_listener
journalctl -u discord_listener -f
```

## 6. Requirements & Concerns

### 6.1 Requirements
- **Hardware**: Raspberry Pi 3 (or 3B+) with at least 1GB RAM (2GB recommended for smooth operation).
- **OS**: Raspberry Pi OS (64-bit) with Python 3.9+.
- **Network**: Stable internet connection (Discord requires low-latency voice).
- **Permissions**: Bot must be invited to **your** server with `Connect`, `Speak`, and `View Channels` permissions in the target voice channel.
- **Storage**: Sufficient disk space for temporary WAV recordings (a 1-minute 48kHz 16-bit mono WAV is ~5.7 MB).
- **Remote Server**: A more powerful server with Whisper and an LLM API key for transcription and processing.

### 6.2 Concerns & Limitations
- **Opus Decoding Performance**: Decoding Opus in real-time on a Raspberry Pi 3 can be CPU-intensive. The Pi 3 may struggle, leading to dropped frames or high CPU usage. Consider using a more efficient decoder or reducing the sample rate.
- **Latency & Buffering**: Discord's voice stream has inherent latency. Ensure your buffer is large enough to handle network jitter without dropping audio.
- **Multi-Speaker Overlap**: If multiple users speak at the same time, the current pipeline (recording per speaker) may interleave audio. You will need to handle this by either:
  - Recording a combined stream (simpler, but less precise).
  - Implementing per-user audio separation (complex, requires advanced DSP).
- **Token Security**: The bot token is sensitive. Never hardcode it in the script. Use environment variables or a `.env` file (add `.env` to `.gitignore`).
- **Discord ToS**: Recording voice may violate Discord's Terms of Service if done without consent. Ensure all users in the channel are aware and have consented to being recorded.
- **WAV File Size**: WAV files are uncompressed. For long recordings, consider converting to MP3 or OGG to save space (requires `ffmpeg`).
- **Voice Channel Changes**: If the voice channel is deleted or renamed, the bot will lose its connection. The **voice reconnection loop** (see §3.1.4) handles this by automatically rejoining the channel.
- **Remote Server Availability**: The remote server may be sometimes off. Implement Wake-on-LAN logic to wake it up before sending the WAV file. Ensure the server is configured to accept Wake-on-LAN packets.
- **Network Latency**: The latency between the Raspberry Pi and the remote server can affect the overall pipeline. Ensure the network is stable and low-latency.

## 7. Next Steps
1. Set up the Discord application and invite the bot to your server.
2. Prepare the Raspberry Pi OS environment.
3. Implement the core bot code (connection, voice state detection, Opus decoding, WAV saving, and sending to the remote server).
4. Test the bot in a private voice channel.
5. Set up the remote server with Whisper and an LLM API key.
6. Set up the systemd service for persistent operation.
7. Monitor performance and adjust buffer sizes as needed.
