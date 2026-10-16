#!/usr/bin/env bash
# Проверка, что бот поднялся: контейнер работает и в журнале есть «Бот запущен»
# (эта строка пишется после успешного обращения к Telegram с токеном из .env).
#   ./deploy/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
C=proxy-bot
WAIT="${CHECK_WAIT:-90}"

hint_telegram() {
  echo >&2
  echo "[!] Похоже, сервер не может достучаться до Telegram. Укажите прокси в .env:" >&2
  echo "    TELEGRAM_PROXY=socks5://login:pass@host:port" >&2
  echo "    и выполните: docker compose up -d && ./deploy/check.sh" >&2
}

printf 'Жду запуска бота (до %s с)' "$WAIT"
STATE=missing
for i in $(seq 1 $((WAIT / 2))); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null || echo missing)"
  STARTED="$(docker inspect -f '{{.State.StartedAt}}' "$C" 2>/dev/null || true)"
  if [ "$STATE" = running ] && [ -n "$STARTED" ]; then
    # Журнал читаем целиком в переменную: «… | grep -q» при pipefail даёт ложный отказ (SIGPIPE)
    LOGS="$(docker compose logs --no-log-prefix --since "${STARTED%.*}Z" "$C" 2>&1 || true)"
    if [[ "$LOGS" == *"Бот запущен"* ]]; then
      echo
      echo "OK: бот работает (запущен ${STARTED%.*}Z)"
      docker compose ps
      exit 0
    fi
  fi
  case "$STATE" in exited|dead|missing) break ;; esac
  printf '.'
  sleep 2
done
echo

echo "Бот не запустился (состояние: ${STATE}). Последние строки журнала:" >&2
LOGS="$(docker compose logs --tail 40 "$C" 2>&1 || true)"
echo "$LOGS" >&2
if [ "$STATE" = running ] || [ "$STATE" = restarting ] || \
   grep -qiE 'TelegramNetworkError|api\.telegram\.org|Cannot connect to host|timed? ?out' <<<"$LOGS"; then
  hint_telegram
fi
exit 1
