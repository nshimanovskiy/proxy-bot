#!/usr/bin/env bash
# Обновление бота до последней версии из GitHub.
# Запускается вручную (./deploy/update.sh) или кнопкой Actions → «Деплой бота».
#   1) бэкап базы;
#   2) git fetch + reset на origin/<ветка> (.env, data/ и backups/ не затрагиваются);
#   3) пересборка и перезапуск контейнера, проверка запуска.
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }
envval() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }

[ -f .env ] || die "Нет .env — сначала выполните: sudo ./deploy/setup.sh"

if [ "${1:-}" != "--no-pull" ]; then
  if docker compose ps --status running -q proxy-bot 2>/dev/null | grep -q .; then
    say "Бэкап базы"
    ./deploy/backup.sh || warn "бэкап не удался — продолжаю"
  fi

  if [ -d .git ]; then
    BRANCH="$(envval DEPLOY_BRANCH)"
    BRANCH="${BRANCH:-main}"
    say "Забираю ${BRANCH} из GitHub"
    OLD="$(git rev-parse --short HEAD 2>/dev/null || echo '-')"
    git fetch --quiet origin "$BRANCH"
    git reset --hard --quiet "origin/${BRANCH}"
    NEW="$(git rev-parse --short HEAD)"
    if [ "$OLD" = "$NEW" ]; then
      echo "Код не изменился (${NEW})"
    else
      echo "${OLD} → ${NEW}"
      git log --oneline --no-decorate "${OLD}..${NEW}" 2>/dev/null | head -20 || true
    fi
    chmod +x deploy/*.sh
    # Дальше — уже новая версия этого скрипта
    exec ./deploy/update.sh --no-pull
  else
    warn "Папка не подключена к GitHub (нет .git). Подключите: sudo ./deploy/connect-github.sh ВЛАДЕЛЕЦ/РЕПОЗИТОРИЙ"
  fi
fi

say "Сборка и перезапуск"
mkdir -p data
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true

say "Проверка"
./deploy/check.sh || die "Бот не запустился после обновления"
