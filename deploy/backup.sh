#!/usr/bin/env bash
# Резервная копия базы бота (балансы, заказы) без остановки. Хранит последние 14 копий.
#   ./deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP="${KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
chmod 700 backups

if [ ! -f data/shop.db ]; then
  echo "Базы ещё нет (data/shop.db) — бэкап не нужен"
  exit 0
fi

# Онлайн-бэкап средствами SQLite внутри контейнера (корректен при включённом WAL)
docker compose exec -T proxy-bot python -c "
import sqlite3
src = sqlite3.connect('/app/data/shop.db'); dst = sqlite3.connect('/app/data/backup-${STAMP}.db')
src.backup(dst); dst.close(); src.close()"
mv "data/backup-${STAMP}.db" "backups/proxy-bot-${STAMP}.db"
gzip -9 "backups/proxy-bot-${STAMP}.db"
chmod 600 "backups/proxy-bot-${STAMP}.db.gz"

ls -1t backups/proxy-bot-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(date -Is) бэкап: backups/proxy-bot-${STAMP}.db.gz"
