#!/usr/bin/env python3
"""Agent bot — voice + text conversation via OpenClaude + DeepSeek."""

import os
import subprocess
import tempfile
import wave
import logging
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
    """Convert audio file to text via sherpa-onnx."""
    # Convert to wav first
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
    """Convert text to speech via Piper, return path to wav file."""
    voice = get_voice()
    out_path = tempfile.mktemp(suffix='.wav')
    with wave.open(out_path, 'wb') as wf:
        voice.synthesize_wav(text, wf)
    # Convert to ogg for Telegram voice note
    ogg_path = out_path.replace('.wav', '.ogg')
    subprocess.run(
        ['ffmpeg', '-y', '-i', out_path, '-c:a', 'libopus', '-b:a', '64k', ogg_path],
        capture_output=True, check=True,
    )
    os.unlink(out_path)
    return ogg_path


def ask_openclaude(text: str) -> str:
    """Send text to OpenClaude and get response."""
    env = {**os.environ}
    # Load openclaude env vars
    env_file = Path.home() / '.config' / 'openclaude-rig' / 'env'
    for line in env_file.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()

    result = subprocess.run(
        ['openclaude', '--print', '-p', text],
        capture_output=True, text=True, timeout=120, env=env,
    )
    return result.stdout.strip() or result.stderr.strip() or 'No response.'


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return

    msg = await update.message.reply_text('🎧 Listening...')

    # Download voice note
    voice_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as f:
        await voice_file.download_to_drive(f.name)
        ogg_path = f.name

    # STT
    try:
        text = transcribe(ogg_path)
    finally:
        os.unlink(ogg_path)

    if not text:
        await msg.edit_text('Could not understand the audio.')
        return

    await msg.edit_text(f'🗣 \"{text}\"')
    thinking_msg = await update.message.reply_text('⏳ Thinking...')

    # LLM
    response = ask_openclaude(text)
    await thinking_msg.edit_text(response[:4096])

    # TTS
    try:
        if _voice_enabled.get(update.effective_user.id, True):
            audio_path = synthesize(response[:500])  # cap TTS length
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

    response = ask_openclaude(text)
    await msg.edit_text(response[:4096])

    # TTS
    try:
        audio_path = synthesize(response[:500])
        await update.message.reply_voice(voice=open(audio_path, 'rb'))
        os.unlink(audio_path)
    except Exception as e:
        log.warning(f'TTS failed: {e}')


# --- per-user state ---
_voice_enabled = {}  # user_id -> bool, default True


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
        '/status - Bot status and memory usage'
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
        '/status - Bot status and memory usage'
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
        f'STT: English (sherpa-onnx streaming zipformer)\n'
        f'TTS: en_GB-cori-medium (Piper)'
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    await update.message.reply_text('Context cleared. (OpenClaude runs stateless per message — each message is independent.)')


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    stt_state = 'loaded' if _recognizer else 'not loaded'
    tts_state = 'loaded' if _voice else 'not loaded'
    mem = subprocess.run(['free', '-h'], capture_output=True, text=True).stdout
    await update.message.reply_text(
        f'Agent bot RSS: {rss} MB\n'
        f'STT: {stt_state}\n'
        f'TTS: {tts_state}\n\n'
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
