#!/usr/bin/env bash
# Optional: install Grimoire as a systemd service (auto-start on boot, auto-restart on crash,
# logs in journald). Use this instead of running `python3 grimoire.py 8730 &` by hand —
# a bare background process dies silently with its shell session and nothing revives it.
#
# Usage:
#   ./deploy/install-systemd.sh                          # defaults: repo dir, port 8730, current user
#   ./deploy/install-systemd.sh --dir /srv/grimoire --port 9000 --user agent
#
# Requires: systemd, python3, sudo. Idempotent: re-running updates the unit in place.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=8730
USER_NAME="$(id -un)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)   DIR="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    --user)  USER_NAME="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

PYTHON="$(readlink -f "$(command -v python3)")"
[[ -n "$PYTHON" ]] || { echo "python3 not found" >&2; exit 1; }
[[ -f "$DIR/grimoire.py" ]] || { echo "grimoire.py not found in $DIR (use --dir)" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "invalid port: $PORT" >&2; exit 1; }

UNIT_SRC="$DIR/deploy/grimoire.service.template"
UNIT_DST=/etc/systemd/system/grimoire.service

sed -e "s|{{USER}}|$USER_NAME|g" \
    -e "s|{{DIR}}|$DIR|g" \
    -e "s|{{PORT}}|$PORT|g" \
    -e "s|{{PYTHON}}|$PYTHON|g" \
    "$UNIT_SRC" | sudo tee "$UNIT_DST" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now grimoire
sleep 1
systemctl --no-pager --lines=3 status grimoire || true
echo
echo "Verify: curl -s http://127.0.0.1:$PORT/stats"
