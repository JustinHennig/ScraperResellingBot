#!/bin/bash
# Auto-updater for the Raspberry Pi. Run every 5 minutes by resellingbot-update.timer
# (as user justinhennig): pulls new commits from origin/main, installs requirements
# into the bot's venv and restarts the bot. Prints nothing when already up to date.
set -euo pipefail
cd /home/justinhennig/ResellingBot

git fetch --quiet origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "New commits ${LOCAL:0:7} -> ${REMOTE:0:7}, updating..."
# Fast-forward only: refuses to touch local edits or a diverged history instead of overwriting them
git merge --ff-only --quiet origin/main

# Don't restart into code that doesn't even compile — go back and fail loudly instead
if ! venv/bin/python -m compileall -q bot server.py >/dev/null; then
	echo "New code doesn't compile — staying on ${LOCAL:0:7}" >&2
	git reset --quiet --hard "$LOCAL"
	exit 1
fi

venv/bin/pip install --quiet -r requirements.txt
sudo -n systemctl restart resellingbot.service
echo "Bot restarted on $(git rev-parse --short HEAD)"
