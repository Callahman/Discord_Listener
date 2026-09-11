import asyncio
import logging
import os
import struct
import time
from datetime import datetime, timedelta

import discord
import discord.voice
from discord.sinks import WaveSink

logger = logging.getLogger(__name__)

# --- Voice Bot Configuration ---
VOICE_CHANNEL_ID = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", "0"))
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID", "0"))
RECORDINGS_DIR = os.path.expanduser(os.getenv("RECORDINGS_DIR", os.path.join(os.path.expanduser("~"), "discord_listener", "recordings")))

# Ensure recordings directory exists
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# --- Keep-Awake Configuration ---
# The tower's idle-shutdown script checks /var/run/keep-awake.d/ for lockfiles.
# While the tower-bot is active (users in VC or text channel), it drops a
# lockfile there to prevent the tower from shutting down.
KEEP_AWAKE_DIR = os.getenv("KEEP_AWAKE_DIR", "/var/run/keep-awake.d")
KEEP_AWAKE_LOCKFILE = "discord_tower_bot"
KEEP_AWAKE_GRACE_SECONDS = int(os.getenv("KEEP_AWAKE_GRACE_SECONDS", "300"))  # 5 min grace buffer

# --- State Tracking ---
# {user_id: {'start_time': datetime, 'last_activity': datetime, 'username': str}}
speaking_users = {}

# --- Keep-Awake State ---
# Tracks the last time any user was active (in VC or sent a text message).
# The keep-awake loop checks this timestamp and drops/removes the lockfile
# based on whether the grace period has elapsed.
_last_activity = None  # datetime or None
_keep_awake_lockfile_path = None  # path to the current lockfile, or None

# --- Keep-Awake Loop ---
async def keep_awake_loop():
    """Drop or remove the keep-awake lockfile based on user activity.

    While any user is active (in the VC or sent a text message) and the grace
    period has not elapsed, the lockfile is present in /var/run/keep-awake.d/.
    The tower's idle-shutdown script checks that directory and resets its idle
    counter if any file is present.

    The lockfile is dropped when activity is detected and removed after the
    grace period (5 min) has elapsed with no further activity.
    """
    global _keep_awake_lockfile_path, _last_activity

    # Remove a stale lockfile left by a previous (crashed) run. /var/run is tmpfs
    # so reboot clears it; this covers crash-without-reboot.
    try:
        stale = os.path.join(KEEP_AWAKE_DIR, KEEP_AWAKE_LOCKFILE)
        if os.path.exists(stale):
            os.remove(stale)
            logger.info(f"Removed stale keep-awake lockfile: {stale}")
    except Exception as e:
        logger.warning(f"Could not check/remove stale lockfile: {type(e).__name__}: {e}")

    while True:
        await asyncio.sleep(30)  # Check every 30 seconds

        now = datetime.now()

        # Is anyone in the target voice channel RIGHT NOW? Live occupancy is the
        # source of truth for VC presence (a timestamp alone goes stale if a user
        # sits in the VC longer than the grace period).
        vc_present = False
        try:
            channel = discord_client.get_channel(VOICE_CHANNEL_ID)
            if channel:
                for vs in channel.voice_states:
                    if vs.user and not vs.user.bot:
                        vc_present = True
                        break
        except Exception:
            pass

        if vc_present:
            # Live presence keeps activity fresh; the grace window then runs from
            # the moment the last user leaves.
            _last_activity = now
            should_keep_awake = True
        else:
            # No one in the VC — rely on the timestamp (last leave or last text).
            should_keep_awake = False
            if _last_activity is not None:
                elapsed = (now - _last_activity).total_seconds()
                if elapsed < KEEP_AWAKE_GRACE_SECONDS:
                    should_keep_awake = True

        if should_keep_awake:
            # Drop the lockfile if it's not already present.
            if _keep_awake_lockfile_path is None:
                try:
                    os.makedirs(KEEP_AWAKE_DIR, exist_ok=True)
                    lockfile_path = os.path.join(KEEP_AWAKE_DIR, KEEP_AWAKE_LOCKFILE)
                    # Write a timestamp to the lockfile for debugging.
                    with open(lockfile_path, 'w') as f:
                        f.write(str(now))
                    _keep_awake_lockfile_path = lockfile_path
                    logger.info(f"Keep-awake lockfile dropped: {lockfile_path}")
                except Exception as e:
                    logger.error(f"Failed to drop keep-awake lockfile: {type(e).__name__}: {e}")
        else:
            # Remove the lockfile if it's present.
            if _keep_awake_lockfile_path is not None:
                try:
                    if os.path.exists(_keep_awake_lockfile_path):
                        os.remove(_keep_awake_lockfile_path)
                    logger.info(f"Keep-awake lockfile removed: {_keep_awake_lockfile_path}")
                    _keep_awake_lockfile_path = None
                except Exception as e:
                    logger.error(f"Failed to remove keep-awake lockfile: {type(e).__name__}: {e}")

# --- Voice Connect Guard (prevents double-connect race) ---
_voice_connecting = False

# --- Atomic WAV Path Allocation ---
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

# --- WAV Write Helper (blocking; always run via asyncio.to_thread) ---
def _write_wav(audio_data: bytes, username: str) -> str:
    """Write the audio data to an atomically-claimed WAV path.

    The WaveSink produces raw PCM data (16-bit 48kHz mono) when read mid-recording
    (before Sink.cleanup() has run format_audio() to add a WAV header). We wrap the
    PCM in a WAV header here so Whisper can transcribe it.

    If the audio already has a WAV header (e.g., if format_audio() was called), the
    'RIFF' magic bytes will be present at the start. In that case, write the data
    as-is. Otherwise, add a WAV header.

    Returns the path of the written WAV file.
    """
    wav_path = get_next_wav_path()
    with open(wav_path, 'wb') as f:
        if audio_data[:4] == b'RIFF':
            # Already has a WAV header — write as-is.
            f.write(audio_data)
        else:
            # Raw PCM — wrap in a WAV header (16-bit 48kHz mono).
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
    logger.info(f"Saved WAV file: {wav_path} (speaker: {username})")
    return wav_path

# --- Custom Voice Client: real audio capture via start_listening() + WaveSink ---
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

    def start_listening(self, sink=None, callback=None):
        """Start receiving audio via the documented py-cord API.

        If no sink is provided, uses the internal WaveSink.
        """
        if sink is None:
            sink = self._sink
        super().start_listening(sink, callback)

# --- Recording Start/Stop ---
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
    logger.info(f"Started recording for: {member.name}")

def stop_recording(member):
    """Stop recording for a user and save the WAV file.

    Retrieves the user's recorded audio from the WaveSink by reading the
    sink's audio_data entry directly (get_user_audio() is broken in this
    py-cord version) and writes it to RECORDINGS_DIR via asyncio.to_thread.
    """
    if member.id not in speaking_users:
        return

    user_data = speaking_users.pop(member.id)
    username = user_data['username']

    # Get the VoiceClient to access the WaveSink.
    channel = discord_client.get_channel(VOICE_CHANNEL_ID)
    vc = channel.voice_client if channel else None
    if not isinstance(vc, CustomVoiceClient):
        logger.warning(f"No voice client available to retrieve audio for {username}")
        return

    sink = vc._sink
    if sink is None:
        logger.warning(f"No sink available to retrieve audio for {username}")
        return

    try:
        entry = sink.audio_data.get(member.id)
        if entry is None:
            logger.warning(f"No audio recorded for: {username}")
            return

        audio_data = entry.file.getvalue()

        if not audio_data:
            logger.warning(f"No audio recorded for: {username}")
            return

        # Write the WAV file on a worker thread so the event loop is never
        # stalled by disk I/O.
        async def _do_write():
            wav_path = await asyncio.to_thread(_write_wav, audio_data, username)
            # The WAV file is now in RECORDINGS_DIR. The server's file watcher
            # will pick it up and process it (transcribe -> LLM -> Discord).
            logger.info(f"WAV file ready for processing: {wav_path} (speaker: {username})")

        asyncio.create_task(_do_write())
    except Exception as e:
        logger.error(f"Error saving WAV file for {username}: {e}")

# --- Flush helper for users not in cache ---
def _flush_user_buffer(user_id: int):
    """Save the buffer for a user whose object is no longer in cache.

    Retrieves the user's recorded audio from the WaveSink by reading the
    sink's audio_data entry directly and writes it to RECORDINGS_DIR via
    asyncio.to_thread.
    """
    if user_id not in speaking_users:
        return
    user_data = speaking_users.pop(user_id)
    username = user_data['username']

    # Get the VoiceClient to access the WaveSink.
    channel = discord_client.get_channel(VOICE_CHANNEL_ID)
    vc = channel.voice_client if channel else None
    if not isinstance(vc, CustomVoiceClient):
        logger.warning(f"No voice client available to retrieve audio for {username} (user not in cache)")
        return

    sink = vc._sink
    if sink is None:
        logger.warning(f"No sink available to retrieve audio for {username} (user not in cache)")
        return

    try:
        entry = sink.audio_data.get(user_id)
        if entry is None:
            logger.warning(f"No audio recorded for: {username} (user not in cache)")
            return

        audio_data = entry.file.getvalue()

        if not audio_data:
            logger.warning(f"No audio recorded for: {username} (user not in cache)")
            return

        async def _do_write():
            wav_path = await asyncio.to_thread(_write_wav, audio_data, username)
            logger.info(f"WAV file ready for processing: {wav_path} (speaker: {username}, user not in cache)")

        asyncio.create_task(_do_write())
    except Exception as e:
        logger.error(f"Error saving WAV file for {username}: {e}")

# --- Voice Connect ---
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
                "blocked during connect. This risks the 60s voice connect timeout and "
                "prevents audio from being received."
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
                "BROKEN for DAVE calls. Even if capture works, you will get OPUS_SILENCE. "
                "Audio will NOT be transcribed."
            )
        elif is_dave is False:
            logger.info("Voice channel is NOT a DAVE (E2EE) call. Voice reception should work.")
        logger.info(f"Joined voice channel: {channel.name}")

        # Safety net: enumerate users ALREADY in the channel. on_voice_state_update
        # only fires on changes, so existing users would otherwise be missed for
        # audio (and keep-awake). Start recording for any untracked non-bot user.
        global _last_activity
        for vs in channel.voice_states:
            if vs.user and not vs.user.bot:
                _last_activity = datetime.now()
                if vs.user.id not in speaking_users:
                    start_recording(vs.user)
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error(f"Failed to join voice channel after {elapsed:.1f}s: {type(e).__name__}: {e}")
    finally:
        _voice_connecting = False

# --- Voice Reconnection Loop ---
async def voice_reconnect_loop():
    """Periodically check if the bot is still connected to the voice channel
    and rejoin if the connection was lost."""
    while True:
        await asyncio.sleep(30)
        channel = discord_client.get_channel(VOICE_CHANNEL_ID)
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
                member = discord_client.get_user(user_id)
                if member:
                    stop_recording(member)
                else:
                    _flush_user_buffer(user_id)

# --- Diagnostic Audio Report ---
async def diagnostic_audio_report():
    """Every 10 s, log the per-user byte counts in the WaveSink while users are
    speaking. Uses entry.file.tell() instead of getvalue() to avoid full buffer
    copies on the event loop.
    """
    while True:
        await asyncio.sleep(10)
        try:
            channel = discord_client.get_channel(VOICE_CHANNEL_ID)
            if not channel:
                continue
            vc = getattr(channel, 'voice_client', None)
            if not isinstance(vc, CustomVoiceClient):
                continue
            sink = vc._sink
            if sink is None:
                continue
            if not vc.is_recording():
                continue
            audio_data = sink.audio_data
            if not audio_data:
                if speaking_users:
                    logger.warning(
                        "Diagnostic: recording active but sink.audio_data is EMPTY "
                        f"while {len(speaking_users)} user(s) are tracked as speaking. "
                        "No audio packets have been received/decoded. Likely causes: "
                        "voice connect timed out, DAVE/E2EE, or no packets arriving."
                    )
                continue
            parts = []
            total_bytes = 0
            for user_id, entry in audio_data.items():
                try:
                    # Use tell() to get the current position without copying the buffer.
                    nbytes = entry.file.tell()
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

# --- Voice Bot Event Handlers ---
# These are registered on the Discord client by the server module.

def register_voice_handlers(client):
    """Register the voice bot event handlers on the given Discord client.

    This function is called by the server module after creating the Discord
    client. It sets the module-level discord_client reference and registers
    the event handlers.
    """
    global discord_client
    discord_client = client

    @client.event
    async def on_voice_state_update(member, before, after):
        """Handle voice state updates for leave/move/mute/deaf handling."""
        if member.bot:
            return

        global _last_activity

        # If the user left the target channel OR moved to another channel, stop
        # recording immediately.
        if before.channel and before.channel.id == VOICE_CHANNEL_ID and \
                (after.channel is None or after.channel.id != VOICE_CHANNEL_ID):
            if member.id in speaking_users:
                stop_recording(member)
            return

        # Check if the user is in the target voice channel.
        if not after.channel or after.channel.id != VOICE_CHANNEL_ID:
            return

        # Check mute/deaf status. If muted/deaf, stop recording if they were
        # speaking.
        if after.self_mute or after.deaf:
            if member.id in speaking_users:
                stop_recording(member)
            return

        # User is present in the target voice channel — update keep-awake activity.
        _last_activity = datetime.now()

        # Safety net: if the speaking op never fired for this user, start
        # recording here so we don't miss their audio.
        if member.id not in speaking_users:
            start_recording(member)

    @client.event
    async def on_member_speaking_state_update(member, ssrc, state):
        """Handle the voice-gateway speaking op (real per-user start/stop)."""
        if member is None or member.bot:
            return

        channel = client.get_channel(VOICE_CHANNEL_ID)
        if channel is None:
            return
        in_target = False
        for vs in channel.voice_states:
            if vs.user and vs.user.id == member.id:
                in_target = True
                break
        if not in_target:
            return

        is_speaking = bool(int(state) & 1)

        if is_speaking:
            if member.id not in speaking_users:
                start_recording(member)
            else:
                speaking_users[member.id]['last_activity'] = datetime.now()
        else:
            if member.id in speaking_users:
                stop_recording(member)

def update_keep_awake_activity():
    """Update the keep-awake activity timestamp.

    Call this from the server's on_message handler when a user sends a message
    in the target text channel. This keeps the tower awake while text activity
    is occurring.
    """
    global _last_activity
    _last_activity = datetime.now()

# Module-level reference to the Discord client (set by register_voice_handlers)
discord_client = None
