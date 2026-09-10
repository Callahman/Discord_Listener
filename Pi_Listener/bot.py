import asyncio
import logging
import discord
import discord.voice
from discord.sinks import WaveSink
# py-cord (pip package: py-cord) is the actively-maintained fork of discord.py.
# Starting with the 2.x rewrite it installs as the `discord` module (same as
# discord.py), so we import it directly.
#
# Voice receive: py-cord exposes a documented start_listening() API on
# VoiceClient that records incoming audio into a Sink object. We use the
# documented WaveSink class, which handles DTLS/SRTP decryption and Opus
# decoding internally and produces 16-bit 48kHz WAV files. This is the stable,
# first-party receive path (not a private-internal hook).
#
# NOTE: start_listening() may not work as expected if the voice channel is
# subject to DAVE (Discord's End-to-End Encryption for voice calls). DAVE is
# a platform-level feature that cannot be disabled by server owners. We detect
# it at runtime via vc.is_dave_connection() and log a warning. Test in your
# specific channel before deployment to confirm that audio capture works.
#
# NOTE: py-cord's VoiceClient also exposes is_speaking(member) (added in 2.7,
# returns True/False/None for whether a member is speaking). We use the
# voice-gateway speaking hook (op 5) for per-user start/stop transitions,
# which is more reliable than is_speaking() (documented as an approximate
# calculation that may have outdated data).
import wave
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
TOWER_HOST = os.getenv("TOWER_HOST")
WOL_MAC_ADDRESS = os.getenv("WOL_MAC_ADDRESS")
TOWER_SSH_USER = os.getenv("TOWER_SSH_USER")
TOWER_SSH_KEY = os.path.expanduser(os.getenv("TOWER_SSH_KEY", "~/.ssh/id_ed25519"))
# TOWER_UPLOAD_DIR is a REMOTE path on the tower. Do NOT expanduser it here
# (that would resolve ~ to the Pi's home, which may not exist on the tower).
# The default is a repo-relative path: the repo is cloned to
# <home>/discord_listener on both the Pi and the tower, so
# discord_listener/Server_Listener/incoming resolves to the same directory
# on the tower. Override with an absolute path if your layout differs.
TOWER_UPLOAD_DIR = os.getenv("TOWER_UPLOAD_DIR", "discord_listener/Server_Listener/incoming")
RECORDINGS_DIR = os.path.join(os.path.expanduser("~"), "discord_listener", "recordings")

# Ensure recordings directory exists
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# --- Discord Intents ---
intents = discord.Intents.default()
intents.members = True
intents.presences = True

# --- Bot Initialization ---
bot = discord.Client(intents=intents)

# --- State Tracking ---
# {user_id: {'buffer': bytearray, 'start_time': datetime, 'last_activity': datetime, 'username': str}}
speaking_users = {}

# --- Atomic WAV Path Allocation ---
# Use O_CREAT | O_EXCL to atomically claim a file name. This avoids the
# check-then-create race that os.path.exists() + wave.open() would have once
# sends move to worker threads.
def get_next_wav_path():
    """Atomically claim a unique WAV path recording_N.wav.

    Increments N until os.open() with O_CREAT|O_EXCL succeeds, returning the
    claimed path. The file is created empty; the caller writes WAV content
    into it (truncating is safe because we just claimed it).
    """
    n = 1
    while True:
        candidate = os.path.join(RECORDINGS_DIR, f"recording_{n}.wav")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate
        except FileExistsError:
            n += 1

# --- Wake-on-LAN Helper ---
def send_wol(mac_address: str):
    """Send a Wake-on-LAN magic packet to the specified MAC address."""
    if not mac_address:
        return
    try:
        mac_bytes = bytes.fromhex(mac_address.replace(':', ''))
        packet = b'\xff' * 6 + (mac_bytes * 16)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, ('<broadcast>', 9))
        sock.close()
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

# --- SCP Transfer (atomic) ---
def scp_to_tower(wav_file: str, upload_dir: str) -> bool:
    """Copy the WAV file to the tower via SCP, atomically.

    The file is first SCP'd to a temp name (<name>.part) in the upload dir,
    then renamed to the final name over SSH. This prevents the tower's file
    watcher from transcribing a partially-written file. Returns True on
    success (final file present on the tower), False otherwise.
    """
    fname = os.path.basename(wav_file)
    tmp_name = fname + ".part"
    final_name = fname
    try:
        # Step 1: SCP to a temp name so the watcher never sees a partial file.
        cmd = [
            'scp',
            '-i', TOWER_SSH_KEY,
            '-o', 'BatchMode=yes',
            wav_file,
            f"{TOWER_SSH_USER}@{TOWER_HOST}:{upload_dir}/{tmp_name}",
        ]
        result = subprocess.run(cmd, timeout=60)
        if result.returncode != 0:
            logger.warning(f"SCP to temp name failed for {fname} (rc={result.returncode})")
            return False

        # Step 2: Rename temp -> final over SSH (atomic on the same filesystem).
        mv_cmd = [
            'ssh',
            '-i', TOWER_SSH_KEY,
            '-o', 'BatchMode=yes',
            f"{TOWER_SSH_USER}@{TOWER_HOST}",
            f"mv -f {upload_dir}/{tmp_name} {upload_dir}/{final_name}",
        ]
        mv_result = subprocess.run(mv_cmd, timeout=30)
        if mv_result.returncode != 0:
            logger.warning(f"Remote rename failed for {fname} (rc={mv_result.returncode})")
            return False

        return True
    except Exception as e:
        logger.error(f"SCP error: {e}")
        return False

# --- In-flight send tracking (prevents double-send race) ---
# Set of filenames currently being sent (via _spawn_send or retry_unsent_wavs).
# Both the fire-and-forget path and the retry task consult this so the same
# file is never SCP'd twice concurrently.
_inflight_sends = set()

# --- WAV File Sender (blocking; always run via asyncio.to_thread) ---
def send_wav_file(wav_file: str, username: str):
    """Send the WAV file to the tower and delete it on success.

    BLOCKING: may sleep up to 300 s while the tower boots. Callers must use
    asyncio.to_thread() so the event loop is never stalled.
    """
    if not os.path.exists(wav_file):
        logger.info(f"WAV file not found: {wav_file}")
        return

    try:
        if ssh_reachable(TOWER_HOST):
            ok = scp_to_tower(wav_file, TOWER_UPLOAD_DIR)
        else:
            send_wol(WOL_MAC_ADDRESS)
            # Wait for the tower to finish booting after the initial WoL.
            # wait_for_ssh polls TCP port 22 with retries (up to 300 s by
            # default) to cover the tower's boot time before attempting the
            # transfer.
            if not wait_for_ssh(TOWER_HOST, timeout=300, interval=5):
                logger.warning(f"Tower not reachable after WoL. Keeping file for retry.")
                return
            ok = scp_to_tower(wav_file, TOWER_UPLOAD_DIR)

        if ok:
            os.remove(wav_file)
            logger.info(f"Successfully sent and deleted WAV file: {wav_file} (speaker: {username})")
        else:
            logger.warning(f"Failed to send WAV file. Keeping file for retry.")
    except Exception as e:
        logger.error(f"Error sending WAV file: {e}. Keeping file for retry.")

# --- Fire-and-forget send helper (async-safe) ---
def _spawn_send(wav_file: str, username: str):
    """Schedule send_wav_file on a worker thread without blocking the loop.

    Keeps a reference to the task and adds a done-callback that logs any
    exception, so failures are never silently swallowed.
    """
    fname = os.path.basename(wav_file)
    if fname in _inflight_sends:
        logger.info(f"Send already in flight for {fname}; skipping duplicate.")
        return
    _inflight_sends.add(fname)

    async def _wrapped():
        try:
            await asyncio.to_thread(send_wav_file, wav_file, username)
        finally:
            _inflight_sends.discard(fname)

    task = asyncio.create_task(_wrapped())

    def _on_done(t):
        if not t.cancelled() and t.exception() is not None:
            logger.error(f"Send task for {fname} raised: {t.exception()}")

    task.add_done_callback(_on_done)

# --- Custom Voice Client: real audio capture via start_listening() + WaveSink ---
# py-cord's VoiceClient exposes a documented start_listening() API that
# records incoming audio into a Sink object. We use the documented WaveSink
# class, which handles DTLS/SRTP decryption and Opus decoding internally and
# produces 16-bit 48kHz WAV files. This is the stable, first-party receive
# path (not a private-internal hook).
#
# We subclass VoiceClient and do two things:
#
# 1. Override _handle_speaking (voice-gateway op 5). In py-cord this is a
#    no-op; Discord sends it with the user id and a speaking flag whenever a
#    user's speaking state changes. This gives us true per-user start/stop
#    transitions for recording.
#
# 2. On speaking stop, retrieve the user's recorded audio from the WaveSink
#    via get_user_audio(user_id) and write it to RECORDINGS_DIR.
#
# NOTE: start_listening() may not work as expected if the voice channel is
# subject to DAVE (Discord's End-to-End Encryption for voice calls). DAVE is
# a platform-level feature that cannot be disabled by server owners. We detect
# it at runtime via vc.is_dave_connection() and log a warning. Test in your
# specific channel before deployment to confirm that audio capture works.


class CustomVoiceClient(discord.voice.VoiceClient):
    """VoiceClient subclass that captures incoming audio via the documented
    start_listening() API + WaveSink, and tracks per-user speaking state via
    the voice-gateway speaking hook."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sink = WaveSink()
        # py-cord's VoiceClient.__init__ sets self.loop to a non-running loop
        # (via get_event_loop()) on Python 3.13, which causes 'attached to a
        # different loop' when voice tasks (voice-connector, voice-ws-poller)
        # are spawned. Override it with the currently-running loop so those
        # tasks bind to the same loop as the rest of the bot. This runs during
        # channel.connect() (inside on_ready), so get_running_loop() returns
        # the correct loop.
        self.loop = asyncio.get_running_loop()

    # NOTE: The previous _handle_speaking override is REMOVED. In this py-cord
    # version (2.8.1) VoiceClient has no _handle_speaking method; the
    # voice-gateway speaking op is handled in _recv_hook, which dispatches the
    # 'member_speaking_state_update' event to the main client. The main bot
    # listens for it via on_member_speaking_state_update (see below) for real
    # per-user start/stop transitions.

    # --- Start listening (documented py-cord API) ---
    # Called after connecting to the voice channel. Passes the WaveSink to
    # start_listening() so all incoming audio is recorded into the Sink.
    #
    # NOTE: start_listening() requires a Sink object. WaveSink is the
    # documented, recommended class for WAV output. It handles DTLS/SRTP
    # decryption and Opus decoding internally, producing 16-bit 48kHz WAV
    # files.
    def start_listening(self, sink=None, callback=None):
        """Start receiving audio via the documented py-cord API.

        If no sink is provided, uses the internal WaveSink.
        """
        if sink is None:
            sink = self._sink
        super().start_listening(sink, callback)

# --- Flush helper for users not in cache ---
def _flush_user_buffer(user_id: int):
    """Save and send the buffer for a user whose object is no longer in cache.

    Retrieves the user's recorded audio from the WaveSink by reading the
    sink's audio_data entry directly (get_user_audio() is broken in this
    py-cord version) and writes it to RECORDINGS_DIR.
    """
    if user_id not in speaking_users:
        return
    user_data = speaking_users.pop(user_id)
    username = user_data['username']

    # Get the VoiceClient to access the WaveSink.
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    vc = channel.voice_client if channel else None
    if not isinstance(vc, CustomVoiceClient):
        print(f"No voice client available to retrieve audio for {username} (user not in cache)")
        return

    sink = vc._sink
    if sink is None:
        print(f"No sink available to retrieve audio for {username} (user not in cache)")
        return

    try:
        # get_user_audio() is BROKEN in this py-cord version: it does
        # os.path.realpath(self.audio_data.pop(user)) where audio_data[user]
        # is an AudioData object (wrapping a BytesIO), not a path. It always
        # raises (KeyError if no audio, TypeError if audio present). Access
        # the sink's audio_data directly instead. Do NOT pop: the entry is
        # still being written to by the PacketRouter thread while the user
        # speaks, and we want to keep it for any subsequent flush.
        entry = sink.audio_data.get(user_id)
        if entry is None:
            print(f"No audio recorded for: {username} (user not in cache)")
            return

        # entry is an AudioData object wrapping a BytesIO of decoded PCM.
        audio_data = entry.file.getvalue()

        if not audio_data:
            print(f"No audio recorded for: {username} (user not in cache)")
            return

        wav_path = get_next_wav_path()
        with open(wav_path, 'wb') as f:
            if audio_data[:4] == b'RIFF':
                f.write(audio_data)
            else:
                import struct
                pcm_size = len(audio_data)
                f.write(b'RIFF')
                f.write(struct.pack('<I', 36 + pcm_size))
                f.write(b'WAVE')
                f.write(b'fmt ')
                f.write(struct.pack('<I', 16))
                f.write(struct.pack('<H', 1))
                f.write(struct.pack('<H', 1))
                f.write(struct.pack('<I', 48000))
                f.write(struct.pack('<I', 48000 * 2))
                f.write(struct.pack('<H', 2))
                f.write(struct.pack('<H', 16))
                f.write(b'data')
                f.write(struct.pack('<I', pcm_size))
                f.write(audio_data)
        print(f"Saved WAV file: {wav_path} (speaker: {username}, user not in cache)")
        _spawn_send(wav_path, username)
    except Exception as e:
        print(f"Error saving WAV file for {username}: {e}")

# --- Events ---
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel:
        # Guard: only connect if not already connected (on_ready re-fires
        # after every gateway reconnect). _do_voice_connect also checks the
        # _voice_connecting flag to prevent a double-connect race.
        await _do_voice_connect(channel)
    else:
        logger.warning(f"Voice channel with ID {VOICE_CHANNEL_ID} not found.")

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state updates for leave/move/mute/deaf handling.

    Speaking start/stop is handled by the voice-gateway speaking hook in
    CustomVoiceClient._handle_speaking. This handler covers the cases the
    speaking hook does not: channel leave, channel-to-channel move, and
    mute/deaf transitions.
    """
    # Ignore the bot's own voice state changes.
    if member.bot:
        return

    # NOTE: The docstring above is stale — it references the removed
    # CustomVoiceClient._handle_speaking override. In this py-cord version
    # (2.8.1) the voice-gateway speaking op is handled in _recv_hook, which
    # dispatches 'member_speaking_state_update' to the main client. The main
    # bot listens for it via on_member_speaking_state_update (defined below)
    # for real per-user start/stop transitions. This on_voice_state_update
    # handler covers the cases the speaking op does not: channel leave,
    # channel-to-channel move, and mute/deaf transitions (all stop recording
    # immediately), plus a safety-net start if the speaking op never fired.

    # If the user left the target channel OR moved to another channel, stop
    # recording immediately (not just via the idle timer).
    if before.channel and before.channel.id == VOICE_CHANNEL_ID and \
            (after.channel is None or after.channel.id != VOICE_CHANNEL_ID):
        if member.id in speaking_users:
            stop_recording(member)
        return

    # Check if the user is in the target voice channel.
    if not after.channel or after.channel.id != VOICE_CHANNEL_ID:
        return

    # Check mute/deaf status. If muted/deaf, stop recording if they were
    # speaking (the speaking hook will not fire for a muted user).
    if after.self_mute or after.deaf:
        if member.id in speaking_users:
            stop_recording(member)
        return

    # Update presence for the SSH session manager. We do NOT update activity
    # here (activity is only updated on genuine speaking events via the
    # speaking hook), so mute toggles do not keep the session "active".
    update_user_presence()

    # NOTE: The previous "fallback: start recording" is REMOVED. Real per-user
    # speaking start/stop now comes from the voice-gateway speaking op via
    # on_member_speaking_state_update (see below). This handler only covers the
    # cases the speaking op does not: channel leave, channel-to-channel move,
    # and mute/deaf transitions (all of which stop recording immediately).

    # Safety net: if the speaking op never fired for this user (e.g., the
    # voice connection was still establishing when they joined), start
    # recording here so we don't miss their audio. The idle-cleanup task
    # stops recording after 10 s of inactivity, so this does not produce
    # false-positive recordings for users who are present but silent.
    if member.id not in speaking_users:
        start_recording(member)

@bot.event
async def on_member_speaking_state_update(member, ssrc, state):
    """Handle the voice-gateway speaking op (real per-user start/stop).

    In py-cord 2.8.1 the voice-gateway speaking op (op 5) is handled in
    VoiceClient._recv_hook, which dispatches 'member_speaking_state_update'
    to the main client. The arguments are:
      - member: a GuildMember (or None if not in cache)
      - ssrc: the SSRC associated with the user
      - state: a SpeakingState bitmask (none=0, voice=1, soundshare=2, priority=4)

    A user is producing voice audio when the 'voice' bit (1) is set in the
    state bitmask. We use this for real per-user start/stop transitions:
      - voice bit set   -> start recording (or update last_activity)
      - voice bit clear -> stop recording (save + send the WAV)
    """
    if member is None or member.bot:
        return

    # Only care about users in the target voice channel.
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        return
    in_target = False
    for vs in channel.voice_states:
        if vs.user and vs.user.id == member.id:
            in_target = True
            break
    if not in_target:
        return

    # state is a SpeakingState bitmask. Voice audio is present when the
    # 'voice' bit (1) is set.
    is_speaking = bool(int(state) & 1)

    if is_speaking:
        if member.id not in speaking_users:
            start_recording(member)
        else:
            speaking_users[member.id]['last_activity'] = datetime.now()
        # Genuine speaking event -> update global activity for SSH manager.
        update_user_activity()
    else:
        # Speaking stopped -> stop recording for this user (save + send WAV).
        if member.id in speaking_users:
            stop_recording(member)

def start_recording(member):
    """Start recording for a user.

    Note: with the WaveSink-based implementation, the actual audio is captured
    by the WaveSink (via start_listening()). This function only tracks the
    user's speaking state in speaking_users so we know who to retrieve audio
    for when they stop speaking.
    """
    speaking_users[member.id] = {
        'start_time': datetime.now(),
        'last_activity': datetime.now(),
        'username': member.name
    }
    print(f"Started recording for: {member.name}")

def stop_recording(member):
    """Stop recording for a user and save/send the WAV file.

    Retrieves the user's recorded audio from the WaveSink by reading the
    sink's audio_data entry directly (get_user_audio() is broken in this
    py-cord version) and writes it to RECORDINGS_DIR. The WaveSink handles
    DTLS/SRTP decryption and Opus decoding internally, producing 16-bit
    48kHz PCM data (wrapped in a WAV header below).
    """
    if member.id not in speaking_users:
        return

    user_data = speaking_users.pop(member.id)
    username = user_data['username']

    # Get the VoiceClient to access the WaveSink.
    channel = bot.get_channel(VOICE_CHANNEL_ID)
    vc = channel.voice_client if channel else None
    if not isinstance(vc, CustomVoiceClient):
        print(f"No voice client available to retrieve audio for {username}")
        return

    sink = vc._sink
    if sink is None:
        print(f"No sink available to retrieve audio for {username}")
        return

    try:
        # get_user_audio() is BROKEN in this py-cord version: it does
        # os.path.realpath(self.audio_data.pop(user)) where audio_data[user]
        # is an AudioData object (wrapping a BytesIO), not a path. It always
        # raises (KeyError if no audio, TypeError if audio present). Access
        # the sink's audio_data directly instead. Do NOT pop: the entry is
        # still being written to by the PacketRouter thread while the user
        # speaks, and we want to keep it for any subsequent flush.
        entry = sink.audio_data.get(member.id)
        if entry is None:
            print(f"No audio recorded for: {username}")
            return

        # entry is an AudioData object wrapping a BytesIO of decoded PCM.
        audio_data = entry.file.getvalue()

        if not audio_data:
            print(f"No audio recorded for: {username}")
            return

        # Write the audio to an atomically-claimed WAV path.
        #
        # NOTE: The WaveSink produces raw PCM data (16-bit 48kHz mono) when
        # get_user_audio() is called mid-recording (before Sink.cleanup()
        # has run format_audio() to add a WAV header). We wrap the PCM in a
        # WAV header here so the tower's Whisper can transcribe it.
        #
        # If the audio already has a WAV header (e.g., if format_audio() was
        # called), the 'RIFF' magic bytes will be present at the start. In
        # that case, write the data as-is. Otherwise, add a WAV header.
        wav_path = get_next_wav_path()
        with open(wav_path, 'wb') as f:
            if audio_data[:4] == b'RIFF':
                # Already has a WAV header — write as-is.
                f.write(audio_data)
            else:
                # Raw PCM — wrap in a WAV header (16-bit 48kHz mono).
                import struct
                pcm_size = len(audio_data)
                f.write(b'RIFF')
                f.write(struct.pack('<I', 36 + pcm_size))
                f.write(b'WAVE')
                f.write(b'fmt ')
                f.write(struct.pack('<I', 16))  # fmt chunk size
                f.write(struct.pack('<H', 1))   # PCM format
                f.write(struct.pack('<H', 1))   # mono
                f.write(struct.pack('<I', 48000))  # sample rate
                f.write(struct.pack('<I', 48000 * 2))  # byte rate
                f.write(struct.pack('<H', 2))   # block align
                f.write(struct.pack('<H', 16))  # bits per sample
                f.write(b'data')
                f.write(struct.pack('<I', pcm_size))
                f.write(audio_data)
        print(f"Saved WAV file: {wav_path} (speaker: {username})")

        # Send the WAV file to the remote server (fire-and-forget on a thread).
        _spawn_send(wav_path, username)
    except Exception as e:
        print(f"Error saving WAV file for {username}: {e}")

# --- Voice Connect Guard (prevents double-connect race) ---
# A flag that is True while a voice connect() is in progress. Both on_ready
# and voice_reconnect_loop check this before attempting to connect, so a
# connect that is still in flight (voice_client may still read as None) is
# not duplicated.
_voice_connecting = False

async def _do_voice_connect(channel):
    """Connect to the voice channel with the CustomVoiceClient, guarded by
    _voice_connecting to prevent a double-connect race."""
    global _voice_connecting
    if _voice_connecting:
        return
    try:
        if channel.voice_client is not None:
            return
    except AttributeError:
        pass
    _voice_connecting = True
    t0 = time.monotonic()
    try:
        logger.info(f"Voice connect starting for channel '{channel.name}'...")
        vc = await channel.connect(cls=CustomVoiceClient)
        elapsed = time.monotonic() - t0
        logger.info(f"Voice connect completed in {elapsed:.1f}s for channel '{channel.name}'.")
        if elapsed > 30:
            logger.warning(
                f"Voice connect took {elapsed:.1f}s (>30s). The event loop may have been "
                "blocked during connect (Pi load/throttling). This risks the 60s voice "
                "connect timeout and prevents audio from being received."
            )
        vc.start_listening()
        # DAVE/E2EE detection: if this is a DAVE call, voice reception is broken
        # (py-cord emits a RuntimeWarning and decrypt_rtp silently substitutes
        # OPUS_SILENCE on DAVE decryption failure). Log it clearly.
        try:
            is_dave = vc.is_dave_connection()
        except Exception as dave_err:
            logger.warning(f"is_dave_connection() raised: {type(dave_err).__name__}: {dave_err}")
            is_dave = None
        if is_dave:
            logger.warning(
                "Voice channel is a DAVE (E2EE) call. py-cord's voice reception is "
                "BROKEN for DAVE calls (see Pycord issue #3139). Even if capture works, "
                "you will get OPUS_SILENCE. Audio will NOT be transcribed."
            )
        elif is_dave is False:
            logger.info("Voice channel is NOT a DAVE (E2EE) call. Voice reception should work.")
        logger.info(f"Joined voice channel: {channel.name}")
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error(f"Failed to join voice channel after {elapsed:.1f}s: {type(e).__name__}: {e}")
    finally:
        _voice_connecting = False

# --- Voice Reconnection Loop ---
async def voice_reconnect_loop():
    """Periodically check if the bot is still connected to the voice channel
    and rejoin if the connection was lost (e.g., channel renamed/deleted,
    network blip, or Discord gateway reconnect)."""
    while True:
        await asyncio.sleep(30)
        channel = bot.get_channel(VOICE_CHANNEL_ID)
        if channel and not _voice_connecting:
            try:
                if channel.voice_client is not None:
                    continue
            except AttributeError:
                pass
            logger.info("Bot disconnected from voice channel. Rejoining...")
            await _do_voice_connect(channel)

# --- Inactivity Cleanup ---
async def cleanup_inactive_users():
    """Stop recording for users who have been inactive for a while."""
    while True:
        await asyncio.sleep(5)
        now = datetime.now()
        for user_id in list(speaking_users.keys()):
            if now - speaking_users[user_id]['last_activity'] > timedelta(seconds=10):
                member = bot.get_user(user_id)
                if member:
                    stop_recording(member)
                else:
                    _flush_user_buffer(user_id)

# --- Diagnostic Audio Report ---
async def diagnostic_audio_report():
    """Every 10 s, log the per-user byte counts in the WaveSink while users are
    speaking. This confirms whether audio is actually being captured (non-zero
    byte counts) or whether the sink is empty (voice connect timed out / DAVE
    / no packets received). Also logs the total number of users with audio in
    the sink.
    """
    while True:
        await asyncio.sleep(10)
        try:
            channel = bot.get_channel(VOICE_CHANNEL_ID)
            if not channel:
                continue
            vc = getattr(channel, 'voice_client', None)
            if not isinstance(vc, CustomVoiceClient):
                continue
            sink = vc._sink
            if sink is None:
                continue
            # Only report when recording is active (a reader is attached).
            if not vc.is_recording():
                continue
            audio_data = sink.audio_data
            if not audio_data:
                # No audio in the sink at all. This means no packets have been
                # received and decoded (voice connect timed out, DAVE, or the
                # user is silent). Log it so we can distinguish from the
                # "user is present but silent" case.
                if speaking_users:
                    logger.warning(
                        "Diagnostic: recording active but sink.audio_data is EMPTY "
                        f"while {len(speaking_users)} user(s) are tracked as speaking. "
                        "No audio packets have been received/decoded. Likely causes: "
                        "voice connect timed out, DAVE/E2EE, or no packets arriving."
                    )
                continue
            # Report per-user byte counts.
            parts = []
            total_bytes = 0
            for user_id, entry in audio_data.items():
                try:
                    nbytes = len(entry.file.getvalue())
                except Exception:
                    nbytes = -1
                total_bytes += max(0, nbytes)
                username = speaking_users.get(user_id, {}).get('username', f"uid:{user_id}")
                parts.append(f"{username}={nbytes}B")
            logger.info(
                f"Diagnostic: sink has {len(audio_data)} user(s) with audio, "
                f"total={total_bytes}B. Per-user: {', '.join(parts)}"
            )
        except Exception as e:
            logger.error(f"Error in diagnostic_audio_report: {type(e).__name__}: {e}")

# --- Unsent WAV Retry Task ---
async def retry_unsent_wavs():
    """Every 60 s, scan RECORDINGS_DIR for .wav files older than 5 minutes
    and re-attempt sending them. This recovers files that failed to transfer
    (e.g., tower was asleep and WoL/wait failed).

    Files already in flight (via _spawn_send) are skipped to avoid a
    double-send race.
    """
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            for fname in os.listdir(RECORDINGS_DIR):
                if not fname.endswith('.wav'):
                    continue
                # Skip files already being sent to avoid a double-send race.
                if fname in _inflight_sends:
                    continue
                fpath = os.path.join(RECORDINGS_DIR, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                if now - mtime > 300:  # older than 5 minutes
                    logger.info(f"Retrying unsent WAV: {fname}")
                    _inflight_sends.add(fname)
                    try:
                        await asyncio.to_thread(send_wav_file, fpath, "retry")
                    finally:
                        _inflight_sends.discard(fname)
        except Exception as e:
            logger.error(f"Error in retry_unsent_wavs: {e}")

# --- Persistent SSH Session Manager ---
# Keeps the tower awake while Discord voice activity warrants it.
# State machine:
#   ACTIVE  - >=1 user in the voice channel. Keep session alive. If user is
#             present but inactive/silent for 20 min -> CLOSED.
#   GRACE   - No user in the voice channel. Keep session alive for 5 min
#             (reconnection buffer). If a user re-enters within 5 min -> ACTIVE.
#             If 5 min elapse with no user -> CLOSED.
#   CLOSED  - Terminate the session. The tower sleeps on its own 20-min
#             inactivity rule. Re-awakened via WoL when the next transfer/entry needs it.
#
# Implementation: the manager holds a long-lived `ssh` subprocess (a no-op
# keepalive command) that stays open as long as the state is ACTIVE or GRACE.
# The SSH session itself keeps the tower awake (the tower stays awake while an
# SSH session is active and sleeps after ~20 min idle). When the state
# transitions to CLOSED, the subprocess is terminated and the tower is allowed
# to sleep. The next transfer/entry re-awakens the tower via WoL if needed.

ACTIVE_INACTIVITY_LIMIT = timedelta(minutes=20)
GRACE_LIMIT = timedelta(minutes=5)

# Track the last time a user was active (speaking) in the voice channel
last_user_activity = datetime.now()
# Track the last time a user was present in the voice channel
last_user_presence = datetime.now()

# The long-lived SSH keepalive subprocess (None when CLOSED)
_ssh_keepalive_proc = None

def update_user_activity():
    """Update the last user activity timestamp (called on genuine speaking events)."""
    global last_user_activity
    last_user_activity = datetime.now()

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
    """Background coroutine that maintains the SSH session to the tower."""
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

            # Check if the user has been inactive for 20 min
            if now - last_user_activity > ACTIVE_INACTIVITY_LIMIT:
                print("User present but inactive for 20 min. Closing SSH session.")
                close_ssh_keepalive()
            else:
                # Keep the session alive (retry if the proc died).
                if not _keepalive_alive():
                    open_ssh_keepalive()
        else:
            # No users in the channel -> GRACE state
            if now - last_user_presence > GRACE_LIMIT:
                print("No user in channel for 5 min. Closing SSH session.")
                close_ssh_keepalive()
            else:
                # Keep the session alive for the reconnection buffer.
                if not _keepalive_alive():
                    open_ssh_keepalive()

# --- Run the Bot ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is not set.")
    if VOICE_CHANNEL_ID == 0:
        raise ValueError("DISCORD_VOICE_CHANNEL_ID environment variable is not set.")

    # Validate required configuration
    if not TOWER_HOST:
        raise ValueError("TOWER_HOST environment variable is not set.")
    if not WOL_MAC_ADDRESS:
        raise ValueError("WOL_MAC_ADDRESS environment variable is not set.")
    if not TOWER_SSH_USER:
        raise ValueError("TOWER_SSH_USER environment variable is not set.")

    async def run_bot():
        cleanup_task = asyncio.create_task(cleanup_inactive_users())
        session_task = asyncio.create_task(ssh_session_manager())
        reconnect_task = asyncio.create_task(voice_reconnect_loop())
        retry_task = asyncio.create_task(retry_unsent_wavs())
        diagnostic_task = asyncio.create_task(diagnostic_audio_report())
        try:
            await bot.start(BOT_TOKEN)
        finally:
            cleanup_task.cancel()
            session_task.cancel()
            reconnect_task.cancel()
            retry_task.cancel()
            diagnostic_task.cancel()

    asyncio.run(run_bot())
