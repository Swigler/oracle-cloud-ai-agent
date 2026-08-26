#!/usr/bin/env python3
"""Ops bot — fixed-verb control plane. No shell, no sudo."""

import os
import subprocess
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ['OPS_BOT_TOKEN']
ALLOWED = {int(uid) for uid in os.environ.get('ALLOWED_USER_ID', '').split(',') if uid}


def gate(user_id: int) -> bool:
    if not ALLOWED or user_id in ALLOWED:
        return True
    return False


def run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()[:4000]
    except subprocess.TimeoutExpired:
        return 'Command timed out.'
    except Exception as e:
        return str(e)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return

    lines = []
    # Agent service
    agent = run(['systemctl', '--user', 'is-active', 'herald-agent'])
    lines.append(f'Agent: {agent}')

    # Memory
    mem = run(['free', '-h'])
    lines.append(f'\n{mem}')

    # Disk
    disk = run(['df', '-h', '/'])
    lines.append(f'\n{disk}')

    # Uptime
    uptime = run(['uptime', '-p'])
    lines.append(f'\n{uptime}')

    await update.message.reply_text('\n'.join(lines))


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    result = run(['systemctl', '--user', 'restart', 'herald-agent'])
    await update.message.reply_text(f'Restart issued.\n{result or ok}')


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    n = 30
    if ctx.args:
        try:
            n = int(ctx.args[0])
        except ValueError:
            pass
    result = run(['journalctl', '--user', '-u', 'herald-agent', '-n', str(n), '--no-pager'], timeout=15)
    await update.message.reply_text(f'<pre>{result[:4000]}</pre>', parse_mode='HTML')


async def cmd_speak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """TTS smoke test."""
    if not gate(update.effective_user.id):
        return
    text = ' '.join(ctx.args) if ctx.args else 'Hello, this is a test.'
    try:
        import tempfile
        from pathlib import Path
        wav_path = tempfile.mktemp(suffix='.wav')
        ogg_path = wav_path.replace('.wav', '.ogg')
        venv = Path.home() / 'rig' / '.venv' / 'bin' / 'python'
        subprocess.run([
            str(venv), '-c',
            f'from piper import PiperVoice; import wave; '
            f'v = PiperVoice.load(str(Path.home() / "models/piper/en_GB-cori-medium.onnx")); '
            f'wf = wave.open("{wav_path}", "wb"); v.synthesize("{text}", wf); wf.close()',
        ], check=True, timeout=30, capture_output=True)
        subprocess.run(
            ['ffmpeg', '-y', '-i', wav_path, '-c:a', 'libopus', '-b:a', '64k', ogg_path],
            capture_output=True, check=True,
        )
        await update.message.reply_voice(voice=open(ogg_path, 'rb'))
        os.unlink(wav_path)
        os.unlink(ogg_path)
    except Exception as e:
        await update.message.reply_text(f'TTS failed: {e}')


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    await update.message.reply_text(
        'Herald Ops. Commands:\n'
        '/status — services, RAM, disk, uptime\n'
        '/restart — restart agent bot\n'
        '/logs [N] — last N journal lines\n'
        '/speak <text> — TTS smoke test'
    )


def main():
    log.info(f'Starting ops bot, allowed users: {ALLOWED}')
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(CommandHandler('restart', cmd_restart))
    app.add_handler(CommandHandler('logs', cmd_logs))
    app.add_handler(CommandHandler('speak', cmd_speak))
    log.info('Polling...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
