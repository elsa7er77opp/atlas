#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/project-atlas"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/launcher.log" 2>&1
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed or not in PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is not available." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing $PROJECT_DIR/.env. Create it from .env.example before starting Atlas." >&2
  exit 1
fi

printf 'Starting Project Atlas services...\n'
docker compose up -d

printf 'Waiting for API health'
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    printf '\nAtlas is ready at http://127.0.0.1:5173\n'
    break
  fi
  printf '.'
  sleep 2
done

if ! curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
  printf '\nAPI did not become healthy. Recent status:\n' >&2
  docker compose ps >&2
  exit 1
fi

URL='http://127.0.0.1:5173'
if command -v chromium >/dev/null 2>&1; then
  nohup chromium --app="$URL" >/tmp/atlas-browser.log 2>&1 &
elif command -v chromium-browser >/dev/null 2>&1; then
  nohup chromium-browser --app="$URL" >/tmp/atlas-browser.log 2>&1 &
elif command -v google-chrome >/dev/null 2>&1; then
  nohup google-chrome --app="$URL" >/tmp/atlas-browser.log 2>&1 &
elif command -v firefox >/dev/null 2>&1; then
  nohup firefox --new-window "$URL" >/tmp/atlas-browser.log 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  nohup xdg-open "$URL" >/tmp/atlas-browser.log 2>&1 &
else
  printf 'Open %s in a browser.\n' "$URL"
fi
