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
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.dirname(_CODE_DIR)
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

# --- Voice Bot Configuration ---
VOICE_CHANNEL_ID = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", "0"))
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID", "0"))
RECORDINGS_DIR = os.path.expanduser(os.getenv("RECORDINGS_DIR", os.path.join(_BASE_DIR, "Server_Listener", "recordings")))

# --- Audio Archive Configuration ---
AUDIO_ARCHIVE_DIR = os.path.expanduser(os.getenv("AUDIO_ARCHIVE_DIR", os.path.join(_BASE_DIR, "Server_Listener", "archive")))
AUDIO_RETENTION_GB = float(os.getenv("AUDIO_RETENTION_GB", "5"))

# Ensure directories exist
os.makedirs(INCOMING_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(FAILED_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(AUDIO_ARCHIVE_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- JSONL Activity Log ---
ACTIVITY_LOG_PATH = os.path.join(LOG_DIR, "activity.jsonl")

def log_activity(record: dict):
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
    global _activity_handler
    if _activity_handler is None:
        _activity_handler = logging.handlers.RotatingFileHandler(
            ACTIVITY_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8',
        )
        _activity_handler.setFormatter(logging.Formatter('%(message)s'))
    return _activity_handler

# --- Audio Archive Retention ---

def _enforce_audio_retention():
    try:
        files = []
        for f in os.listdir(AUDIO_ARCHIVE_DIR):
            fpath = os.path.join(AUDIO_ARCHIVE_DIR, f)
            if os.path.isfile(fpath):
                files.append((os.path.getmtime(fpath), fpath, os.path.getsize(fpath)))
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
    global _whisper_model
    if _whisper_model is None:
        import whisper
        logger.info(f"Loading Whisper model: {WHISPER_MODEL_SIZE}")
        _whisper_model = whisper.load_model(WHISPER_MODEL_SIZE)
        logger.info("Whisper model loaded.")
    return _whisper_model

# --- Transcription (blocking; always run via asyncio.to_thread) ---
def transcribe_audio(wav_file: str) -> str:
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

# --- Discord Client ---
import discord

intents = discord.Intents.default()
intents.members = True
intents.presences = True

discord_client = discord.Client(intents=intents)

# --- Voice Bot Registration ---
from voice_bot import register_voice_handlers, voice_reconnect_loop, diagnostic_audio_report, cleanup_inactive_users, _do_voice_connect

register_voice_handlers(discord_client)

@discord_client.event
async def on_ready():
    logger.info(f"Logged in as {discord_client.user}")
    # Connect to the voice channel
    if VOICE_CHANNEL_ID:
        channel = discord_client.get_channel(VOICE_CHANNEL_ID)
        if channel:
            await _do_voice_connect(channel)
        else:
            logger.warning(f"Voice channel with ID {VOICE_CHANNEL_ID} not found.")

# --- Text Channel Reader ---
@discord_client.event
async def on_message(message):
    """Read user messages from the target text channel and process them with the LLM.

    No voice join needed for the text path.
    """
    if message.author.bot:
        return
    if message.channel.id != TEXT_CHANNEL_ID:
        return
    
    text = message.content.strip()
    if not text:
        return
    
    logger.info(f"Text message from {message.author.name}: {text[:100]}")
    log_activity({'user': message.author.name, 'channel': TEXT_CHANNEL_ID, 'text': text, 'stage': 'text_received'})
    
    # Process with LLM (on a worker thread)
    llm_response = await asyncio.to_thread(process_with_llm, text)
    if not llm_response:
        logger.error(f"No LLM response for text message from {message.author.name}")
        log_activity({'user': message.author.name, 'channel': TEXT_CHANNEL_ID, 'text': text, 'stage': 'llm', 'status': 'failed'})
        return
    
    # Push to Discord
    discord_ok = await push_to_discord(llm_response)
    if not discord_ok:
        logger.error(f"Discord push failed for text message from {message.author.name}")
        log_activity({'user': message.author.name, 'channel': TEXT_CHANNEL_ID, 'text': text, 'stage': 'discord', 'status': 'failed', 'llm_response': llm_response})
        return
    
    log_activity({'user': message.author.name, 'channel': TEXT_CHANNEL_ID, 'text': text, 'stage': 'complete', 'status': 'success', 'llm_response': llm_response})

# --- Message chunking ---
def _chunk_message(text: str, limit: int = 2000):
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
    if not llm_response:
        return False
    try:
        if not discord_client.is_ready():
            logger.error("Discord client not ready")
            return False
        channel = discord_client.get_channel(DISCORD_CHANNEL_ID)
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
retry_counts = {}
seen_filenames = set()
_MAX_TRACKED = 1000

def _prune_tracking():
    try:
        present = set()
        for f in os.listdir(INCOMING_DIR):
            if f.endswith('.wav'):
                present.add(f)
        for fname in list(retry_counts.keys()):
            if fname not in present:
                retry_counts.pop(fname, None)
        for fname in list(seen_filenames):
            if fname not in present:
                seen_filenames.discard(fname)
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
    if filename not in seen_filenames:
        seen_filenames.add(filename)
        retry_counts[filename] = 0

# --- File Watcher ---
async def watch_incoming_directory():
    """Monitor the incoming directory for new WAV files and process them.

    The voice bot writes WAV files to RECORDINGS_DIR. This watcher monitors
    INCOMING_DIR (which can be set to RECORDINGS_DIR or a separate directory)
    for new WAV files and processes them through the pipeline.
    """
    logger.info(f"Watching incoming directory: {INCOMING_DIR}")

    while True:
        try:
            _prune_tracking()
            wav_files = [f for f in os.listdir(INCOMING_DIR) if f.endswith('.wav')]

            for wav_file in wav_files:
                wav_path = os.path.join(INCOMING_DIR, wav_file)
                logger.info(f"Processing new WAV file: {wav_file}")

                _note_first_seen(wav_file)

                transcription = await asyncio.to_thread(transcribe_audio, wav_path)
                if not transcription:
                    retries = increment_retry_count(wav_file)
                    logger.warning(f"No transcription for {wav_file}. Retry {retries}/{MAX_RETRIES}.")
                    log_activity({'file': wav_file, 'stage': 'transcription', 'status': 'failed', 'retries': retries})
                    if retries >= MAX_RETRIES:
                        failed_path = os.path.join(FAILED_DIR, wav_file)
                        if os.path.exists(failed_path):
                            ts = datetime.now().strftime('%Y%m%d%H%M%S')
                            failed_path = os.path.join(FAILED_DIR, f"{wav_file}.{ts}")
                        os.rename(wav_path, failed_path)
                        logger.error(f"Moved {wav_file} to failed directory after {MAX_RETRIES} retries.")
                        clear_retry_count(wav_file)
                    continue

                llm_response = await asyncio.to_thread(process_with_llm, transcription)
                if not llm_response:
                    retries = increment_retry_count(wav_file)
                    logger.warning(f"No LLM response for {wav_file}. Retry {retries}/{MAX_RETRIES}.")
                    log_activity({'file': wav_file, 'stage': 'llm', 'status': 'failed', 'retries': retries, 'transcription': transcription})
                    if retries >= MAX_RETRIES:
                        failed_path = os.path.join(FAILED_DIR, wav_file)
                        if os.path.exists(failed_path):
                            ts = datetime.now().strftime('%Y%m%d%H%M%S')
                            failed_path = os.path.join(FAILED_DIR, f"{wav_file}.{ts}")
                        os.rename(wav_path, failed_path)
                        logger.error(f"Moved {wav_file} to failed directory after {MAX_RETRIES} retries.")
                        clear_retry_count(wav_file)
                    continue

                discord_ok = await push_to_discord(llm_response)
                if not discord_ok:
                    retries = increment_retry_count(wav_file)
                    logger.warning(f"Discord push failed for {wav_file}. Retry {retries}/{MAX_RETRIES}.")
                    log_activity({'file': wav_file, 'stage': 'discord', 'status': 'failed', 'retries': retries, 'transcription': transcription, 'llm_response': llm_response})
                    if retries >= MAX_RETRIES:
                        failed_path = os.path.join(FAILED_DIR, wav_file)
                        if os.path.exists(failed_path):
                            ts = datetime.now().strftime('%Y%m%d%H%M%S')
                            failed_path = os.path.join(FAILED_DIR, f"{wav_file}.{ts}")
                        os.rename(wav_path, failed_path)
                        logger.error(f"Moved {wav_file} to failed directory after {MAX_RETRIES} retries.")
                        clear_retry_count(wav_file)
                    continue

                log_activity({'file': wav_file, 'stage': 'complete', 'status': 'success',
                              'transcription': transcription, 'llm_response': llm_response})
                clear_retry_count(wav_file)

                ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                archive_name = f"{wav_file}.{ts}"
                archive_path = os.path.join(AUDIO_ARCHIVE_DIR, archive_name)
                shutil.copy2(wav_path, archive_path)
                logger.info(f"Archived {wav_file} -> {archive_name}")

                os.remove(wav_path)

                _enforce_audio_retention()

        except Exception as e:
            logger.error(f"Error in file watcher: {e}")

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
        logger.info("Preloading Whisper model...")
        await asyncio.to_thread(get_whisper_model)
        logger.info("Whisper model preloaded.")

        # Start voice-related tasks
        reconnect_task = asyncio.create_task(voice_reconnect_loop())
        diagnostic_task = asyncio.create_task(diagnostic_audio_report())
        cleanup_task = asyncio.create_task(cleanup_inactive_users())

        try:
            # Start the file watcher and the Discord client concurrently
            watcher_task = asyncio.create_task(watch_incoming_directory())
            await discord_client.start(DISCORD_BOT_TOKEN, log_handler=None)
            watcher_task.cancel()
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            reconnect_task.cancel()
            diagnostic_task.cancel()
            cleanup_task.cancel()

    asyncio.run(run_server())
