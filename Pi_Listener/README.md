# Pi Listener

Lightweight text-only Discord trigger for Raspberry Pi. Watches for user activity (entering the target voice channel or sending a message in the target text channel) and sends a Wake-on-LAN packet to wake the tower. The tower handles all voice recording, transcription, and LLM processing.

## Setup

### 1. Install system packages

The following terminal commands install everything the Pi Listener needs on a headless Raspberry Pi (Raspberry Pi OS 64-bit). Run them in an SSH or serial console session:

```bash
# Update the package index and upgrade installed packages
sudo apt update && sudo apt upgrade -y

# Install Python, the venv module, git, and the SSH client (for the optional
# SSH keepalive session to the tower)
sudo apt install -y python3 python3-pip python3-venv git ssh

# Verify the versions
python3 --version   # expect 3.9+
ssh -V              # expect an OpenSSH version
```

> **Note:** The Pi no longer needs `libopus0` or any voice-related system packages. Voice recording has moved to the tower.

### 2. Clone the repository

The repo is shared by the Pi and the tower. Clone it to `~/discord_listener`:

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
- `DISCORD_VOICE_CHANNEL_ID` — the ID of the voice channel to watch for user entry
- `TEXT_CHANNEL_ID` — the ID of the text channel to watch for user messages
- `TOWER_HOST` — the tower's fixed LAN IP or hostname
- `WOL_MAC_ADDRESS` — the tower's MAC address (for Wake-on-LAN)
- `TOWER_SSH_USER` — the SSH user on the tower

Optional values:
- `TOWER_SSH_KEY` — path to the Pi's private SSH key (defaults to `~/.ssh/id_ed25519`)

### 6. Set up SSH key authentication (one-time, optional)

If you want to use the optional SSH keepalive session to keep the tower awake during activity:

Generate an SSH keypair on the Pi (if you don't already have one):

```bash
ssh-keygen -t ed25519
```

Copy the public key to the tower:

```bash
ssh-copy-id <TOWER_SSH_USER>@<TOWER_HOST>
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

## How It Works

1. **Connection** — The bot logs in to Discord (text-only, no voice).
2. **Voice Channel Detection** — The bot monitors `on_voice_state_update` to detect when a user enters the target voice channel.
3. **Text Channel Detection** — The bot monitors `on_message` to detect when a user sends a message in the target text channel.
4. **Wake-on-LAN** — On either trigger, the bot sends a Wake-on-LAN magic packet to the tower's MAC address.
5. **Optional SSH Keepalive** — A background coroutine can maintain a long-lived SSH session to keep the tower awake while users are present in the voice channel (ACTIVE/GRACE/CLOSED state machine).

### Why Text-Only?

The Pi cannot sustain a voice connection due to OS scheduling limitations. The voice pipeline demands a responsive event loop for the 60-second handshake, continuous UDP socket management, and tight heartbeat-ACK windows. A throttling Pi will eventually drop it. A text-only gateway sends one heartbeat every ~41 seconds and tolerates significant delay, so the Pi can maintain it even under load. The tower has the CPU headroom, thermal margin, and fast storage to run the voice pipeline reliably.

### Persistent SSH Session Manager (Optional)

A background coroutine maintains the SSH session to the tower, keeping it awake while Discord activity warrants it:

- **ACTIVE** — ≥1 user in the voice channel. Keep the session alive.
- **GRACE** — No user in the voice channel. Keep the session alive for 5 min (reconnection buffer). If a user re-enters within 5 min → ACTIVE. If 5 min elapse with no user → CLOSED.
- **CLOSED** — Terminate the session. The tower sleeps on its own inactivity rule. Re-awakened via WoL when the next trigger needs it.

## Troubleshooting

- **Bot not staying connected** — Verify the `DISCORD_BOT_TOKEN` is correct and the bot has `View Channels` permission in the target voice and text channels.
- **Tower not waking** — Verify the tower's MAC address in `.env` is correct and the tower is configured to accept Wake-on-LAN packets. Wake-on-LAN via broadcast only reaches the tower if both devices are on the same L2 segment (same switch/VLAN). If the tower is on another subnet, use a directed broadcast or a WoL relay. Also ensure the tower has a static IP or reserved DHCP lease matching `TOWER_HOST`.
- **No trigger on voice entry** — Verify the `DISCORD_VOICE_CHANNEL_ID` in `.env` matches the actual voice channel ID. The bot only triggers when a user enters that specific channel.
- **No trigger on text message** — Verify the `TEXT_CHANNEL_ID` in `.env` matches the actual text channel ID. The bot only triggers on messages in that specific channel.
