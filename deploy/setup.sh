#!/usr/bin/env bash
# Первая установка бота на VPS с Docker.
#   sudo ./deploy/setup.sh
#
# Что делает (файрвол, nginx и другие сайты не трогает — боту не нужны входящие порты):
#   1) спрашивает токен бота, API-ключ Steel Proxy и ID админов, пишет .env;
#   2) собирает и запускает контейнер proxy-bot;
#   3) включает ежедневный бэкап базы в 03:40.
# Скрипт можно запускать повторно — уже заполненные значения он не спрашивает.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo $0"
[ -f docker-compose.yml ] || die "Запускайте из папки проекта (там, где docker-compose.yml)"
command -v docker >/dev/null && docker compose version >/dev/null 2>&1 || die "Нужны docker и docker compose"

# ---------- .env ----------
say "Настройки (.env)"
[ -f .env ] || cp .env.example .env
chmod 600 .env
envval() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
set_env() {
  local v="${2//|/\\|}"
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=${v}|" .env; else echo "$1=$2" >> .env; fi
}
ask() {  # ask ПЕРЕМЕННАЯ "Вопрос" ПРИЗНАК_ЗАГЛУШКИ
  local cur; cur="$(envval "$1")"
  if [ -z "$cur" ] || [[ "$cur" == *"$3"* ]]; then
    local ans=""
    while [ -z "$ans" ]; do read -rp "$2: " ans </dev/tty; done
    set_env "$1" "$ans"
  else
    echo "$1 уже задан"
  fi
}
ask BOT_TOKEN "Токен бота от @BotFather" "ABC..."
ask STEELPROXY_API_KEY "API-ключ Steel Proxy (Ф-бот → Профиль → API-ключ)" "XXXX"
ask ADMIN_IDS "Ваш Telegram ID (несколько — через запятую; узнать: @userinfobot)" "123456789"

TOKEN="$(envval BOT_TOKEN)"
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || warn "Токен бота выглядит странно — проверьте BOT_TOKEN в .env"

APIKEY="$(envval STEELPROXY_API_KEY)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -H "X-API-Key: ${APIKEY}" \
        https://api.steelproxy.com/v1/proxy-types || true)"
case "$CODE" in
  200) echo "OK: API Steel Proxy принимает ключ" ;;
  401) warn "API Steel Proxy: ключ неверный (401) — проверьте STEELPROXY_API_KEY" ;;
  402) warn "API Steel Proxy: нет активной подписки на франшизу (402)" ;;
  403) warn "API Steel Proxy: аккаунт заблокирован (403)" ;;
  *)   warn "API Steel Proxy ответил ${CODE:-ничего} — проверьте позже" ;;
esac

echo
echo "Наценку, реквизиты для оплаты, контакт поддержки и название магазина"
echo "можно поменять в ${DIR}/.env (MARKUP_PERCENT, PAYMENT_INFO, SUPPORT_CONTACT, SHOP_NAME)."

# ---------- Запуск ----------
say "Сборка и запуск"
mkdir -p data backups
chmod +x deploy/*.sh
docker compose up -d --build --remove-orphans
./deploy/check.sh || die "Бот не запустился — исправьте .env и запустите скрипт снова"

# ---------- Бэкап ----------
say "Ежедневный бэкап базы"
CRON=/etc/cron.d/proxy-bot-backup
echo "40 3 * * * root ${DIR}/deploy/backup.sh >> ${DIR}/backups/backup.log 2>&1" > "$CRON"
chmod 644 "$CRON"
echo "OK: каждый день в 03:40 → ${DIR}/backups/ (последние 14 копий)"

cat <<EOF

Готово. Откройте бота в Telegram и нажмите /start, админ-панель — /admin.

Обновление кнопкой из GitHub (один раз):
  sudo ./deploy/connect-github.sh nshimanovskiy/proxy-bot
EOF
