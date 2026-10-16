#!/usr/bin/env bash
# Вызывается по SSH из GitHub Actions («Деплой бота»).
# Ключ Actions (пользователь proxy-bot-deploy) привязан к этому скрипту через
# command="sudo -n …" в authorized_keys, а sudo разрешает только его.
set -euo pipefail
cd "$(dirname "$0")/.."
exec 9>/tmp/proxy-bot-deploy.lock
flock -n 9 || { echo "Деплой уже выполняется"; exit 1; }
echo "Proxy bot: деплой на $(hostname), $(date -Is)"
exec ./deploy/update.sh
