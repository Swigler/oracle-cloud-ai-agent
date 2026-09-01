#!/usr/bin/env python3
"""Ops bot — fixed-verb control plane. No shell, no sudo."""

import os
import shutil
import subprocess
import logging
import time
import json
import urllib.request
import urllib.error
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["OPS_BOT_TOKEN"]
ALLOWED = {int(uid) for uid in os.environ.get("ALLOWED_USER_ID", "").split(",") if uid}
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")

OC_DIR = Path.home() / ".openclaude"
OC_CONFIG = Path.home() / ".openclaude.json"  # where OpenClaude reads MCP servers
OC_BOX_IP = os.environ.get("OC_BOX_IP", "158.178.145.191")
BROWSER_POD_NAME = "browser-pod"
TTS_FLAG = Path.home() / ".tts_off"


def gate(user_id: int) -> bool:
    if not ALLOWED or user_id in ALLOWED:
        return True
    return False


def run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()[:4000]
    except subprocess.TimeoutExpired:
        return "Command timed out."
    except Exception as e:
        return str(e)


# --- RunPod v1 API (same as agent_bot.py, proven to work) ---

def _runpod_v1(method, path, body=None):
    url = "https://rest.runpod.io/v1" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + RUNPOD_API_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        log.error("[runpod] v1 API error %d: %s", e.code, body_text)
        return {"error": body_text}


def _find_browser_pod():
    result = _runpod_v1("GET", "/pods")
    pods = result if isinstance(result, list) else result.get("items", result.get("pods", []))
    if isinstance(result, dict) and "error" in result:
        return None
    for pod in pods:
        if pod.get("name") == BROWSER_POD_NAME:
            return pod
    return None


def _create_browser_pod():
    body = {
        "name": BROWSER_POD_NAME,
        "imageName": "radu372/browser-pod:latest",
        "computeType": "CPU",
        "containerDiskInGb": 10,
        "ports": ["6080/http", "9222/http", "3000/http"],
        "env": {"VNC_PASSWORD": "browser123"},
    }
    return _runpod_v1("POST", "/pods", body)


def _kill_pod(pod_id):
    return _runpod_v1("DELETE", "/pods/" + pod_id)


def _wait_pod_running(timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        pod = _find_browser_pod()
        if pod and pod.get("desiredStatus") == "RUNNING":
            return pod
        time.sleep(5)
    return _find_browser_pod()


def _write_mcp_config(pod_id):
    """Add Playwright MCP to ~/.openclaude.json (where OpenClaude reads MCP servers)."""
    config = {}
    if OC_CONFIG.exists():
        config = json.loads(OC_CONFIG.read_text())

    mcp_url = f"https://{pod_id}-3000.proxy.runpod.net/mcp"
    config["mcpServers"] = {
        "playwright": {
            "type": "http",
            "url": mcp_url,
        }
    }
    OC_CONFIG.write_text(json.dumps(config, indent=2))
    log.info("[browse] Wrote MCP config to .openclaude.json: %s", mcp_url)


def _remove_mcp_config():
    """Remove Playwright MCP from ~/.openclaude.json."""
    if not OC_CONFIG.exists():
        return
    config = json.loads(OC_CONFIG.read_text())
    config.pop("mcpServers", None)
    OC_CONFIG.write_text(json.dumps(config, indent=2))
    log.info("[browse] Removed MCP config from .openclaude.json")


# --- OC box check ---

def check_oc_box() -> str:
    result = run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
         "-i", str(Path.home() / ".ssh" / "id_ed25519"),
         f"ubuntu@{OC_BOX_IP}", "uptime -p"],
        timeout=10,
    )
    if "up" in result.lower():
        return f"up ({result.strip()})"
    return f"unreachable ({result.strip()[:80]})"


def check_runpod_pods() -> str:
    if not RUNPOD_API_KEY:
        return "no API key configured"
    try:
        result = _runpod_v1("GET", "/pods")
        pods = result if isinstance(result, list) else result.get("items", result.get("pods", []))
        if isinstance(result, dict) and "error" in result:
            return f"API error: {result['error'][:100]}"
        if not pods:
            return "none active"
        lines = []
        for pod in pods:
            name = pod.get("name", "unnamed")
            pid = pod.get("id", "?")
            status = pod.get("desiredStatus", "?")
            cost = pod.get("costPerHr", "?")
            lines.append(f"  {name} ({pid}) — {status}, ${cost}/hr")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


# --- Commands ---

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return

    await update.message.reply_text("Checking all systems...")

    lines = []

    # Herald box
    lines.append("=== Herald Box ===")
    agent = run(["systemctl", "--user", "is-active", "herald-agent"])
    ops = run(["systemctl", "--user", "is-active", "herald-ops"])
    lines.append(f"Agent bot: {agent}")
    lines.append(f"Ops bot: {ops}")
    mem = run(["free", "-h"])
    lines.append(f"\n{mem}")
    uptime_str = run(["uptime", "-p"])
    lines.append(uptime_str)

    # OC box
    lines.append(f"\n=== OC Box ({OC_BOX_IP}) ===")
    oc_status = check_oc_box()
    lines.append(f"Status: {oc_status}")

    # RunPod
    lines.append("\n=== RunPod Pods ===")
    pods = check_runpod_pods()
    lines.append(pods)

    # Browser MCP config
    if OC_CONFIG.exists():
        settings = json.loads(OC_CONFIG.read_text())
        mcp = settings.get("mcpServers", {}).get("playwright", {})
        if mcp:
            lines.append(f"\n=== Browser MCP ===")
            lines.append(f"URL: {mcp.get('url', '?')}")

    await update.message.reply_text("\n".join(lines))


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    await update.message.reply_text("Restarting herald-agent...")
    result = run(["systemctl", "--user", "restart", "herald-agent"], timeout=30)
    status = run(["systemctl", "--user", "is-active", "herald-agent"])
    await update.message.reply_text(f"Result: {result or 'OK'}\nAgent is now: {status}")


async def cmd_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Spin up browser pod, configure MCP, restart agent."""
    if not gate(update.effective_user.id):
        return
    if not RUNPOD_API_KEY:
        await update.message.reply_text("RUNPOD_API_KEY not configured.")
        return

    # Check if pod already exists
    pod = _find_browser_pod()
    if pod and pod.get("desiredStatus") == "RUNNING":
        pod_id = pod["id"]
        novnc = f"https://{pod_id}-6080.proxy.runpod.net"
        _write_mcp_config(pod_id)
        run(["systemctl", "--user", "restart", "herald-agent"], timeout=30)
        status = run(["systemctl", "--user", "is-active", "herald-agent"])
        await update.message.reply_text(
            f"Browser pod already running!\n"
            f"noVNC: {novnc}\n"
            f"VNC pw: browser123\n"
            f"MCP configured, agent restarted ({status}).\n"
            f"Agent bot now has browser tools."
        )
        return

    msg = await update.message.reply_text("Creating browser pod...")

    # Create pod
    result = _create_browser_pod()
    if "error" in result:
        await msg.edit_text(f"Failed to create pod: {result['error'][:200]}")
        return

    pod_id = result.get("id", "")
    cost = result.get("costPerHr", "?")
    await msg.edit_text(f"Pod created ({pod_id}), ${cost}/hr. Waiting for it to start...")

    # Wait for RUNNING
    pod = _wait_pod_running(timeout=120)
    if not pod or pod.get("desiredStatus") != "RUNNING":
        await msg.edit_text("Pod failed to start within 2 minutes.")
        return

    pod_id = pod["id"]
    novnc = f"https://{pod_id}-6080.proxy.runpod.net"

    # Write MCP config and restart agent
    _write_mcp_config(pod_id)
    await msg.edit_text(f"Pod running! Configuring MCP and restarting agent...")
    run(["systemctl", "--user", "restart", "herald-agent"], timeout=30)
    status = run(["systemctl", "--user", "is-active", "herald-agent"])

    await update.message.reply_text(
        f"Browser ready!\n\n"
        f"noVNC: {novnc}\n"
        f"VNC pw: browser123\n"
        f"Cost: ${cost}/hr\n\n"
        f"Agent restarted ({status}) with browser tools.\n"
        f"Talk to the agent bot — it can browse now.\n\n"
        f"Use /stopbrowser when done (saves money)."
    )


async def cmd_stopbrowser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kill browser pod. Do NOT restart agent — keep conversation alive."""
    if not gate(update.effective_user.id):
        return

    pod = _find_browser_pod()
    if not pod:
        await update.message.reply_text("No browser pod found.")
        return

    pod_id = pod.get("id", "?")
    result = _kill_pod(pod_id)
    if isinstance(result, dict) and "error" in result:
        await update.message.reply_text(f"Failed: {result['error'][:200]}")
        return

    # Remove MCP config but do NOT restart agent
    _remove_mcp_config()
    await update.message.reply_text(
        f"Browser pod {pod_id} terminated.\n"
        f"MCP config removed.\n"
        f"Agent NOT restarted — you can keep talking about what was browsed."
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Clear OpenClaude sessions and restart the agent bot."""
    if not gate(update.effective_user.id):
        return

    cleared = []

    sessions_dir = OC_DIR / "sessions"
    if sessions_dir.exists():
        count = len(list(sessions_dir.iterdir()))
        for f in sessions_dir.iterdir():
            f.unlink()
        cleared.append(f"sessions: {count} files deleted")

    projects_dir = OC_DIR / "projects"
    if projects_dir.exists():
        count = 0
        for proj in projects_dir.iterdir():
            if proj.is_dir():
                for f in proj.iterdir():
                    if f.suffix in (".jsonl", ".json") and f.name != "CLAUDE.md":
                        f.unlink()
                        count += 1
        cleared.append(f"project logs: {count} files deleted")

    snaps_dir = OC_DIR / "shell-snapshots"
    if snaps_dir.exists():
        count = len(list(snaps_dir.iterdir()))
        for f in snaps_dir.iterdir():
            f.unlink()
        cleared.append(f"shell-snapshots: {count} files deleted")

    env_dir = OC_DIR / "session-env"
    if env_dir.exists():
        count = 0
        for item in env_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                count += 1
            else:
                item.unlink()
                count += 1
        cleared.append(f"session-env: {count} entries deleted")

    summary = "\n".join(cleared) if cleared else "Nothing to clear."

    await update.message.reply_text(f"Session reset:\n{summary}\n\nRestarting herald-agent...")
    run(["systemctl", "--user", "restart", "herald-agent"], timeout=30)
    status = run(["systemctl", "--user", "is-active", "herald-agent"])
    await update.message.reply_text(f"Agent is now: {status}")


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    n = 30
    if ctx.args:
        try:
            n = int(ctx.args[0])
        except ValueError:
            pass
    result = run(["journalctl", "--user", "-u", "herald-agent", "-n", str(n), "--no-pager"], timeout=15)
    await update.message.reply_text(f"<pre>{result[:4000]}</pre>", parse_mode="HTML")


async def cmd_speak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """TTS smoke test."""
    if not gate(update.effective_user.id):
        return
    text = " ".join(ctx.args) if ctx.args else "Hello, this is a test."
    try:
        import tempfile
        wav_path = tempfile.mktemp(suffix=".wav")
        ogg_path = wav_path.replace(".wav", ".ogg")
        venv = Path.home() / "rig" / ".venv" / "bin" / "python"
        subprocess.run([
            str(venv), "-c",
            f'from piper import PiperVoice; import wave; '
            f'v = PiperVoice.load(str(__import__("pathlib").Path.home() / "models/piper/en_GB-cori-medium.onnx")); '
            f'wf = wave.open("{wav_path}", "wb"); v.synthesize("{text}", wf); wf.close()',
        ], check=True, timeout=30, capture_output=True)
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "64k", ogg_path],
            capture_output=True, check=True,
        )
        await update.message.reply_voice(voice=open(ogg_path, "rb"))
        os.unlink(wav_path)
        os.unlink(ogg_path)
    except Exception as e:
        await update.message.reply_text(f"TTS failed: {e}")


async def cmd_tts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Toggle TTS on/off for the agent bot via shared file flag."""
    if not gate(update.effective_user.id):
        return
    if TTS_FLAG.exists():
        TTS_FLAG.unlink()
        await update.message.reply_text("TTS: ON — agent bot will send voice replies.")
    else:
        TTS_FLAG.touch()
        await update.message.reply_text("TTS: OFF — agent bot will send text only.")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not gate(update.effective_user.id):
        return
    tts_state = "OFF" if TTS_FLAG.exists() else "ON"
    await update.message.reply_text(
        f"Herald Ops. Commands:\n"
        f"/status — both boxes + RunPod pods + MCP config\n"
        f"/browse — spin up browser pod + configure agent\n"
        f"/stopbrowser — kill browser pod (agent keeps running)\n"
        f"/tts — toggle voice replies on/off (currently {tts_state})\n"
        f"/restart — restart agent bot\n"
        f"/reset — clear OpenClaude sessions + restart agent\n"
        f"/logs [N] — last N journal lines\n"
        f"/speak <text> — TTS smoke test"
    )


def main():
    log.info(f"Starting ops bot, allowed users: {ALLOWED}")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("browse", cmd_browse))
    app.add_handler(CommandHandler("stopbrowser", cmd_stopbrowser))
    app.add_handler(CommandHandler("tts", cmd_tts))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("speak", cmd_speak))
    log.info("Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
