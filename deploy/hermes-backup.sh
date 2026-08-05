#!/usr/bin/env bash
# Бэкап БД Hermes: pg_dump в /opt/hermes/backups/hermes_YYYY-MM-DD.sql.gz.
# Хранит последние 14 бэкапов, старые удаляет. Запускается таймером hermes-backup.timer.
set -euo pipefail

ENV_FILE=/opt/hermes/.env
BACKUP_DIR=/opt/hermes/backups
KEEP=14

# DATABASE_URL берём из .env (строка DATABASE_URL=..., снимаем возможные кавычки).
DATABASE_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//')
if [ -z "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL не найден в $ENV_FILE" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP=$(date -u +%Y-%m-%d)
OUT="$BACKUP_DIR/hermes_${STAMP}.sql.gz"

pg_dump "$DATABASE_URL" | gzip > "$OUT"
echo "Бэкап готов: $OUT ($(du -h "$OUT" | cut -f1))"

# Чистим старые, оставляя последние KEEP по времени.
ls -1t "$BACKUP_DIR"/hermes_*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "Хранится бэкапов: $(ls -1 "$BACKUP_DIR"/hermes_*.sql.gz 2>/dev/null | wc -l) (лимит $KEEP)"
