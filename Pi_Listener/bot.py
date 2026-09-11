import asyncio
import logging
import discord
import os
import time
import socket
import subprocess
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Logging Setup ---
# Use the standard logging module so background-task exceptions (which are
# only surfaced via done-callbacks) are captured in the same journal as the
# rest of the bot's output.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration (use environment variables for sensitive data) ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", "0"))
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID", "0"))
TOWER_HOST = os.getenv("TOWER_HOST")
WOL_MAC_ADDRESS = os.getenv("WOL_MAC_ADDRESS")
TOWER_SSH_USER = os.getenv("TOWER_SSH_USER")
TOWER_SSH_KEY = os.path.expanduser(os.getenv("TOWER_SSH_KEY", "~/.ssh/id_ed25519"))

# --- Discord Intents ---
# Text-only gateway: no voice, no members, no presences. The Pi's role is
# now just to watch for activity (user entering the target voice channel or
# sending a message in the target text channel) and WOL the tower.
intents = discord.Intents.default()

# --- Bot Initialization ---
bot = discord.Client(intents=intents)

# --- Wake-on-LAN Helper ---
def send_wol(mac_address: str, verbose: bool = True):
    """Send a Wake-on-LAN magic packet to the specified MAC address.

    WOL is idempotent: a magic packet addressed to an already-awake machine is
    simply dropped by its NIC. This makes it safe to call periodically as a
    state-based catch-up (see ssh_session_manager and text_channel_catchup),
    which guarantees the tower is woken even if the corresponding gateway
    event was missed (e.g. while the session was down or the loop was starved).
    """
    if not mac_address:
        return
    try:
        mac_bytes = bytes.fromhex(mac_address.replace(':', ''))
        packet = b'\xff' * 6 + (mac_bytes * 16)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, ('<broadcast>', 9))
        sock.close()
        if verbose:
            print(f"Wake-on-LAN packet sent to {mac_address}")
    except Exception as e:
        print(f"Failed to send Wake-on-LAN packet: {e}")

# --- SSH Reachability Check ---
def ssh_reachable(host: str, timeout: int = 3) -> bool:
    """Check if the tower's SSH port is reachable."""
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False

# --- Wait for SSH ---
def wait_for_ssh(host: str, timeout: int = 300, interval: int = 5) -> bool:
    """Poll TCP connect to SSH port 22 with retries until reachable or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ssh_reachable(host, timeout=3):
            return True
        time.sleep(interval)
    return False

# --- Trigger Helper ---
def _trigger_tower(reason: str):
    """WOL the tower and log the reason. Called when the user enters the
    target voice channel or sends a message in the target text channel.

    The tower handles its own session lifecycle (it stays awake while its
    voice/text pipeline is active and sleeps on its own inactivity rule).
    """
    logger.info(f"Trigger: {reason}")
    send_wol(WOL_MAC_ADDRESS)

# --- Events ---
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Detect a user entering the target voice channel and WOL the tower.

    The Pi no longer records audio. Its only job is to wake the tower so the
    tower's voice bot can join the channel and start recording.
    """
    # Ignore the bot's own voice state changes.
    if member.bot:
        return

    # Only care about users entering the target voice channel.
    if after.channel and after.channel.id == VOICE_CHANNEL_ID:
        # Was the user already in the target channel before this update?
        if before.channel is None or before.channel.id != VOICE_CHANNEL_ID:
            _trigger_tower(f"User {member.name} entered voice channel {VOICE_CHANNEL_ID}")

@bot.event
async def on_message(message):
    """Detect a user sending a message in the target text channel and WOL the tower.

    The tower will read the message from the text channel (no voice join
    needed) and process it with the LLM.
    """
    # Ignore the bot's own messages and messages from other bots.
    if message.author.bot:
        return

    # Only care about messages in the target text channel.
    if message.channel.id == TEXT_CHANNEL_ID:
        _trigger_tower(f"User {message.author.name} sent message in text channel {TEXT_CHANNEL_ID}")

# --- Persistent SSH Session Manager ---
# Optional: keeps the tower awake while Discord activity warrants it.
# State machine:
#   ACTIVE  - >=1 user in the voice channel. Keep session alive. If user is
#             present but inactive for 20 min -> CLOSED.
#   GRACE   - No user in the voice channel. Keep session alive for 5 min
#             (reconnection buffer). If a user re-enters within 5 min -> ACTIVE.
#             If 5 min elapse with no user -> CLOSED.
#   CLOSED  - Terminate the session. The tower sleeps on its own 20-min
#             inactivity rule. Re-awakened via WoL when the next trigger needs it.
#
# Implementation: the manager holds a long-lived `ssh` subprocess (a no-op
# keepalive command) that stays open as long as the state is ACTIVE or GRACE.
# The SSH session itself keeps the tower awake (the tower stays awake while an
# SSH session is active and sleeps after ~20 min idle). When the state
# transitions to CLOSED, the subprocess is terminated and the tower is allowed
# to sleep. The next trigger re-awakens the tower via WoL if needed.

ACTIVE_INACTIVITY_LIMIT = timedelta(minutes=20)
GRACE_LIMIT = timedelta(minutes=5)

# Track the last time a user was present in the voice channel
last_user_presence = datetime.now()

# The long-lived SSH keepalive subprocess (None when CLOSED)
_ssh_keepalive_proc = None

def update_user_presence():
    """Update the last user presence timestamp (called when a user is in the channel)."""
    global last_user_presence
    last_user_presence = datetime.now()

def open_ssh_keepalive():
    """Open a long-lived SSH session to the tower to keep it awake."""
    global _ssh_keepalive_proc
    if _ssh_keepalive_proc is not None:
        return
    try:
        cmd = [
            'ssh',
            '-i', TOWER_SSH_KEY,
            '-o', 'BatchMode=yes',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            f"{TOWER_SSH_USER}@{TOWER_HOST}",
            'sleep infinity',
        ]
        _ssh_keepalive_proc = subprocess.Popen(cmd)
        print("SSH keepalive session opened.")
    except Exception as e:
        print(f"Failed to open SSH keepalive session: {e}")
        _ssh_keepalive_proc = None

def close_ssh_keepalive():
    """Terminate the long-lived SSH session, allowing the tower to sleep."""
    global _ssh_keepalive_proc
    if _ssh_keepalive_proc is None:
        return
    try:
        _ssh_keepalive_proc.terminate()
        _ssh_keepalive_proc.wait(timeout=5)
    except Exception:
        try:
            _ssh_keepalive_proc.kill()
        except Exception:
            pass
    _ssh_keepalive_proc = None
    print("SSH keepalive session closed.")

def _keepalive_alive() -> bool:
    """Return True if the keepalive subprocess is still running."""
    global _ssh_keepalive_proc
    if _ssh_keepalive_proc is None:
        return False
    if _ssh_keepalive_proc.poll() is not None:
        # Process died (e.g., tower was asleep when we tried to open).
        # Treat as dead so we can retry.
        _ssh_keepalive_proc = None
        print("SSH keepalive process died. Will retry.")
        return False
    return True

async def ssh_session_manager():
    """Background coroutine that maintains the SSH session to the tower and
    provides state-based WOL catch-up.

    While any user is present in the target voice channel, a quiet WOL packet
    is sent every tick. This is idempotent and ensures the tower is woken even
    if the on_voice_state_update join event was missed (e.g. while the gateway
    session was down, or while the event loop was starved by OS load).
    """
    while True:
        await asyncio.sleep(30)
        now = datetime.now()

        # Check if there are users in the voice channel
        channel = bot.get_channel(VOICE_CHANNEL_ID)
        users_in_channel = []
        if channel:
            for voice_state in channel.voice_states:
                if voice_state.user and not voice_state.user.bot:
                    users_in_channel.append(voice_state.user)

        if users_in_channel:
            # Users are present -> ACTIVE state
            update_user_presence()

            # Keep the session alive (retry if the proc died).
            if not _keepalive_alive():
                open_ssh_keepalive()

            # State-based WOL catch-up (idempotent; quiet).
            send_wol(WOL_MAC_ADDRESS, verbose=False)
        else:
            # No users in the channel -> GRACE state
            if now - last_user_presence > GRACE_LIMIT:
                print("No user in channel for 5 min. Closing SSH session.")
                close_ssh_keepalive()
            else:
                # Keep the session alive for the reconnection buffer.
                if not _keepalive_alive():
                    open_ssh_keepalive()

# --- Text Channel Catch-Up ---
# The on_message trigger only fires while the gateway session is healthy. If
# the loop is starved and the session drops (Discord closes it after ~120 s of
# missed heartbeats; py-cord resumes automatically), messages sent in the text
# channel during the outage are missed. This coroutine periodically fetches
# recent messages and WOLs the tower if a non-bot user has messaged since the
# last check. The first fetch only establishes a baseline (no trigger), so a
# restart does not re-trigger for old messages.
_last_text_message_id = None

async def text_channel_catchup():
    global _last_text_message_id
    while True:
        await asyncio.sleep(60)
        try:
            channel = bot.get_channel(TEXT_CHANNEL_ID)
            if channel is None:
                continue
            newest = 0
            missed = None
            # channel.history() fetches via HTTP (works regardless of gateway
            # state) and is available on this py-cord version.
            async for msg in channel.history(limit=25):
                if msg.id > newest:
                    newest = msg.id
                if _last_text_message_id is None or msg.id > _last_text_message_id:
                    # Match on_message semantics: any non-bot message counts.
                    # Do NOT check msg.content — if the app lacks the
                    # message_content intent, the REST API returns empty
                    # content and the check would silently disable catch-up.
                    if missed is None and not msg.author.bot:
                        missed = (msg.author.name, msg.id)
            if newest == 0:
                continue
            if _last_text_message_id is None:
                _last_text_message_id = newest
                logger.info(f"Text catch-up baseline set at message id {newest}.")
                continue
            _last_text_message_id = newest
            if missed is not None:
                _trigger_tower(
                    f"Text catch-up: user {missed[0]} messaged in text channel "
                    f"{TEXT_CHANNEL_ID} (message id {missed[1]})"
                )
        except Exception as e:
            logger.warning(f"Text catch-up check failed: {type(e).__name__}: {e}")

# --- Run the Bot ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is not set.")
    if VOICE_CHANNEL_ID == 0:
        raise ValueError("DISCORD_VOICE_CHANNEL_ID environment variable is not set.")
    if TEXT_CHANNEL_ID == 0:
        raise ValueError("TEXT_CHANNEL_ID environment variable is not set.")

    # Validate required configuration
    if not TOWER_HOST:
        raise ValueError("TOWER_HOST environment variable is not set.")
    if not WOL_MAC_ADDRESS:
        raise ValueError("WOL_MAC_ADDRESS environment variable is not set.")
    if not TOWER_SSH_USER:
        raise ValueError("TOWER_SSH_USER environment variable is not set.")

    async def run_bot():
        # Optional: keep the tower awake via a long-lived SSH session while
        # users are present in the voice channel.
        session_task = asyncio.create_task(ssh_session_manager())
        # Catch up on text-channel messages missed while the gateway was down.
        catchup_task = asyncio.create_task(text_channel_catchup())
        try:
            await bot.start(BOT_TOKEN)
        finally:
            session_task.cancel()
            catchup_task.cancel()

    asyncio.run(run_bot())
