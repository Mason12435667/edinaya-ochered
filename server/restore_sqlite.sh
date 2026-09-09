#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запусти через sudo." >&2
    exit 1
fi

if [[ $# -ne 1 ]]; then
    echo "Использование: $0 /path/to/tickets-YYYYMMDD-HHMMSS.db.gz" >&2
    exit 1
fi

BACKUP="$1"
DB="/var/lib/edinaya-ochered/tickets.db"
TMP="$(mktemp /tmp/edinaya-restore.XXXXXX.db)"
trap 'rm -f "$TMP"' EXIT

if [[ ! -f "$BACKUP" ]]; then
    echo "Файл не найден: $BACKUP" >&2
    exit 1
fi

gzip -dc "$BACKUP" > "$TMP"

CHECK="$(sqlite3 "$TMP" 'PRAGMA integrity_check;')"
if [[ "$CHECK" != "ok" ]]; then
    echo "Проверка SQLite не пройдена: $CHECK" >&2
    exit 1
fi

systemctl stop edinaya-ochered-whatsapp.service 2>/dev/null || true
systemctl stop edinaya-ochered.service

if [[ -f "$DB" ]]; then
    cp -a "$DB" "$DB.before-restore-$(date '+%Y%m%d-%H%M%S')"
fi

install -o queueapp -g queueapp -m 0640 "$TMP" "$DB"

systemctl start edinaya-ochered.service
systemctl start edinaya-ochered-whatsapp.service 2>/dev/null || true

echo "База восстановлена: $DB"
