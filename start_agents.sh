#!/bin/bash
# Charge le .env et démarre main.py pour launchd
set -a
source /Users/welldone/Desktop/VM/@ClaudeCode/welldone-agents/.env
set +a
cd /Users/welldone/Desktop/VM/@ClaudeCode/welldone-agents
exec python3 main.py
