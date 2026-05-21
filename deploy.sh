#!/bin/bash
# One-shot deploy for the Flask creatorgrowth app:
#   scp the files that actually matter → ssh ./rebuild.sh
# Usage: ./deploy.sh [optional commit message]
set -e

cd "$(dirname "$0")"

MSG="${1:-quick: $(date +%H:%M)}"

scp app.py root@148.230.108.170:/opt/idea_dashboard/app.py
# templates/static are bind-mounted on the VPS image; scp them only if changed
if ! git diff --quiet templates/ 2>/dev/null || [ -n "$(git status --porcelain templates/ 2>/dev/null)" ]; then
  scp -r templates root@148.230.108.170:/opt/idea_dashboard/
fi
if ! git diff --quiet static/ 2>/dev/null || [ -n "$(git status --porcelain static/ 2>/dev/null)" ]; then
  scp -r static root@148.230.108.170:/opt/idea_dashboard/
fi

ssh root@148.230.108.170 "cd /opt/idea_dashboard && ./rebuild.sh \"$MSG\""

echo "deployed @ $(date +%H:%M:%S)"
