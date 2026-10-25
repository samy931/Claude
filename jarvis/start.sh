#!/bin/bash
# Startet Jarvis auf der Tailscale-Adresse dieses Macs.
# Der Mac bleibt dabei wach (caffeinate), solange Jarvis laeuft.
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${JARVIS_HOST:-}" ]; then
  TS=""
  for bin in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    if command -v "$bin" >/dev/null 2>&1 || [ -x "$bin" ]; then TS="$bin"; break; fi
  done
  if [ -z "$TS" ]; then
    echo "Tailscale nicht gefunden. Bitte installieren: https://tailscale.com/download/mac" >&2
    exit 1
  fi
  JARVIS_HOST="$("$TS" ip -4 2>/dev/null | head -n1)"
  if [ -z "$JARVIS_HOST" ]; then
    echo "Keine Tailscale-Adresse. Ist Tailscale verbunden?" >&2
    exit 1
  fi
fi
export JARVIS_HOST

if ! command -v "${JARVIS_CLAUDE_BIN:-claude}" >/dev/null 2>&1; then
  echo "Der Befehl 'claude' wurde nicht gefunden. Ist Claude Code installiert?" >&2
  exit 1
fi

TOKEN_FILE="${JARVIS_CONFIG_DIR:-$HOME/.jarvis}/token"
python3 -c 'import server; server.logging.basicConfig(level=20); server.load_token()'
echo
echo "URL fuer den Kurzbefehl:  http://$JARVIS_HOST:${JARVIS_PORT:-8765}/ask"
echo "Token (Header Authorization: Bearer ...):"
echo "  ${JARVIS_TOKEN:-$(cat "$TOKEN_FILE")}"
echo

if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -is python3 server.py
else
  exec python3 server.py
fi
