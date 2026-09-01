# Oracle Cloud AI Agent

> Voice AI assistant + browser automation on Oracle Cloud. Two free micros, zero running cost.

A self-hosted AI assistant that runs on Oracle's always-free VMs. Talk to it via Telegram -- voice or text in, voice + text out. Spin up a cloud browser on demand for web automation. The client brings an Oracle account and an LLM API key; everything else runs locally and offline.

## Architecture

```
User (Telegram)
    │
    ├── Agent Bot (Herald box — voice + conversation)
    │   ├── STT:  sherpa-onnx streaming zipformer (int8, ~70 MB per language)
    │   ├── LLM:  OpenClaude → DeepSeek V4 Pro (or any OpenAI-compatible API)
    │   ├── TTS:  Piper VITS (MIT, offline, ~63 MB per voice)
    │   └── Memory: viral-git-agent-memory (git-native, markdown, auto-consolidation)
    │
    └── Ops Bot (Herald box — control plane)
        ├── /status      Both boxes + RunPod pods + MCP config
        ├── /browse      Spin up RunPod browser pod + configure agent MCP
        ├── /stopbrowser Kill browser pod (agent keeps conversation)
        ├── /tts         Toggle voice replies on/off
        ├── /restart     Restart agent bot
        ├── /reset       Clear OpenClaude sessions + restart
        ├── /logs [N]    Last N journal lines
        └── /speak       TTS smoke test

OpenClaude Box (browser automation)
    ├── Xvfb + x11vnc + noVNC (port 6080)
    ├── Playwright + Chromium (CDP on port 9222)
    └── start_vnc.sh
```

Both bots use outbound long-polling only -- **no inbound ports, no webhook, no TLS certificate to manage.**

### Two-box design

| Box | IP | Role | RAM used |
|---|---|---|---|
| Herald box | E2.1.Micro #1 | Agent bot + Ops bot + voice stack | ~828 MB / 954 MB |
| OpenClaude box | E2.1.Micro #2 | Browser automation + noVNC + memory server | ~115 MB / 956 MB |

## What You Need

| What | Cost | Purpose |
|---|---|---|
| Oracle Cloud account | $0 | Two VMs -- card for identity check, never billed |
| DeepSeek API key | ~$0.14/M tokens | The LLM brain |
| 2 Telegram bot tokens | $0 | Agent bot + Ops bot (via @BotFather) |
| RunPod API key (optional) | pay-per-use | On-demand cloud browser pods |

No GPU, no monthly hosting bill.

## Features

### Agent Bot
- **Voice conversations** -- send a voice note, get text + audio reply
- **Text conversations** -- send text, get a response via OpenClaude + DeepSeek
- **Session persistence** -- `--continue` flag resumes the most recent OpenClaude session
- **Memory integration** -- every 30 messages, conversation facts are extracted and saved to git-native markdown files via [viral-git-agent-memory](https://github.com/Swigler/viral-git-agent-memory)
- **Per-user TTS toggle** -- `/voice` to turn audio replies on/off
- **Browser tools** -- when ops bot spins up a browser pod, agent automatically gets Playwright MCP tools

### Ops Bot
- **System monitoring** -- `/status` checks both Oracle boxes, RunPod pods, and browser MCP config
- **Browser on demand** -- `/browse` creates a RunPod CPU pod with noVNC + Playwright, configures MCP, restarts agent with browser tools. `/stopbrowser` kills the pod without restarting the agent (preserves conversation context)
- **TTS control** -- `/tts` toggles voice replies via a shared file flag
- **Session management** -- `/reset` clears OpenClaude sessions/logs/snapshots and restarts
- **No shell access** -- fixed verb list only, no arbitrary execution

## Install

SSH into a fresh Ubuntu 22.04/24.04 `VM.Standard.E2.1.Micro` and run:

```bash
curl -O https://raw.githubusercontent.com/Swigler/oracle-cloud-ai-agent/main/install.sh
bash install.sh
```

The script will:
1. Create 2 GB swap (1 GB RAM can't run `apt` + `npm` without it)
2. Purge bloatware (`snapd`, `fwupd`) -- reclaims ~100 MB on a 1 GB box
3. Harden SSH (key-only, no root, kill `rpcbind`)
4. Install Node.js 22, OpenClaude, uv, Python dependencies
5. Download English STT + TTS models
6. Prompt for your API key, bot tokens, and Telegram user ID
7. Deploy both bots as systemd user services (survive reboot)
8. Run a TTS-to-STT smoke test

## Browser Automation

The ops bot can spin up a RunPod CPU pod with a full browser environment:

```
/browse    → Creates pod → Configures Playwright MCP → Restarts agent with browser tools
/stopbrowser → Kills pod, removes MCP config, agent keeps running
```

The agent gets Playwright tools via MCP and can browse the web, fill forms, post content. noVNC lets you watch what the browser is doing in real time.

## Memory System

The agent bot integrates with [viral-git-agent-memory](https://github.com/Swigler/viral-git-agent-memory):

- Every 30 messages, facts are extracted from the conversation
- Durable facts (name, job, preferences) are saved as markdown files
- Agent adaptations (tone, format preferences) are tracked separately
- Everything is committed to git -- `git log` shows the full memory timeline
- `/clear` triggers immediate consolidation before clearing context

## Project Structure

```
agent_bot.py     — Telegram agent: voice/text → STT → LLM → TTS → reply + memory
ops_bot.py       — Telegram ops: /status /browse /stopbrowser /tts /restart /reset /logs /speak
start_vnc.sh     — Launch Xvfb + x11vnc + noVNC on the OpenClaude box
install.sh       — Full installer (swap, OS trim, SSH hardening, models, services)
build.sh         — Embeds bot scripts into install_final.sh
install_final.sh — Ready-to-deploy installer (generated by build.sh)
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AGENT_BOT_TOKEN` | Yes | Telegram bot token for the agent |
| `OPS_BOT_TOKEN` | Yes | Telegram bot token for ops |
| `ALLOWED_USER_ID` | Yes | Comma-separated Telegram user IDs |
| `OPENAI_API_KEY` | Yes | DeepSeek (or compatible) API key |
| `OPENAI_BASE_URL` | Yes | API base URL (e.g. `https://api.deepseek.com/v1`) |
| `OPENAI_MODEL` | Yes | Model name (e.g. `deepseek-chat`) |
| `RUNPOD_API_KEY` | No | RunPod API key for browser pods |
| `OC_BOX_IP` | No | OpenClaude box IP (default: from env file) |

## Security

- **SSH:** key-only authentication, root login disabled
- **Telegram:** allowlist by user ID -- checked before any handler runs
- **Network:** no inbound ports open (bots use outbound long-polling only)
- **Secrets:** `~/.config/openclaude-rig/env`, mode 600, never committed
- **Ops bot:** fixed verb list only -- no shell, no sudo, no arbitrary execution
- **Browser pods:** VNC password set per-pod, terminated when not in use
- **Firewall:** Oracle's default + `rpcbind` disabled and masked

## Language Support

English works out of the box. The architecture supports per-client language selection:

**STT (sherpa-onnx zoo):** Streaming models for `de`, `es`, `fr`, `it`, `nl`, `pt`, `ru` (~68 MB each). English + one language per client.

**TTS (Piper):** 37 languages, 143 voices, all MIT, all offline. Full European coverage.

## Built With

- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) -- CPU-first ASR runtime, no PyTorch
- [Piper](https://github.com/rhasspy/piper) -- fast local neural TTS, MIT licensed
- [OpenClaude](https://github.com/Gitlawb/openclaude) -- open-source coding agent (OpenAI-compatible)
- [viral-git-agent-memory](https://github.com/Swigler/viral-git-agent-memory) -- git-native AI memory system
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) -- Telegram Bot API wrapper
- [uv](https://github.com/astral-sh/uv) -- fast Python package manager

## License

MIT
