#!/bin/bash
# Safe rebuild: commits current state before rebuilding the container.
# Usage: ./rebuild.sh [optional commit message]

set -e
cd /opt/idea_dashboard

MSG="${1:-auto-save before rebuild}"

# Commit any changes
if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "$MSG"
    echo "Committed: $MSG"
else
    echo "No changes to commit"
fi

echo "Current version: $(git describe --tags --always)"
echo "Rebuilding container..."
cd /root && docker compose up -d --build idea-dashboard
echo "Done. Container rebuilt."
