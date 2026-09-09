import asyncio
import json
import os
import shutil
import logging
import logging.handlers
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# All env-supplied directory paths are expanduser'd so that values like
# "~/discord_listener/incoming" resolve to the user's home directory rather
# than a literal "./~/..." relative path.
#
# The server is deployed at /mnt/hdd_storage/discord_listener (not the home
# directory), so the code derives its base path from the location of this
# file (server.py lives at <base>/Server_Listener/server.py). The default
# directory values are resolved relative to that base. Override any of them
# with an absolute path in .env if your layout differs.
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.dirname(_CODE_DIR)  # /mnt/hdd_storage/discord_listener
INCOMING_DIR = os.path.expanduser(os.getenv("INCOMING_DIR", os.path.join(_BASE_DIR, "Server_Listener", "incoming")))
PROCESSED_DIR = os.path.expanduser(os.getenv("PROCESSED_DIR", os.path.join(_BASE_DIR, "Server_Listener", "processed")))
FAILED_DIR = os.path.expanduser(os.getenv("FAILED_DIR", os.path.join(_BASE_DIR, "Server_Listener", "failed")))
LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", os.path.join(_BASE_DIR, "Server_Listener", "logs")))
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:latest")
LLM_SYSTEM_PROMPT = os.getenv("LLM_SYSTEM_PROMPT", "You are a helpful assistant. Summarize and analyze the provided transcription.")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# --- Audio Archive Configuration ---
# After the full pipeline succeeds, a copy of each WAV is archived to
# AUDIO_ARCHIVE_DIR with a timestamp suffix (the Pi reuses recording_N.wav
# names, so a plain copy would overwrite a previous archive entry). The
# archive is capped at AUDIO_RETENTION_GB; oldest files are deleted first
# when the limit is exceeded.
AUDIO_ARCHIVE_DIR = os.path.expanduser(os.getenv("AUDIO_ARCHIVE_DIR", os.path.join(_BASE_DIR, "Server_Listener", "archive")))
AUDIO_RETENTION_GB = float(os.getenv("AUDIO_RETENTION_GB", "5"))

# Ensure directories exist
os.makedirs(INCOMING_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(FAILED_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(AUDIO_ARCHIVE_DIR, exist_ok=True)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- JSONL Activity Log ---
ACTIVITY_LOG_PATH = os.path.join(LOG_DIR, "activity.jsonl")

def log_activity(record: dict):
    """Append a structured record to the JSONL activity log for review/debugging.

    Uses a RotatingFileHandler so the log does not grow unbounded. The active
    file is activity.jsonl; when it exceeds maxBytes it is rotated to
    activity.jsonl.1, .2, etc. (up to backupCount).
    """
    record['timestamp'] = datetime.now().isoformat()
    try:
        _get_activity_handler().emit(logging.LogRecord(
            name='activity', level=logging.INFO, pathname='', lineno=0,
            msg=json.dumps(record, ensure_ascii=False), args=None, exc_info=None,
        ))
    except Exception as e:
        logger.error(f"Failed to write activity log: {e}")

_activity_handler = None

def _get_activity_handler():
    """Return (and lazily create) the RotatingFileHandler for the activity log."""
    global _activity_handler
    if _activity_handler is None:
        _activity_handler = logging.handlers.RotatingFileHandler(
            ACTIVITY_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8',
        )
        # The handler writes raw msg strings (no formatting), so use a NullHandler-style
        # formatter that emits the message as-is.
        _activity_handler.setFormatter(logging.Formatter('%(message)s'))
    return _activity_handler

# --- Audio Archive Retention ---

def _enforce_audio_retention():
    """Delete oldest files from AUDIO_ARCHIVE_DIR until total size is under
    AUDIO_RETENTION_GB. Files are ordered by modification time (oldest first).

    Called after each successful pipeline completion so the archive never
    grows past the configured limit.
    """
    try:
        files = []
        for f in os.listdir(AUDIO_ARCHIVE_DIR):
            fpath = os.path.join(AUDIO_ARCHIVE_DIR, f)
            if os.path.isfile(fpath):
                files.append((os.path.getmtime(fpath), fpath, os.path.getsize(fpath)))

        # Sort oldest-first
        files.sort()

        total_size = sum(size for _, _, size in files)
        max_bytes = int(AUDIO_RETENTION_GB * 1024 * 1024 * 1024)

        if total_size <= max_bytes:
            return

        logger.info(f"Archive at {total_size / (1024**3):.2f} GB exceeds {AUDIO_RETENTION_GB} GB limit. Pruning oldest files...")
        for mtime, fpath, size in files:
            if total_size <= max_bytes:
                break
            os.remove(fpath)
            total_size -= size
            logger.info(f"Retention: deleted {os.path.basename(fpath)} ({size / (1024**2):.1f} MB)")

        logger.info(f"Archive pruned to {total_size / (1024**3):.2f} GB.")
    except Exception as e:
        logger.error(f"Error enforcing audio retention: {e}")


# --- Whisper Model (loaded once at startup) ---
_whisper_model = None

def get_whisper_model():
    """Lazily load the Whisper model once and reuse it for all transcriptions."""
    global _whisper_model
    if _whisper_model is None:
        import whisper
        logger.info(f"Loading Whisper model: {WHISPER_MODEL_SIZE}")
        _whisper_model = whisper.load_model(WHISPER_MODEL_SIZE)
        logger.info("Whisper model loaded.")
    return _whisper_model

# --- Transcription (blocking; always run via asyncio.to_thread) ---
def transcribe_audio(wav_file: str) -> str:
    """Transcribe the WAV file using Whisper. Returns the transcription text.

    BLOCKING: Whisper on CPU can take seconds to minutes. Callers must use
    asyncio.to_thread() so the event loop is never stalled.
    """
    try:
        model = get_whisper_model()
        result = model.transcribe(wav_file, fp16=False)
        transcription = result['text'].strip()
        logger.info(f"Transcription complete for {wav_file}")
        return transcription
    except Exception as e:
        logger.error(f"Error transcribing {wav_file}: {e}")
        return ""

# --- LLM Processing (blocking; always run via asyncio.to_thread) ---
def process_with_llm(transcription: str) -> str:
    """Send the transcription to an LLM for processing. Returns the LLM response.

    BLOCKING: makes a network call. Callers must use asyncio.to_thread().
    """
    if not transcription:
        return ""

    try:
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
        logger.info("LLM processing complete")
        return llm_response
    except Exception as e:
        logger.error(f"Error processing with LLM: {e}")
        return ""

# --- Persistent Discord Client ---
_discord_client = None
_discord_started = False

def get_discord_client():
    """Return the persistent Discord client, creating it on first use."""
    global _discord_client
    if _discord_client is None:
        import py_cord as discord
        intents = discord.Intents.default()
        # Note: we do NOT enable message_content. The server only sends
        # messages; enabling a privileged intent that is not turned on in the
        # Developer Portal would break login with PrivilegedIntentsRequired.
        _discord_client = discord.Client(intents=intents)
    return _discord_client

_discord_start_task = None

async def ensure_discord_connected():
    """Ensure the persistent Discord client is connected and ready."""
    global _discord_started, _discord_start_task
    client = get_discord_client()
    if client.is_ready():
        return True

    # Start the client in the background if not already running. We track a
    # module-level flag rather than calling client.is_connecting() (which may
    # not exist on discord.Client depending on version). The task is retained
    # so a failure (e.g., invalid token) is logged rather than silently
    # swallowed.
    if not _discord_started:
        _discord_started = True

        async def _start_discord():
            try:
                await client.start(DISCORD_BOT_TOKEN, log_handler=None)
            except Exception as e:
                logger.error(f"Discord client start failed: {e}")
                # Reset the flag so a later call can retry.
                global _discord_started
                _discord_started = False

        _discord_start_task = asyncio.create_task(_start_discord())

    # Wait for the client to be ready (with a timeout)
    try:
        await asyncio.wait_for(client.wait_until_ready(), timeout=30)
        return True
    except asyncio.TimeoutError:
        logger.error("Discord client did not become ready in time")
        return False

# --- Message chunking (Discord's limit is 2000 chars) ---
def _chunk_message(text: str, limit: int = 2000):
    """Split text into chunks of at most `limit` chars. Prefers splitting on
    newlines; hard-splits if a single line exceeds the limit."""
    chunks = []
    for line in text.split('\n'):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if chunks and len(chunks[-1]) + 1 + len(line) <= limit:
            chunks[-1] = chunks[-1] + '\n' + line
        else:
            chunks.append(line)
    return [c for c in chunks if c]

async def push_to_discord(llm_response: str) -> bool:
    """Push the LLM response into the Discord server's chat room using the persistent client.
    Long responses are chunked at 2000 chars (Discord's message limit).
    Returns True on success, False on failure."""
    if not llm_response:
        return False

    try:
        if not await ensure_discord_connected():
            return False

        client = get_discord_client()
        channel = client.get_channel(DISCORD_CHANNEL_ID)
        if channel:
            for chunk in _chunk_message(llm_response):
                await channel.send(chunk)
            logger.info(f"Pushed response to Discord channel {DISCORD_CHANNEL_ID}")
            return True
        else:
            logger.error(f"Discord channel {DISCORD_CHANNEL_ID} not found")
            return False
    except Exception as e:
        logger.error(f"Error pushing to Discord: {e}")
        return False

# --- Retry Tracking ---
# {filename: retry_count}
retry_counts = {}
# Track filenames we have seen so we can reset the count when a file with a
# reused name first appears (the Pi reuses recording_N.wav names across the
# session; a new file should not inherit the old file's retry count).
seen_filenames = set()

# Cap on the number of entries to keep in retry_counts / seen_filenames so
# they do not grow unbounded over the life of the process.
_MAX_TRACKED = 1000

def _prune_tracking():
    """Prune retry_counts and seen_filenames to files that are still present
    in INCOMING_DIR, and cap the structures so they do not grow unbounded.
    Called periodically from the file watcher."""
    try:
        present = set()
        for f in os.listdir(INCOMING_DIR):
            if f.endswith('.wav'):
                present.add(f)
        # Remove entries for files no longer present in the incoming dir.
        for fname in list(retry_counts.keys()):
            if fname not in present:
                retry_counts.pop(fname, None)
        for fname in list(seen_filenames):
            if fname not in present:
                seen_filenames.discard(fname)
        # Cap the structures if they still exceed the limit.
        if len(retry_counts) > _MAX_TRACKED:
            for fname in list(retry_counts.keys())[:len(retry_counts) - _MAX_TRACKED]:
                retry_counts.pop(fname, None)
        if len(seen_filenames) > _MAX_TRACKED:
            for fname in list(seen_filenames)[:len(seen_filenames) - _MAX_TRACKED]:
                seen_filenames.discard(fname)
    except Exception as e:
        logger.error(f"Error pruning tracking structures: {e}")

def get_retry_count(filename: str) -> int:
    return retry_counts.get(filename, 0)

def increment_retry_count(filename: str) -> int:
    retry_counts[filename] = get_retry_count(filename) + 1
    return retry_counts[filename]

def clear_retry_count(filename: str):
    retry_counts.pop(filename, None)

def _note_first_seen(filename: str):
    """If this filename is being seen for the first time, reset its retry
    count to 0 (a new file with a reused name should not inherit the old
    file's retry count)."""
    if filename not in seen_filenames:
        seen_filenames.add(filename)
        retry_counts[filename] = 0

# --- File Watcher ---
async def watch_incoming_directory():
    """Monitor the incoming directory for new WAV files and process them.

    After the full pipeline (transcription -> LLM -> Discord push) completes
    successfully, a copy of the WAV is archived to AUDIO_ARCHIVE_DIR with a
    timestamp suffix, the original is removed from INCOMING_DIR, and the
    archive is pruned to stay under AUDIO_RETENTION_GB. If any step fails,
    the file remains in the incoming directory for retry on the next poll
    cycle. After MAX_RETRIES failures, the file is moved to the failed
    directory to prevent infinite retry loops.
    """
    logger.info(f"Watching incoming directory: {INCOMING_DIR}")

    while True:
        try:
            # Prune tracking structures periodically so they do not grow
            # unbounded over the life of the process.
            _prune_tracking()

            # List all WAV files in the incoming directory (only .wav files;
            # .part files are still being written by the Pi's atomic SCP and
            # are ignored until renamed to .wav on the tower).
            wav_files = [f for f in os.listdir(INCOMING_DIR) if f.endswith('.wav')]

            for wav_file in wav_files:
                wav_path = os.path.join(INCOMING_DIR, wav_file)
                logger.info(f"Processing new WAV file: {wav_file}")

                # Reset retry count if this filename is being seen for the
                # first time (handles the Pi reusing recording_N.wav names).
                _note_first_seen(wav_file)

                # Transcribe the audio (on a worker thread so the loop is
                # never stalled by a long CPU-bound transcription).
                transcription = await asyncio.to_thread(transcribe_audio, wav_path)
                if not transcription:
                    retries = increment_retry_count(wav_file)
                    logger.warning(f"No transcription for {wav_file}. Retry {retries}/{MAX_RETRIES}.")
                    log_activity({'file': wav_file, 'stage': 'transcription', 'status': 'failed', 'retries': retries})
                    if retries >= MAX_RETRIES:
                        failed_path = os.path.join(FAILED_DIR, wav_file)
                        if os.path.exists(failed_path):
                            # Avoid overwriting a previously quarantined file
                            # with the same name; append a timestamp suffix.
                            ts = datetime.now().strftime('%Y%m%d%H%M%S')
                            failed_path = os.path.join(FAILED_DIR, f"{wav_file}.{ts}")
                        os.rename(wav_path, failed_path)
                        logger.error(f"Moved {wav_file} to failed directory after {MAX_RETRIES} retries.")
                        clear_retry_count(wav_file)
                    continue

                # Process with LLM (on a worker thread so the loop is never
                # stalled by a network call).
                llm_response = await asyncio.to_thread(process_with_llm, transcription)
                if not llm_response:
                    retries = increment_retry_count(wav_file)
                    logger.warning(f"No LLM response for {wav_file}. Retry {retries}/{MAX_RETRIES}.")
                    log_activity({'file': wav_file, 'stage': 'llm', 'status': 'failed', 'retries': retries, 'transcription': transcription})
                    if retries >= MAX_RETRIES:
                        failed_path = os.path.join(FAILED_DIR, wav_file)
                        if os.path.exists(failed_path):
                            # Avoid overwriting a previously quarantined file
                            # with the same name; append a timestamp suffix.
                            ts = datetime.now().strftime('%Y%m%d%H%M%S')
                            failed_path = os.path.join(FAILED_DIR, f"{wav_file}.{ts}")
                        os.rename(wav_path, failed_path)
                        logger.error(f"Moved {wav_file} to failed directory after {MAX_RETRIES} retries.")
                        clear_retry_count(wav_file)
                    continue

                # Push to Discord
                discord_ok = await push_to_discord(llm_response)
                if not discord_ok:
                    retries = increment_retry_count(wav_file)
                    logger.warning(f"Discord push failed for {wav_file}. Retry {retries}/{MAX_RETRIES}.")
                    log_activity({'file': wav_file, 'stage': 'discord', 'status': 'failed', 'retries': retries, 'transcription': transcription, 'llm_response': llm_response})
                    if retries >= MAX_RETRIES:
                        failed_path = os.path.join(FAILED_DIR, wav_file)
                        if os.path.exists(failed_path):
                            # Avoid overwriting a previously quarantined file
                            # with the same name; append a timestamp suffix.
                            ts = datetime.now().strftime('%Y%m%d%H%M%S')
                            failed_path = os.path.join(FAILED_DIR, f"{wav_file}.{ts}")
                        os.rename(wav_path, failed_path)
                        logger.error(f"Moved {wav_file} to failed directory after {MAX_RETRIES} retries.")
                        clear_retry_count(wav_file)
                    continue

                # Full pipeline succeeded — archive a copy, clean up incoming, enforce retention
                log_activity({'file': wav_file, 'stage': 'complete', 'status': 'success',
                              'transcription': transcription, 'llm_response': llm_response})
                clear_retry_count(wav_file)

                # Copy to archive with a timestamp suffix (the Pi reuses recording_N.wav
                # names, so a plain copy would overwrite a previous archive entry).
                ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                archive_name = f"{wav_file}.{ts}"
                archive_path = os.path.join(AUDIO_ARCHIVE_DIR, archive_name)
                shutil.copy2(wav_path, archive_path)
                logger.info(f"Archived {wav_file} -> {archive_name}")

                # Remove from incoming (replaces the old move-to-processed)
                os.remove(wav_path)

                # Enforce the retention cap
                _enforce_audio_retention()

        except Exception as e:
            logger.error(f"Error in file watcher: {e}")

        # Sleep to avoid busy-waiting
        await asyncio.sleep(5)

# --- Run the Server ---
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is not set.")
    if DISCORD_CHANNEL_ID == 0:
        raise ValueError("DISCORD_CHANNEL_ID environment variable is not set.")
    import urllib.request
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
    except Exception as e:
        raise ValueError(f"Ollama not reachable at {OLLAMA_BASE_URL}: {e}")

    async def run_server():
        # Preload the Whisper model on a worker thread before starting the
        # watch loop so the first file is not delayed by the model load.
        logger.info("Preloading Whisper model...")
        await asyncio.to_thread(get_whisper_model)
        logger.info("Whisper model preloaded.")
        try:
            await watch_incoming_directory()
        finally:
            # Clean up the Discord start task if it is still running.
            if _discord_start_task is not None and not _discord_start_task.done():
                _discord_start_task.cancel()

    asyncio.run(run_server())
