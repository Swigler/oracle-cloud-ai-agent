#!/bin/bash
set -euo pipefail

# Herald Rig — installer for Oracle free-tier AI voice assistant
# Runs on a fresh Ubuntu 24.04 VM.Standard.E2.1.Micro
# Usage: bash install.sh

BOLD='\033[1m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step() { echo -e "\n${BOLD}=== $* ===${NC}"; }

# ─── Phase 0: Preflight ─────────────────────────────────────────────
step "Preflight checks"

ARCH=$(uname -m)
[[ "$ARCH" == "x86_64" ]] || fail "This script requires x86_64. Got: $ARCH"
log "Architecture: $ARCH"

if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    [[ "$VERSION_ID" == "20.04" ]] && fail "Ubuntu 20.04 is EOL. Use 24.04."
    log "OS: $PRETTY_NAME"
else
    fail "Cannot detect OS"
fi

TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
[[ "$TOTAL_RAM_MB" -lt 800 ]] && fail "Need at least 800 MB RAM. Got: ${TOTAL_RAM_MB} MB"
log "RAM: ${TOTAL_RAM_MB} MB"

# ─── Phase 1: Swap ──────────────────────────────────────────────────
step "Swap (2 GB)"

if [[ -f /swapfile ]]; then
    SWAP_SIZE=$(stat -c%s /swapfile 2>/dev/null || echo 0)
    if [[ "$SWAP_SIZE" -ge 2147483648 ]]; then
        log "Swap already exists (2 GB+), skipping"
    else
        warn "Swap exists but is small — recreating"
        sudo swapoff /swapfile 2>/dev/null || true
        sudo rm -f /swapfile
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
        sudo swapon /swapfile
        log "Swap recreated at 2 GB"
    fi
else
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    log "Swap created (2 GB)"
fi

grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
log "Swap in fstab"

# ─── Phase 2: OS trim ───────────────────────────────────────────────
step "OS cleanup"

for pkg in snapd fwupd; do
    if dpkg -l "$pkg" &>/dev/null; then
        sudo apt purge -y "$pkg" >/dev/null 2>&1
        log "Removed $pkg"
    else
        log "$pkg not installed, skipping"
    fi
done
sudo apt autoremove -y >/dev/null 2>&1

# Cap journal
sudo journalctl --vacuum-size=50M >/dev/null 2>&1
log "Journal capped at 50 MB"

# ─── Phase 3: Harden SSH ────────────────────────────────────────────
step "SSH hardening"

SSHD_CONF="/etc/ssh/sshd_config"
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONF"
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONF"
grep -q "^PasswordAuthentication no" "$SSHD_CONF" || echo "PasswordAuthentication no" | sudo tee -a "$SSHD_CONF" >/dev/null
grep -q "^PermitRootLogin no" "$SSHD_CONF" || echo "PermitRootLogin no" | sudo tee -a "$SSHD_CONF" >/dev/null

# Restart SSH (Ubuntu uses 'ssh', not 'sshd')
sudo systemctl restart ssh 2>/dev/null || sudo systemctl restart sshd 2>/dev/null || true
log "SSH: password auth off, root login off"

# Kill rpcbind
if systemctl is-active rpcbind.socket &>/dev/null; then
    sudo systemctl stop rpcbind rpcbind.socket
    sudo systemctl disable rpcbind rpcbind.socket
    sudo systemctl mask rpcbind rpcbind.socket
    log "rpcbind disabled"
else
    log "rpcbind already inactive"
fi

# ─── Phase 4: Base packages ─────────────────────────────────────────
step "Installing base packages"

sudo apt update -qq
sudo apt install -y -qq curl git ca-certificates ffmpeg python3-venv python3-pip build-essential >/dev/null 2>&1
log "Base packages installed"

# ─── Phase 5: Node 22 ───────────────────────────────────────────────
step "Installing Node.js 22"

if command -v node &>/dev/null && [[ "$(node -v | cut -d. -f1)" == "v22" ]]; then
    log "Node 22 already installed: $(node -v)"
else
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null 2>&1
    sudo apt install -y -qq nodejs >/dev/null 2>&1
    log "Node installed: $(node -v)"
fi

# ─── Phase 6: OpenClaude ────────────────────────────────────────────
step "Installing OpenClaude"

if command -v openclaude &>/dev/null; then
    log "OpenClaude already installed: $(openclaude --version 2>&1 | head -1)"
else
    sudo npm i -g @gitlawb/openclaude >/dev/null 2>&1
    log "OpenClaude installed: $(openclaude --version 2>&1 | head -1)"
fi

# ─── Phase 7: uv + Python venv ──────────────────────────────────────
step "Setting up uv + Python environment"

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
    log "uv installed"
else
    log "uv already installed: $(uv --version)"
fi

mkdir -p ~/rig
cd ~/rig

if [[ ! -d .venv ]]; then
    uv venv >/dev/null 2>&1
    log "venv created"
else
    log "venv already exists"
fi

uv pip install sherpa-onnx piper-tts python-telegram-bot >/dev/null 2>&1
log "Python packages installed"

# ─── Phase 8: Models ────────────────────────────────────────────────
step "Downloading models"

mkdir -p ~/models/piper

# English STT
STT_DIR=~/models/sherpa-onnx-streaming-zipformer-en-2023-06-26
if [[ -d "$STT_DIR" ]]; then
    log "English STT model already present"
else
    echo "  Downloading English STT model (~296 MB)..."
    curl -L -o /tmp/stt-en.tar.bz2 \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2"
    cd ~/models && tar xf /tmp/stt-en.tar.bz2 && rm /tmp/stt-en.tar.bz2
    log "English STT model downloaded"
fi

# English TTS
TTS_MODEL=~/models/piper/en_GB-cori-medium.onnx
if [[ -f "$TTS_MODEL" ]]; then
    log "English TTS voice already present"
else
    echo "  Downloading English TTS voice (~61 MB)..."
    curl -L -o "$TTS_MODEL" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/cori/medium/en_GB-cori-medium.onnx"
    curl -L -o "${TTS_MODEL}.json" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/cori/medium/en_GB-cori-medium.onnx.json"
    log "English TTS voice downloaded"
fi

# ─── Phase 9: Configuration ─────────────────────────────────────────
step "Configuration"

ENV_FILE="$HOME/.config/openclaude-rig/env"
mkdir -p "$(dirname "$ENV_FILE")"

if [[ -f "$ENV_FILE" ]]; then
    warn "Config already exists at $ENV_FILE"
    read -rp "  Overwrite? [y/N] " overwrite
    [[ "$overwrite" =~ ^[yY]$ ]] || { log "Keeping existing config"; SKIP_CONFIG=1; }
fi

if [[ "${SKIP_CONFIG:-}" != "1" ]]; then
    echo ""
    echo -e "${BOLD}Enter your credentials:${NC}"
    echo "  (Get a DeepSeek API key at https://platform.deepseek.com)"
    echo "  (Create two Telegram bots via @BotFather)"
    echo ""

    read -rp "  DeepSeek API key: " DS_KEY
    [[ -n "$DS_KEY" ]] || fail "API key is required"

    read -rp "  Agent bot token (@BotFather): " AGENT_TOKEN
    [[ -n "$AGENT_TOKEN" ]] || fail "Agent bot token is required"

    read -rp "  Ops bot token (@BotFather): " OPS_TOKEN
    [[ -n "$OPS_TOKEN" ]] || fail "Ops bot token is required"

    read -rp "  Your Telegram user ID: " TG_UID
    [[ -n "$TG_UID" ]] || fail "Telegram user ID is required"

    cat > "$ENV_FILE" << EOF
CLAUDE_CODE_USE_OPENAI=1
OPENAI_API_KEY=$DS_KEY
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-v4-pro
AGENT_BOT_TOKEN=$AGENT_TOKEN
OPS_BOT_TOKEN=$OPS_TOKEN
ALLOWED_USER_ID=$TG_UID
EOF
    chmod 600 "$ENV_FILE"
    log "Config written to $ENV_FILE (mode 600)"
fi

# ─── Phase 10: Deploy bot scripts ───────────────────────────────────
step "Deploying bot scripts"

# agent_bot.py is embedded below via heredoc
cat > ~/rig/agent_bot.py << 'AGENT_EOF'
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

AGENT_EOF

cat > ~/rig/ops_bot.py << 'OPS_EOF'
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

OPS_EOF

log "Bot scripts deployed"

# ─── Phase 11: Set Telegram bot commands ─────────────────────────────
step "Setting Telegram bot commands"

source "$ENV_FILE" 2>/dev/null || true

~/rig/.venv/bin/python << CMDEOF
import asyncio
from telegram import Bot

async def set_commands():
    ops = Bot("$OPS_BOT_TOKEN")
    await ops.set_my_commands([
        ("start", "Show available commands"),
        ("status", "Services, RAM, disk, uptime"),
        ("restart", "Restart agent bot"),
        ("logs", "Last N journal lines"),
        ("speak", "TTS smoke test"),
    ])
    agent = Bot("$AGENT_BOT_TOKEN")
    await agent.set_my_commands([
        ("start", "Start the bot"),
        ("help", "Show all commands"),
        ("voice", "Toggle voice replies on/off"),
        ("model", "Show current LLM model"),
        ("lang", "Show STT/TTS language"),
        ("clear", "Clear conversation context"),
        ("status", "Bot status and memory usage"),
    ])
    print("done")

asyncio.run(set_commands())
CMDEOF
log "Bot commands registered"

# ─── Phase 12: Systemd units ────────────────────────────────────────
step "Creating systemd services"

mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/herald-agent.service << EOF
[Unit]
Description=Herald Agent Bot
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/openclaude-rig/env
ExecStart=%h/rig/.venv/bin/python %h/rig/agent_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/herald-ops.service << EOF
[Unit]
Description=Herald Ops Bot
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/openclaude-rig/env
ExecStart=%h/rig/.venv/bin/python %h/rig/ops_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

sudo loginctl enable-linger "$(whoami)"
systemctl --user daemon-reload
systemctl --user enable herald-agent herald-ops
systemctl --user start herald-agent herald-ops
log "Services started and enabled (survive reboot)"

# ─── Phase 13: Smoke test ───────────────────────────────────────────
step "Smoke test"

sleep 3
AGENT_STATUS=$(systemctl --user is-active herald-agent)
OPS_STATUS=$(systemctl --user is-active herald-ops)

if [[ "$AGENT_STATUS" == "active" && "$OPS_STATUS" == "active" ]]; then
    log "Agent bot: $AGENT_STATUS"
    log "Ops bot: $OPS_STATUS"
else
    warn "Agent bot: $AGENT_STATUS"
    warn "Ops bot: $OPS_STATUS"
    warn "Check logs: journalctl --user -u herald-agent -n 20"
fi

# TTS → STT round-trip
echo "  Running TTS → STT round-trip..."
RESULT=$(~/rig/.venv/bin/python << 'TESTEOF'
import wave, os, subprocess
import numpy as np
from piper import PiperVoice
import sherpa_onnx

# TTS
v = PiperVoice.load(os.path.expanduser("~/models/piper/en_GB-cori-medium.onnx"))
with wave.open("/tmp/smoke.wav", "wb") as wf:
    v.synthesize_wav("The voice system is working correctly.", wf)

# Convert to 16kHz for STT
subprocess.run(["ffmpeg", "-y", "-i", "/tmp/smoke.wav", "-ar", "16000", "-ac", "1", "-f", "wav", "/tmp/smoke16.wav"],
    capture_output=True, check=True)

# STT
stt_dir = os.path.expanduser("~/models/sherpa-onnx-streaming-zipformer-en-2023-06-26")
recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=f"{stt_dir}/tokens.txt",
    encoder=f"{stt_dir}/encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    decoder=f"{stt_dir}/decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    joiner=f"{stt_dir}/joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    num_threads=1, provider="cpu",
)
with wave.open("/tmp/smoke16.wav") as f:
    sr = f.getframerate()
    samples = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

stream = recognizer.create_stream()
stream.accept_waveform(sr, samples)
stream.accept_waveform(sr, np.zeros(int(sr * 0.5), dtype=np.float32))
stream.input_finished()
while recognizer.is_ready(stream):
    recognizer.decode_stream(stream)

text = recognizer.get_result(stream).strip()
os.unlink("/tmp/smoke.wav")
os.unlink("/tmp/smoke16.wav")

if "voice" in text.lower() or "system" in text.lower() or "working" in text.lower():
    print("PASS: " + text)
else:
    print("WARN: " + text)
TESTEOF
)

if [[ "$RESULT" == PASS* ]]; then
    log "$RESULT"
else
    warn "$RESULT"
fi

# ─── Done ────────────────────────────────────────────────────────────
step "Installation complete"

echo ""
echo -e "${GREEN}Your Herald rig is live!${NC}"
echo ""
echo "  Agent bot: message it on Telegram to chat"
echo "  Ops bot:   /status, /restart, /logs, /speak"
echo ""
echo "  Config:    ~/.config/openclaude-rig/env"
echo "  Logs:      journalctl --user -u herald-agent -f"
echo "  Restart:   systemctl --user restart herald-agent"
echo ""
echo -e "${YELLOW}Remember: the only running cost is your DeepSeek API balance.${NC}"
echo -e "${YELLOW}Set a billing alert at https://platform.deepseek.com${NC}"
