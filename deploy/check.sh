#!/usr/bin/env bash
# Проверка, что бот поднялся: контейнер работает и в журнале есть «Бот запущен»
# (эта строка пишется после успешного обращения к Telegram с токеном из .env).
#   ./deploy/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
C=proxy-bot

for i in $(seq 1 45); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null || echo missing)"
  STARTED="$(docker inspect -f '{{.State.StartedAt}}' "$C" 2>/dev/null || true)"
  if [ "$STATE" = running ] && [ -n "$STARTED" ] && \
     docker compose logs --no-log-prefix --since "${STARTED%.*}Z" "$C" 2>/dev/null | grep -q 'Бот запущен'; then
    echo "OK: бот работает (запущен ${STARTED%.*}Z)"
    docker compose ps
    exit 0
  fi
  case "$STATE" in exited|dead|missing) break ;; esac
  sleep 2
done

echo "Бот не запустился (состояние: ${STATE}). Последние строки журнала:" >&2
docker compose logs --tail 40 "$C" >&2 || true
exit 1
