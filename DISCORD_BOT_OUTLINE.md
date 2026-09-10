# Discord Voice Recording Bot for Raspberry Pi 3 (Raspberry Pi OS)

This document outlines the architecture and technical pipeline for a Discord voice recording system. The Raspberry Pi acts as a lightweight text-only trigger that watches for user activity and wakes the tower. The tower handles all voice recording, transcription, and LLM processing.

## 1. Prerequisites & Setup

### 1.1 Discord Application Setup
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name.
3. Under **Bot**:
    - Click **Reset Token** and copy the token securely (you will need it for the bot).
    - Enable **Privileged Gateway Intents**:
      - `SERVER MEMBERS INTENT` (required for voice state updates on the tower)
      - `PRESENCE INTENT` (optional, but helpful for detecting speaking state on the tower)
4. Under **OAuth2**:
    - Go to **Generate URL**.
    - Select scope: `bot`.
    - Select bot permissions: `View Channels`, `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`.
    - Copy the generated URL, open it in your browser, and invite the bot to **your** Discord server.

### 1.2 Bot Permissions Reference

This section provides a reference for the permissions required by this bot. Use this as a checklist when configuring your bot in the Developer Portal.

#### Required Permissions
- [x] View Channels
- [x] Connect
- [x] Speak
- [x] Use Voice Activity
- [x] Send Messages

These are the minimum permissions needed for the tower bot to join a voice channel, record audio, read text messages, and push LLM responses. Do not grant additional permissions unless required.

### 1.3 Raspberry Pi OS Preparation
1. Ensure the Pi is running **Raspberry Pi OS** (64-bit recommended for better performance).
2. Update the system:
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```
3. Install required system packages:
    ```bash
    sudo apt install -y python3 python3-pip python3-venv git ssh
    ```
4. Create a project directory:
    ```bash
    mkdir -p ~/discord_listener
    cd ~/discord_listener
    ```

### 1.4 Tower (Ubuntu Server) Preparation
1. Ensure the tower is running **Ubuntu Server** (20.04+ recommended).
2. Update the system:
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```
3. Install required system packages:
    ```bash
    sudo apt install -y python3 python3-pip python3-venv git ffmpeg libopus0
    ```
4. Create a project directory:
    ```bash
    mkdir -p ~/discord_listener
    cd ~/discord_listener
    ```

## 2. Python Environment & Dependencies

### 2.1 Virtual Environment (Pi and Tower)
```bash
python3 -m venv venv
source venv/bin/activate
```

### 2.2 Install Python Packages (Pi)
```bash
pip install -r Pi_Listener/requirements.txt
```

Required packages (see `Pi_Listener/requirements.txt`):
- **py-cord** (v2.x): The actively-maintained fork of discord.py. The main library for interacting with the Discord gateway. Aliased as `discord` in the code.
- **python-dotenv**: For loading configuration from a `.env` file.

Note: The Pi no longer needs the `[voice]` extra. Voice recording has moved to the tower.

### 2.3 Install Python Packages (Tower)
```bash
# Install the CPU-only PyTorch build first (avoids multi-GB CUDA wheels)
pip install torch --index-url https://download.pytorch.org/whl/cpu

# Install the rest of the project dependencies
pip install -r Server_Listener/requirements.txt
```

Required packages (see `Server_Listener/requirements.txt`):
- **py-cord[voice]** (v2.x): The actively-maintained fork of discord.py with voice support. The main library for interacting with the Discord gateway and handling voice. Aliased as `discord` in the code.
- **python-dotenv**: For loading configuration from a `.env` file.
- **openai**: For interacting with Ollama via the OpenAI client.
- **openai-whisper**: For transcribing voice recordings.
- **torch** (CPU-only): Required by openai-whisper.

Note: py-cord's `WaveSink` handles DTLS/SRTP decryption and Opus decoding internally, so `opuslib` is no longer a required dependency for the receive path. However, the system package `libopus0` is required.

## 3. Technical Pipeline Outline

The system operates in two parts: the Pi (text-only trigger) and the tower (voice + text + LLM). The full pipeline is as follows:

### 3.1 Pi: Text-Only Trigger

The Pi's role is to watch for user activity and wake the tower. It does not record audio.

1. **Connection**:
    - Create a `discord.Client` instance with default intents (no voice, no members, no presences).
    - Use the bot token to connect to the Discord gateway.

2. **Voice Channel Detection**:
    - Implement the `on_voice_state_update` event.
    - Detect when a user enters the target voice channel.
    - On entry, send a Wake-on-LAN packet to the tower.

3. **Text Channel Detection**:
    - Implement the `on_message` event.
    - Detect when a user sends a message in the target text channel.
    - On message, send a Wake-on-LAN packet to the tower.

4. **Optional SSH Keepalive**:
    - A background coroutine can maintain a long-lived SSH session to keep the tower awake while users are present in the voice channel (ACTIVE/GRACE/CLOSED state machine).

### 3.2 Tower: Voice Recording

The tower's role is to join the voice channel, record speakers, and process the audio.

1. **Connection**:
    - Create a `discord.Client` instance with default intents + members + presences.
    - Use the bot token to connect to the Discord gateway.
    - On `on_ready`, look up the target voice channel by ID.
    - Use `channel.connect(cls=CustomVoiceClient)` to join the channel.
    - The bot must have `Connect` and `Speak` permissions in that channel.

2. **CustomVoiceClient**:
    - A subclass of `discord.voice.VoiceClient` that captures incoming audio via the documented `start_listening()` API + `WaveSink`.
    - Overrides `self.loop` with the currently-running loop to fix a py-cord loop-mismatch on Python 3.13.
    - Wraps `start_listening()` to use the internal `WaveSink`.

3. **Listening & Detection**:
    - Implement the `on_member_speaking_state_update` event (voice-gateway speaking op, op 5).
    - This event fires whenever a user's speaking state changes.
    - Track which users are currently speaking in `speaking_users`.
    - When a user transitions from *not speaking* to *speaking*, start recording.
    - When they transition from *speaking* to *not speaking*, stop recording.

4. **Voice State Update Handler**:
    - Implement the `on_voice_state_update` event.
    - This event fires whenever a member joins, leaves, or changes their mute/deaf state in a voice channel.
    - If a user leaves the target channel or moves to another channel, stop recording immediately.
    - If a user is muted or deafened, stop recording if they were speaking.
    - Safety net: if the speaking op never fired for a user, start recording here so we don't miss their audio.

5. **Recording**:
    - The `WaveSink` accumulates decoded audio per user in an internal `audio_data` dict.
    - When a user stops speaking, the bot retrieves their audio from `sink.audio_data` (direct access, since `get_user_audio()` is broken in this py-cord version).
    - The audio is written to a WAV file in `RECORDINGS_DIR` via `asyncio.to_thread` (so disk I/O never blocks the event loop).

6. **WAV File Creation**:
    - The `WaveSink` produces 16-bit 48kHz WAV data internally.
    - The bot writes the retrieved audio to a WAV file on disk.
    - Use an incrementing filename so an unsent recording is never overwritten:
      ```
      .../recordings/recording_{N}.wav
      ```
      N starts at 1 and increments while a file with that name already exists. Once a recording is successfully processed, the local file is deleted, so the next recording reuses N=1 (or the next free N if a previous processing is still pending).

7. **Voice Reconnection Loop**:
    - A background coroutine periodically checks whether the bot is still connected to the voice channel.
    - If the connection is lost (e.g., channel renamed/deleted, network blip, or gateway reconnect), the loop rejoins the channel automatically.
    - This prevents the bot from silently going deaf after a transient failure.

8. **Diagnostic Audio Report**:
    - Every 10 seconds, the bot logs the per-user byte counts in the `WaveSink` while users are speaking.
    - Uses `entry.file.tell()` instead of `getvalue()` to avoid full buffer copies on the event loop.

9. **Inactivity Cleanup**:
    - A background coroutine stops recording for users who have been inactive for more than 10 seconds.

### 3.3 Tower: Text Channel Reader

The tower's role is to read user messages from the target text channel and process them with the LLM.

1. **Text Channel Detection**:
    - Implement the `on_message` event.
    - Detect when a user sends a message in the target text channel.

2. **LLM Processing**:
    - The text is sent directly to the LLM (no transcription needed).

3. **Push to Discord**:
    - The LLM response is pushed into the specified Discord channel.

### 3.4 Tower: Transcription & LLM Processing (Voice Path)

The tower's role is to transcribe voice recordings, process the transcription with an LLM, and push the result to a Discord channel.

1. **File Watcher**:
    - A background coroutine polls `INCOMING_DIR` every 5 seconds for new WAV files.
    - Each file is processed through the full pipeline (transcription → LLM → Discord push) before being archived.
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
    - The LLM is Ollama, accessed via the OpenAI client.
    - Example:
      ```python
      from openai import OpenAI
      
      client = OpenAI(api_key="ollama", base_url=f"{OLLAMA_BASE_URL}/v1")
      response = client.chat.completions.create(
          model=OLLAMA_MODEL,
          messages=[
              {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
              {'role': 'user', 'content': transcription}
          ]
      )
      llm_response = response.choices[0].message.content.strip()
      ```

4. **Push to Discord**:
    - A **persistent Discord client** is maintained (no per-message login/logout), making pushes faster and rate-limit friendly.
    - The client connects on startup and stays connected for the lifetime of the service.
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
├── Server_Listener/
│   ├── recordings/  # WAV files written by the voice bot
│   ├── failed/      # WAV files quarantined after MAX_RETRIES failures
│   ├── logs/
│   │   └── activity.jsonl  # structured pipeline event log
│   └── archive/     # WAV files that completed the full pipeline
```

### 3.5 Wake-on-LAN (Pi)

The Pi sends a Wake-on-LAN packet to the tower when it detects user activity (voice channel entry or text message).

1. **Wake-on-LAN**:
    - The tower is a "sometimes-on" server, so the Pi sends a Wake-on-LAN packet (using the tower's MAC) whenever it detects user activity.
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

2. **Wait for Tower to Be Reachable** (optional, for SSH keepalive):
    - After sending WoL, poll a TCP connect to **SSH port 22** on the tower with retries (e.g., every 5 s, up to ~5 min to cover boot time) before attempting the SSH keepalive.
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

3. **Persistent SSH Session Manager** (optional, for keeping tower awake):
    - A background coroutine maintains the SSH session to the tower, keeping it awake while Discord activity warrants it, and applies defined persistence rules after the last activity.
    - **State machine**:
      - **ACTIVE** — ≥1 user in the monitored Discord voice channel. Keep the session alive.
      - **GRACE** — No user in the voice channel. Keep the session alive for **5 min** (reconnection buffer). If a user re-enters within 5 min → ACTIVE. If 5 min elapse with no user → CLOSED.
      - **CLOSED** — Terminate the session. The tower sleeps on its own inactivity rule. Re-awakened via WoL when the next trigger needs it.

### 3.6 DAVE (End-to-End Encryption) Caveat

py-cord's `start_listening()` may not capture audio if the voice channel is subject to DAVE (Discord's End-to-End Encryption for voice calls). DAVE is a platform-level feature that cannot be disabled by server owners. The tower bot detects DAVE at runtime via `vc.is_dave_connection()` and logs a warning after connecting. Test in your specific channel before deployment to confirm that audio capture works.

## 4. Core Bot Code Structure (Pseudocode)

### 4.1 Pi: Text-Only Trigger

```python
import discord

intents = discord.Intents.default()

bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member, before, after):
    # Detect user entering the target voice channel
    if after.channel and after.channel.id == VOICE_CHANNEL_ID:
        if before.channel is None or before.channel.id != VOICE_CHANNEL_ID:
            send_wol(WOL_MAC_ADDRESS)

@bot.event
async def on_message(message):
    # Detect user sending a message in the target text channel
    if message.channel.id == TEXT_CHANNEL_ID:
        send_wol(WOL_MAC_ADDRESS)
```

### 4.2 Tower: Voice Recording

```python
import discord
import discord.voice
from discord.sinks import WaveSink

intents = discord.Intents.default()
intents.members = True
intents.presences = True

bot = discord.Client(intents=intents)

# State tracking
speaking_users = {}  # {user_id: {'start_time': datetime, 'last_activity': datetime, 'username': str}}

class CustomVoiceClient(discord.voice.VoiceClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sink = WaveSink()
        self.loop = asyncio.get_running_loop()
    
    def start_listening(self, sink=None, callback=None):
        if sink is None:
            sink = self._sink
        super().start_listening(sink, callback)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    await channel.connect(cls=CustomVoiceClient)

@bot.event
async def on_member_speaking_state_update(member, ssrc, state):
    # Detect user speaking state changes
    is_speaking = bool(int(state) & 1)
    if is_speaking:
        if member.id not in speaking_users:
            start_recording(member)
    else:
        if member.id in speaking_users:
            stop_recording(member)

@bot.event
async def on_voice_state_update(member, before, after):
    # Handle leave/move/mute/deaf
    if before.channel and before.channel.id == VOICE_CHANNEL_ID and \
            (after.channel is None or after.channel.id != VOICE_CHANNEL_ID):
        if member.id in speaking_users:
            stop_recording(member)
        return
    
    if not after.channel or after.channel.id != VOICE_CHANNEL_ID:
        return
    
    if after.self_mute or after.deaf:
        if member.id in speaking_users:
            stop_recording(member)
        return
    
    # Safety net: start recording if the speaking op never fired
    if member.id not in speaking_users:
        start_recording(member)
```

### 4.3 Tower: Text Channel Reader

```python
@bot.event
async def on_message(message):
    # Detect user sending a message in the target text channel
    if message.channel.id == TEXT_CHANNEL_ID:
        text = message.content.strip()
        if text:
            llm_response = await asyncio.to_thread(process_with_llm, text)
            await push_to_discord(llm_response)
```

## 5. Running the Bots as Services

### 5.1 Pi: Systemd Service
Create a file at `/etc/systemd/system/discord_listener.service`:

```ini
[Unit]
Description=Discord Text-Only Trigger Bot
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/discord_listener/Pi_Listener
ExecStart=/home/pi/discord_listener/venv/bin/python /home/pi/discord_listener/Pi_Listener/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable discord_listener
sudo systemctl start discord_listener
```

Check status:

```bash
sudo systemctl status discord_listener
journalctl -u discord_listener -f
```

### 5.2 Tower: Systemd Service
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

## 6. Requirements & Concerns

### 6.1 Requirements
- **Hardware (Pi)**: Raspberry Pi 3 (or 3B+) with at least 1GB RAM (2GB recommended for smooth operation).
- **Hardware (Tower)**: A more powerful server with CPU headroom, thermal margin, and fast storage to run the voice pipeline reliably.
- **OS (Pi)**: Raspberry Pi OS (64-bit) with Python 3.9+.
- **OS (Tower)**: Ubuntu Server (20.04+) with Python 3.9+.
- **Network**: Stable internet connection (Discord requires low-latency voice on the tower).
- **Permissions**: Bot must be invited to **your** server with `Connect`, `Speak`, `View Channels`, and `Send Messages` permissions in the target voice and text channels.
- **Storage (Tower)**: Sufficient disk space for WAV recordings (a 1-minute 48kHz 16-bit mono WAV is ~5.7 MB).
- **LLM (Tower)**: Ollama running on the tower or accessible via the network.

### 6.2 Concerns & Limitations
- **Pi Cannot Sustain Voice**: The Pi's OS does not schedule the asyncio loop thread promptly under load/throttling, so Discord heartbeats are delayed. This trips py-cord's "heartbeat blocked" warnings and makes the 60 s voice-connect timeout fire, dropping the bot. The Pi's role is now text-only, which is trivially light.
- **Tower Boot Window**: The tower takes time to wake, log into Discord, and join the voice channel. If the user enters the VC and immediately starts talking, the first few seconds of audio are lost. In practice this is probably fine if the user's pattern is "enter VC → talk in discrete utterances → wait for response." The Pi can post a system message in the text channel ("Server is waking up, joining voice in a few seconds") so the user knows to pause briefly.
- **DAVE/E2EE**: The tower's voice bot should keep the DAVE detection from the Pi's `bot.py`. If the channel is a DAVE call, log a warning.
- **Tower Load**: The tower now runs both the voice pipeline and the transcription/LLM pipeline. If the tower is under heavy load, the voice pipeline may still starve. Monitor with `uptime` / `top` on the tower.
- **Multi-Speaker Overlap**: If multiple users speak at the same time, the current pipeline (recording per speaker) may interleave audio. You will need to handle this by either:
  - Recording a combined stream (simpler, but less precise).
  - Implementing per-user audio separation (complex, requires advanced DSP).
- **Token Security**: The bot token is sensitive. Never hardcode it in the script. Use environment variables or a `.env` file (add `.env` to `.gitignore`).
- **Discord ToS**: Recording voice may violate Discord's Terms of Service if done without consent. Ensure all users in the channel are aware and have consented to being recorded.
- **WAV File Size**: WAV files are uncompressed. For long recordings, consider converting to MP3 or OGG to save space (requires `ffmpeg`).
- **Voice Channel Changes**: If the voice channel is deleted or renamed, the tower bot will lose its connection. The **voice reconnection loop** handles this by automatically rejoining the channel.
- **Tower Availability**: The tower may be sometimes off. The Pi sends Wake-on-LAN packets to wake it up when user activity is detected. Ensure the tower is configured to accept Wake-on-LAN packets.
- **Network Latency**: The latency between the Pi and the tower can affect the overall pipeline. Ensure the network is stable and low-latency.

## 7. Next Steps
1. Set up the Discord application and invite the bot to your server.
2. Prepare the Raspberry Pi OS environment (text-only trigger).
3. Prepare the Ubuntu Server environment (voice + text + LLM).
4. Implement the core Pi bot code (connection, voice state detection, text message detection, WoL).
5. Implement the core tower bot code (voice connection, recording, text channel reader, transcription, LLM, Discord push).
6. Test the Pi bot in a private voice channel and text channel.
7. Test the tower bot in a private voice channel and text channel.
8. Set up the systemd services for persistent operation.
9. Monitor performance and adjust buffer sizes as needed.
