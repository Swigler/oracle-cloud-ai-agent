#!/bin/bash
# Build the final install.sh with embedded bot scripts
set -euo pipefail

cd "$(dirname "$0")"

cp install.sh install_final.sh

# Embed agent_bot.py
AGENT_CONTENT=$(cat agent_bot.py)
python3 -c "
import sys
with open('install_final.sh') as f:
    content = f.read()
with open('agent_bot.py') as f:
    agent = f.read()
with open('ops_bot.py') as f:
    ops = f.read()
content = content.replace('@@AGENT_BOT@@', agent)
content = content.replace('@@OPS_BOT@@', ops)
with open('install_final.sh', 'w') as f:
    f.write(content)
print('Built install_final.sh')
"

chmod +x install_final.sh
echo "Done. Upload install_final.sh to the target box."
