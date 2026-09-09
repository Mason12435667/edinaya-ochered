#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запусти обновление через sudo: sudo bash server/update_ubuntu.sh" >&2
    exit 1
fi

APP_USER="queueapp"
APP_GROUP="queueapp"
APP_DIR="/opt/edinaya-ochered"
DATA_DIR="/var/lib/edinaya-ochered"
BACKUP_DIR="/var/backups/edinaya-ochered"
ENV_FILE="/etc/edinaya-ochered/queue.env"
PASSWORD_FILE="/etc/nginx/.htpasswd-edinaya-ochered"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date '+%Y%m%d-%H%M%S')"
ROLLBACK_DIR="$BACKUP_DIR/release-before-$STAMP"
UPDATE_OK=0

required=(app.py ticketing.py whatsapp_qr_connector.js package.json static/app.js static/style.css)
for name in "${required[@]}"; do
    if [[ ! -f "$SOURCE_DIR/$name" ]]; then
        echo "В пакете обновления не найден $name" >&2
        exit 1
    fi
done

if [[ ! -d "$APP_DIR" || ! -f "$ENV_FILE" ]]; then
    echo "Рабочая установка не найдена. Для первой установки используй server/install_ubuntu.sh" >&2
    exit 1
fi

# Старые установки могли не иметь постоянного имени администратора.
# Берём первый существующий логин сайта и сохраняем его в конфигурации,
# которая находится вне папки кода и переживает все последующие обновления.
if ! grep -Eq '^QUEUE_ADMIN_USER=.+$' "$ENV_FILE"; then
    ADMIN_LOGIN="queueadmin"
    if [[ -s "$PASSWORD_FILE" ]]; then
        EXISTING_LOGIN="$(awk -F: 'NF {print $1; exit}' "$PASSWORD_FILE")"
        if [[ "$EXISTING_LOGIN" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
            ADMIN_LOGIN="$EXISTING_LOGIN"
        fi
    fi
    if grep -q '^QUEUE_ADMIN_USER=' "$ENV_FILE"; then
        sed -i "s/^QUEUE_ADMIN_USER=.*/QUEUE_ADMIN_USER=$ADMIN_LOGIN/" "$ENV_FILE"
    else
        printf '\nQUEUE_ADMIN_USER=%s\n' "$ADMIN_LOGIN" >> "$ENV_FILE"
    fi
    echo "Постоянный администратор сайта: $ADMIN_LOGIN"
fi

mkdir -p "$BACKUP_DIR" "$ROLLBACK_DIR"

echo "1/7 Проверяю новый код"
python3 -m py_compile "$SOURCE_DIR/app.py" "$SOURCE_DIR/ticketing.py" "$SOURCE_DIR/transcription.py"
node --check "$SOURCE_DIR/whatsapp_qr_connector.js"

echo "2/7 Сохраняю базу и предыдущую версию"
if [[ -f "$DATA_DIR/tickets.db" ]]; then
    QUEUE_DB_PATH="$DATA_DIR/tickets.db" \
        QUEUE_BACKUP_DIR="$BACKUP_DIR" \
        bash "$APP_DIR/server/backup_sqlite.sh"
fi
rsync -a \
    --exclude '.venv/' \
    --exclude 'node_modules/' \
    --exclude '.wwebjs_auth/' \
    --exclude '.wwebjs_cache/' \
    --exclude '.connector_token' \
    --exclude '__pycache__/' \
    "$APP_DIR/" "$ROLLBACK_DIR/"

rollback() {
    if [[ "$UPDATE_OK" -eq 1 ]]; then
        return
    fi
    echo
    echo "Обновление не завершилось. Возвращаю предыдущий код..." >&2
    rsync -a --delete \
        --exclude '.venv/' \
        --exclude 'node_modules/' \
        --exclude '.wwebjs_auth/' \
        --exclude '.wwebjs_cache/' \
        --exclude '.connector_token' \
        --exclude '__pycache__/' \
        "$ROLLBACK_DIR/" "$APP_DIR/" || true
    chown -R "$APP_USER:$APP_GROUP" "$APP_DIR" || true
    systemctl daemon-reload || true
    systemctl restart edinaya-ochered.service || true
    systemctl restart edinaya-ochered-whatsapp.service || true
}
trap rollback EXIT

echo "3/7 Останавливаю службы"
systemctl stop edinaya-ochered-whatsapp.service 2>/dev/null || true
systemctl stop edinaya-ochered.service

echo "4/7 Копирую обновление и удаляю старые файлы проекта"
rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'node_modules/' \
    --exclude '.wwebjs_auth/' \
    --exclude '.wwebjs_cache/' \
    --exclude '.connector_token' \
    --exclude '__pycache__/' \
    "$SOURCE_DIR/" "$APP_DIR/"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

echo "5/7 Обновляю зависимости"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-voice.txt"
cd "$APP_DIR"
if [[ -f package-lock.json ]]; then
    npm ci --omit=dev
else
    npm install --omit=dev
fi
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/node_modules"

install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$DATA_DIR/whatsapp-auth"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$DATA_DIR/whatsapp-cache"
ln -sfn "$DATA_DIR/whatsapp-auth" "$APP_DIR/.wwebjs_auth"
ln -sfn "$DATA_DIR/whatsapp-cache" "$APP_DIR/.wwebjs_cache"
chown -h "$APP_USER:$APP_GROUP" "$APP_DIR/.wwebjs_auth" "$APP_DIR/.wwebjs_cache"

echo "6/7 Обновляю службы и запускаю систему"
install -m 0644 "$APP_DIR/server/edinaya-ochered.service" /etc/systemd/system/edinaya-ochered.service
install -m 0644 "$APP_DIR/server/edinaya-ochered-whatsapp.service" /etc/systemd/system/edinaya-ochered-whatsapp.service
install -m 0644 "$APP_DIR/server/edinaya-ochered-backup.service" /etc/systemd/system/edinaya-ochered-backup.service
install -m 0644 "$APP_DIR/server/edinaya-ochered-backup.timer" /etc/systemd/system/edinaya-ochered-backup.timer
chmod +x "$APP_DIR/server/"*.sh
systemctl daemon-reload
systemctl restart edinaya-ochered.service
systemctl enable --now edinaya-ochered-whatsapp.service
systemctl enable --now edinaya-ochered-backup.timer

echo "7/7 Проверяю результат"
for attempt in 1 2 3 4 5; do
    if curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null; then
        UPDATE_OK=1
        break
    fi
    sleep 2
done

if [[ "$UPDATE_OK" -ne 1 ]]; then
    echo "Сайт не прошёл проверку после обновления" >&2
    exit 1
fi

trap - EXIT
echo
echo "Обновление установлено успешно."
echo "База и WhatsApp-сессия сохранены."
echo "Копия предыдущего кода: $ROLLBACK_DIR"
echo
bash "$APP_DIR/server/check_server.sh"
