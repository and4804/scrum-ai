#!/bin/bash
set -e

cd /home/ubuntu/scrum-ai

git pull origin main

# Reinstall Python deps if requirements changed
/home/ubuntu/scrum-ai/.venv/bin/pip install -q -r requirements.txt

# Reinstall Node deps if package.json changed
cd wa_sidecar && npm install --silent && cd ..

sudo systemctl restart scrum-ai-api scrum-ai-wa

echo "Deploy done: $(date)"
