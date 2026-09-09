# Pi Listener

Discord voice recording bot for Raspberry Pi 3. Records speakers in a pre-specified voice channel, saves the audio as a WAV file, and forwards it to a local-network tower over SSH/SCP for transcription and LLM processing.

## Setup

### 1. Install system packages

The following terminal commands install everything the Pi Listener needs on a
headless Raspberry Pi (Raspberry Pi OS 64-bit). Run them in an SSH or serial
console session:

```bash
# Update the package index and upgrade installed packages
sudo apt update && sudo apt upgrade -y

# Install Python, the venv module, git, the SSH client (for scp/ssh to the
tower), and the Opus library (needed by py-cord's WaveSink for audio decode)
sudo apt install -y python3 python3-pip python3-venv git ssh libopus0

# Verify the versions
python3 --version   # expect 3.9+
ssh -V              # expect an OpenSSH version
```

> **Note:** `ssh` provides both the `ssh` and `scp` clients used to transfer
> WAV files to the tower. `libopus0` is required for py-cord's `WaveSink`
> Opus decoding; without it audio capture will fail.

### 2. Clone the repository

The repo is shared by the Pi and the tower. Clone it to `~/discord_listener`
so the repo-relative directory defaults in `bot.py` resolve correctly:

```bash
mkdir -p ~/discord_listener
cd ~/discord_listener
git clone <your-repo-url> .
```

### 3. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install Python dependencies

```bash
# Install the project dependencies (py-cord and python-dotenv)
pip install -r requirements.txt

# Verify the key packages
pip show py-cord python-dotenv | grep -E '^(Name|Version)'
```

### 5. Configure environment variables

Copy the example `.env` file and fill in the values:

```bash
cp .env.example .env
nano .env
```

Required values:
- `DISCORD_BOT_TOKEN` — your Discord bot token from the Developer Portal
- `DISCORD_VOICE_CHANNEL_ID` — the ID of the voice channel to join
- `TOWER_HOST` — the tower's fixed LAN IP or hostname
- `WOL_MAC_ADDRESS` — the tower's MAC address (for Wake-on-LAN)
- `TOWER_SSH_USER` — the SSH user on the tower

Optional values:
- `TOWER_SSH_KEY` — path to the Pi's private SSH key (defaults to `~/.ssh/id_ed25519`)
- `TOWER_UPLOAD_DIR` — destination directory on the tower (defaults to `discord_listener/Server_Listener/incoming`, repo-relative; do not use a `~` path here because it would be expanded on the Pi, not the tower)

### 6. Set up SSH key authentication (one-time)

Generate an SSH keypair on the Pi (if you don't already have one):

```bash
ssh-keygen -t ed25519
```

Copy the public key to the tower:

```bash
ssh-copy-id <TOWER_SSH_USER>@<TOWER_HOST>
```

Verify that you can SCP a file to the tower non-interactively:

```bash
echo "test" > /tmp/test.txt
scp -i ~/.ssh/id_ed25519 /tmp/test.txt <TOWER_SSH_USER>@<TOWER_HOST>:~/discord_listener/incoming/
```

### 7. Create the recordings directory

```bash
mkdir -p ~/discord_listener/recordings
```

## Running the Bot

### Foreground (for testing)

```bash
source venv/bin/activate
python bot.py
```

### As a systemd service (for deployment)

Create a file at `/etc/systemd/system/discord_listener.service`:

```ini
[Unit]
Description=Discord Voice Recording Bot
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

## How It Works

1. **Connection** — The bot logs in to Discord and joins the pre-specified voice channel.
2. **Listening & Detection** — The bot monitors the voice-gateway speaking hook (op 5) to detect when users start/stop speaking.
3. **Recording** — The bot uses py-cord's documented `start_listening()` API with a `WaveSink` to record incoming audio. The `WaveSink` handles DTLS/SRTP decryption and Opus decoding internally, producing 16-bit 48kHz WAV data per user.
4. **Saving** — When a speaker stops, the bot retrieves the user's recorded audio from the `WaveSink` via `get_user_audio(user_id)` and writes it to a WAV file (`~/discord_listener/recordings/recording_{N}.wav`). N starts at 1 and increments while a file with that name already exists, so an unsent recording is never overwritten.
5. **Sending** — The bot transfers the WAV file to the tower via atomic SSH/SCP (writes a `.part` file, then renames to `.wav` on the tower so the watcher never sees a partial file). If the tower is asleep, it sends a Wake-on-LAN packet and waits up to 300 s for the tower to become reachable before transferring. After a successful transfer, the local WAV file is deleted.

### DAVE (End-to-End Encryption) Caveat

py-cord's `start_listening()` may not capture audio if the voice channel is subject to DAVE (Discord's End-to-End Encryption for voice calls). DAVE is a platform-level feature that cannot be disabled by server owners. The bot detects DAVE at runtime via `vc.is_dave_connection()` and logs a warning after connecting. Test in your specific channel before deployment to confirm that audio capture works.

### Persistent SSH Session Manager

A background coroutine maintains the SSH session to the tower, keeping it awake while Discord voice activity warrants it:

- **ACTIVE** — ≥1 user in the voice channel. Keep the session alive. If the user is present but inactive/silent for 20 min → CLOSED.
- **GRACE** — No user in the voice channel. Keep the session alive for 5 min (reconnection buffer). If a user re-enters within 5 min → ACTIVE. If 5 min elapse with no user → CLOSED.
- **CLOSED** — Terminate the session. The tower sleeps on its own 20-min inactivity rule. Re-awakened via WoL when the next transfer/entry needs it.

## Troubleshooting

- **Bot not joining voice channel** — Verify the bot has `Connect`, `Speak`, and `View Channels` permissions in the target channel.
- **No audio recorded** — Verify `py-cord` is installed (`pip install py-cord`) and the system `libopus0` package is present (`sudo apt install libopus0`). The audio capture is implemented in `CustomVoiceClient` in `bot.py` using py-cord's documented `start_listening()` API with a `WaveSink`. If the voice channel is subject to DAVE (End-to-End Encryption), `start_listening()` may not capture audio — the bot logs a warning via `vc.is_dave_connection()` after connecting. Test in your specific channel before deployment to confirm that audio capture works.
- **SCP failing** — Verify SSH key authentication is set up correctly and the tower's `authorized_keys` contains the Pi's public key. Also verify the SSH key path in `.env` matches the key you generated (the README uses `ssh-keygen -t ed25519`, producing `~/.ssh/id_ed25519`, which is the code's default).
- **Tower not waking** — Verify the tower's MAC address in `.env` is correct and the tower is configured to accept Wake-on-LAN packets. Wake-on-LAN via broadcast only reaches the tower if both devices are on the same L2 segment (same switch/VLAN). If the tower is on another subnet, use a directed broadcast or a WoL relay. Also ensure the tower has a static IP or reserved DHCP lease matching `TOWER_HOST`.
