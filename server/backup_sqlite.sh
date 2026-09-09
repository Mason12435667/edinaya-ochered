#!/usr/bin/env bash
set -Eeuo pipefail

DB="${QUEUE_DB_PATH:-/var/lib/edinaya-ochered/tickets.db}"
BACKUP_DIR="${QUEUE_BACKUP_DIR:-/var/backups/edinaya-ochered}"
KEEP_DAYS="${QUEUE_BACKUP_KEEP_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

if [[ ! -f "$DB" ]]; then
    echo "База не найдена: $DB" >&2
    exit 1
fi

STAMP="$(date '+%Y%m%d-%H%M%S')"
TMP="$BACKUP_DIR/.tickets-$STAMP.db"
FINAL="$BACKUP_DIR/tickets-$STAMP.db.gz"

sqlite3 "$DB" ".timeout 10000" ".backup '$TMP'"
gzip -9 "$TMP"
mv "$TMP.gz" "$FINAL"

find "$BACKUP_DIR" -type f -name 'tickets-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "Готово: $FINAL"
