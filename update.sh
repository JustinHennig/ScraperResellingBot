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

# If any step fails, go back to the old commit: otherwise HEAD would already match
# origin/main, the next run would see nothing to do, and the new code would never start
rollback() {
	echo "$1 — staying on ${LOCAL:0:7}, retrying next run" >&2
	git reset --quiet --hard "$LOCAL"
	exit 1
}

# Don't restart into code that doesn't even compile
venv/bin/python -m compileall -q bot server.py >/dev/null || rollback "New code doesn't compile"
venv/bin/pip install --quiet -r requirements.txt || rollback "Installing requirements failed"
sudo -n systemctl restart resellingbot.service || rollback "Restarting the bot failed"
echo "Bot restarted on $(git rev-parse --short HEAD)"
