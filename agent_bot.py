#!/usr/bin/env python3
"""Agent bot — voice + text conversation via OpenClaude + DeepSeek."""

import os
import subprocess
import tempfile
import wave
import logging
import json
import threading
import numpy as np
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# --- config from env ---
BOT_TOKEN = os.environ['AGENT_BOT_TOKEN']
ALLOWED = {int(uid) for uid in os.environ.get('ALLOWED_USER_ID', '').split(',') if uid}
VENV_PYTHON = Path(__file__).parent / '.venv' / 'bin' / 'python'

MODEL_DIR = Path.home() / 'models'
STT_DIR = MODEL_DIR / 'sherpa-onnx-streaming-zipformer-en-2023-06-26'
TTS_MODEL = MODEL_DIR / 'piper' / 'en_GB-cori-medium.onnx'

# --- memory hook ---
import urllib.request
import urllib.error

MEMORY_API_URL = 'http://localhost:3100'
_msg_counts = {}  # user_id -> int
_transcripts = {}  # user_id -> list of {"role": ..., "content": ...}
CONSOLIDATE_EVERY = 30


def _memory_log(user_id, role, text):
    uid = str(user_id)
    if uid not in _transcripts:
        _transcripts[uid] = []
    _transcripts[uid].append({"role": role, "content": text[:500]})
    _msg_counts[uid] = _msg_counts.get(uid, 0) + 1


def _memory_should_consolidate(user_id):
    return _msg_counts.get(str(user_id), 0) % CONSOLIDATE_EVERY == 0


def _memory_consolidate(user_id):
    uid = str(user_id)
    transcript = _transcripts.get(uid, [])
    if not transcript:
        return
    payload = json.dumps({"userId": uid, "transcript": transcript}).encode()
    req = urllib.request.Request(
        f"{MEMORY_API_URL}/v1/consolidate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=120)
        log.info(f"[memory] consolidated {len(transcript)} messages for {uid}")
    except Exception as e:
        log.warning(f"[memory] consolidation failed: {e}")
    _transcripts[uid] = []


# --- lazy-loaded singletons ---
_recognizer = None
_voice = None


def get_recognizer():
    global _recognizer
    if _recognizer is None:
        import sherpa_onnx
        _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(STT_DIR / 'tokens.txt'),
            encoder=str(STT_DIR / 'encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx'),
            decoder=str(STT_DIR / 'decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx'),
            joiner=str(STT_DIR / 'joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx'),
            num_threads=1,
            provider='cpu',
        )
        log.info('STT loaded')
    return _recognizer


def get_voice():
    global _voice
    if _voice is None:
        from piper import PiperVoice
        _voice = PiperVoice.load(str(TTS_MODEL))
        log.info('TTS loaded')
    return _voice


def gate(user_id: int) -> bool:
    return not ALLOWED or user_id in ALLOWED


def transcribe(audio_path: str) -> str:
    wav_path = audio_path + '.wav'
    subprocess.run(
        ['ffmpeg', '-y', '-i', audio_path, '-ar', '16000', '-ac', '1', '-f', 'wav', wav_path],
        capture_output=True, check=True,
    )
    recognizer = get_recognizer()
    with wave.open(wav_path) as f:
        sample_rate = f.getframerate()
        samples = f.readframes(f.getnframes())
    samples = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    tail = np.zeros(int(sample_rate * 0.5), dtype=np.float32)
    stream.accept_waveform(sample_rate, tail)
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    os.unlink(wav_path)
    return recognizer.get_result(stream).strip()


def synthesize(text: str) -> str:
    voice = get_voice()
    out_path = tempfile.mktemp(suffix='.wav')
    with wave.open(out_path, 'wb') as wf:
        voice.synthesize_wav(text, wf)
    ogg_path = out_path.replace('.wav', '.ogg')
    subprocess.run(
        ['ffmpeg', '-y', '-i', out_path, '-c:a', 'libopus', '-b:a', '64k', ogg_path],
        capture_output=True, check=True,
    )
    os.unlink(out_path)
    return ogg_path


_oc_lock = threading.Lock()


def ask_openclaude(text: str, user_id: str = '974838875') -> str:
    """Run OpenClaude locally with DeepSeek, from the user memory dir.
    Browser MCP config comes from ~/.openclaude.json (managed by ops bot).
    """
    if not _oc_lock.acquire(timeout=5):
        return 'I am still working on the previous request. Please wait a moment.'
    try:
        memory_dir = Path.home() / 'memory' / user_id
        memory_dir.mkdir(parents=True, exist_ok=True)
        cmd = ['openclaude', '--print', '--continue', '--dangerously-skip-permissions', '-p', text]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=180,
            cwd=str(memory_dir),
        )
        return result.stdout.strip() or result.stderr.strip() or 'No response.'
    finally:
        _oc_lock.release()


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return

    msg = await update.message.reply_text('🎧 Listening...')

    voice_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
        await voice_file.download_to_drive(f.name)
        ogg_path = f.name

    try:
        text = transcribe(ogg_path)
    finally:
        os.unlink(ogg_path)

    if not text:
        await msg.edit_text('Could not understand the audio.')
        return

    await msg.edit_text(f'🗣 "{text}"')
    thinking_msg = await update.message.reply_text('⏳ Thinking...')

    response = ask_openclaude(text, str(update.effective_user.id))
    await thinking_msg.edit_text(response[:4096])

    uid = update.effective_user.id
    _memory_log(uid, "user", text)
    _memory_log(uid, "assistant", response)
    if _memory_should_consolidate(uid):
        threading.Thread(target=_memory_consolidate, args=(uid,), daemon=True).start()

    try:
        if _tts_active(uid):
            audio_path = synthesize(response[:500])
            await update.message.reply_voice(voice=open(audio_path, 'rb'))
            os.unlink(audio_path)
    except Exception as e:
        log.warning(f'TTS failed: {e}')


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return

    text = update.message.text
    if not text:
        return

    msg = await update.message.reply_text('⏳ Thinking...')

    response = ask_openclaude(text, str(update.effective_user.id))
    await msg.edit_text(response[:4096])

    uid = update.effective_user.id
    _memory_log(uid, "user", text)
    _memory_log(uid, "assistant", response)
    if _memory_should_consolidate(uid):
        threading.Thread(target=_memory_consolidate, args=(uid,), daemon=True).start()

    try:
        if _tts_active(uid):
            audio_path = synthesize(response[:500])
            await update.message.reply_voice(voice=open(audio_path, 'rb'))
            os.unlink(audio_path)
    except Exception as e:
        log.warning(f'TTS failed: {e}')


# --- per-user state ---
_voice_enabled = {}
TTS_FLAG = Path.home() / '.tts_off'


def _tts_active(uid):
    """Check if TTS is on. File flag overrides per-user dict."""
    if TTS_FLAG.exists():
        return False
    return _voice_enabled.get(uid, True)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    await update.message.reply_text(
        'Herald agent ready. Send me text or a voice note.\n\n'
        'Commands:\n'
        '/help - Show all commands\n'
        '/voice - Toggle voice replies on/off\n'
        '/model - Show current LLM model\n'
        '/lang - Show STT/TTS language\n'
        '/clear - Clear conversation context\n'
        '/status - Bot status and memory usage\n\n'
        'For browser: use /browse on the ops bot first.'
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    await update.message.reply_text(
        'Send me a text message or voice note and I will reply with text + audio.\n\n'
        'Commands:\n'
        '/voice - Toggle voice replies on/off\n'
        '/model - Show current LLM model\n'
        '/lang - Show STT/TTS language\n'
        '/clear - Clear conversation context\n'
        '/status - Bot status and memory usage\n\n'
        'Browser is managed by the ops bot (/browse, /stopbrowser).'
    )


async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    uid = update.effective_user.id
    current = _voice_enabled.get(uid, True)
    _voice_enabled[uid] = not current
    state = 'OFF' if current else 'ON'
    await update.message.reply_text(f'Voice replies: {state}')


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    env_file = Path.home() / '.config' / 'openclaude-rig' / 'env'
    model = 'unknown'
    for line in env_file.read_text().splitlines():
        if line.startswith('OPENAI_MODEL='):
            model = line.split('=', 1)[1]
    await update.message.reply_text(f'LLM: {model}')


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    await update.message.reply_text(
        'STT: English (sherpa-onnx streaming zipformer)\n'
        'TTS: en_GB-cori-medium (Piper)'
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    uid = update.effective_user.id
    if _transcripts.get(str(uid)):
        threading.Thread(target=_memory_consolidate, args=(uid,), daemon=True).start()
    await update.message.reply_text('Context cleared. Memories saved.')


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    stt_state = 'loaded' if _recognizer else 'not loaded'
    tts_state = 'loaded' if _voice else 'not loaded'
    mem = subprocess.run(['free', '-h'], capture_output=True, text=True).stdout

    # Check if browser MCP is configured
    oc_config = Path.home() / '.openclaude.json'
    browser_status = 'not configured'
    if oc_config.exists():
        try:
            config = json.loads(oc_config.read_text())
            mcp = config.get('mcpServers', {}).get('playwright', {})
            if mcp:
                browser_status = f"configured ({mcp.get('url', '?')})"
        except Exception:
            pass

    await update.message.reply_text(
        f'Agent bot RSS: {rss} MB\n'
        f'STT: {stt_state}\n'
        f'TTS: {tts_state}\n'
        f'Browser: {browser_status}\n\n'
        f'{mem}'
    )


def main():
    log.info(f'Starting agent bot, allowed users: {ALLOWED}')
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('voice', cmd_voice))
    app.add_handler(CommandHandler('model', cmd_model))
    app.add_handler(CommandHandler('lang', cmd_lang))
    app.add_handler(CommandHandler('clear', cmd_clear))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info('Polling...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
