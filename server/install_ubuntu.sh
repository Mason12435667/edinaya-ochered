#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запусти через sudo: sudo DOMAIN=queue.example.com bash server/install_ubuntu.sh" >&2
    exit 1
fi

DOMAIN="${DOMAIN:-}"
if [[ -z "$DOMAIN" ]]; then
    echo "Укажи действующий домен или внешний IP в переменной DOMAIN." >&2
    echo "Пример: sudo DOMAIN=queue.example.com bash server/install_ubuntu.sh" >&2
    exit 1
fi
APP_USER="queueapp"
APP_GROUP="queueapp"
APP_DIR="/opt/edinaya-ochered"
DATA_DIR="/var/lib/edinaya-ochered"
BACKUP_DIR="/var/backups/edinaya-ochered"
CONFIG_DIR="/etc/edinaya-ochered"
ENV_FILE="$CONFIG_DIR/queue.env"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE_USER="${SITE_USER:-queueadmin}"

required=(app.py ticketing.py requirements-voice.txt)
for name in "${required[@]}"; do
    if [[ ! -f "$SOURCE_DIR/$name" ]]; then
        echo "Не найден $SOURCE_DIR/$name" >&2
        echo "Положи папку server внутрь корня проекта." >&2
        exit 1
    fi
done

echo "=== Единая очередь: установка Ubuntu ==="
echo "Источник: $SOURCE_DIR"
echo "Домен:    $DOMAIN"
echo

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    python3 python3-venv python3-pip \
    nginx apache2-utils \
    sqlite3 rsync curl ca-certificates \
    nodejs \
    ufw

if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$DATA_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$BACKUP_DIR"
install -d -o root -g "$APP_GROUP" -m 0750 "$CONFIG_DIR"

# Сохраняем существующую БД из проекта, если серверная БД ещё не существует.
if [[ ! -f "$DATA_DIR/tickets.db" && -f "$SOURCE_DIR/data/tickets.db" ]]; then
    echo "Копирую существующую tickets.db..."
    install -o "$APP_USER" -g "$APP_GROUP" -m 0640 "$SOURCE_DIR/data/tickets.db" "$DATA_DIR/tickets.db"
fi

rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'node_modules/' \
    --exclude '.wwebjs_auth/' \
    --exclude '.wwebjs_cache/' \
    --exclude '__pycache__/' \
    "$SOURCE_DIR/" "$APP_DIR/"

chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-voice.txt"

if [[ -f "$APP_DIR/package.json" ]]; then
    cd "$APP_DIR"
    if [[ -f package-lock.json ]]; then
        npm ci || npm install
    else
        npm install
    fi
    chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/node_modules" 2>/dev/null || true
fi

# WhatsApp LocalAuth/кэш остаются на постоянном диске.
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$DATA_DIR/whatsapp-auth"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$DATA_DIR/whatsapp-cache"
rm -rf "$APP_DIR/.wwebjs_auth" "$APP_DIR/.wwebjs_cache"
ln -s "$DATA_DIR/whatsapp-auth" "$APP_DIR/.wwebjs_auth"
ln -s "$DATA_DIR/whatsapp-cache" "$APP_DIR/.wwebjs_cache"
chown -h "$APP_USER:$APP_GROUP" "$APP_DIR/.wwebjs_auth" "$APP_DIR/.wwebjs_cache"

if [[ ! -f "$ENV_FILE" ]]; then
    TOKEN="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    cat > "$ENV_FILE" <<EOF
QUEUE_DATA_DIR=$DATA_DIR
TICKET_APP_HOST=127.0.0.1
TICKET_APP_PORT=8000
QUEUE_ALLOWED_HOSTS=$DOMAIN,127.0.0.1,localhost
OPEN_BROWSER=0
WEBHOOK_TOKEN=$TOKEN
QUEUE_ADMIN_USER=$SITE_USER

# Несколько совместимых имён для существующего WhatsApp-коннектора.
QUEUE_SERVER_URL=http://127.0.0.1:8000
QUEUE_API_URL=http://127.0.0.1:8000
TICKET_APP_URL=http://127.0.0.1:8000
WEBHOOK_BASE_URL=http://127.0.0.1:8000
EOF
    chown root:"$APP_GROUP" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
else
    echo "Сохраняю существующий $ENV_FILE"
fi

install -m 0644 "$APP_DIR/server/edinaya-ochered.service" /etc/systemd/system/edinaya-ochered.service
install -m 0644 "$APP_DIR/server/edinaya-ochered-whatsapp.service" /etc/systemd/system/edinaya-ochered-whatsapp.service
install -m 0644 "$APP_DIR/server/edinaya-ochered-backup.service" /etc/systemd/system/edinaya-ochered-backup.service
install -m 0644 "$APP_DIR/server/edinaya-ochered-backup.timer" /etc/systemd/system/edinaya-ochered-backup.timer

chmod +x "$APP_DIR/server/"*.sh

NGINX_FILE="/etc/nginx/sites-available/edinaya-ochered"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/server/nginx-edqueue.conf" > "$NGINX_FILE"
ln -sfn "$NGINX_FILE" /etc/nginx/sites-enabled/edinaya-ochered
rm -f /etc/nginx/sites-enabled/default

echo
echo "Создай пароль для входа сотрудников на сайт."
echo "Логин по умолчанию: $SITE_USER"
if [[ ! -f /etc/nginx/.htpasswd-edinaya-ochered ]]; then
    htpasswd -c /etc/nginx/.htpasswd-edinaya-ochered "$SITE_USER"
else
    echo "Файл паролей уже существует, не перезаписываю."
    echo "Для добавления пользователя: htpasswd /etc/nginx/.htpasswd-edinaya-ochered ИМЯ"
fi
chmod 0640 /etc/nginx/.htpasswd-edinaya-ochered
chown root:www-data /etc/nginx/.htpasswd-edinaya-ochered

nginx -t

systemctl daemon-reload
systemctl enable --now edinaya-ochered.service
systemctl enable --now edinaya-ochered-backup.timer
systemctl enable --now nginx

# WhatsApp не стартуем автоматически до первого QR-входа.
systemctl disable --now edinaya-ochered-whatsapp.service 2>/dev/null || true

# Базовый firewall. SSH оставляем доступным.
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

sleep 2

echo
echo "=== Проверка ==="
if curl -fsS --max-time 5 http://127.0.0.1:8000/health; then
    echo
    echo "Python-сервер отвечает."
else
    echo
    echo "Python-сервер пока не ответил. Смотри:"
    echo "journalctl -u edinaya-ochered -n 100 --no-pager"
fi

echo
echo "Установка завершена."
echo
echo "База:       $DATA_DIR/tickets.db"
echo "Настройки:  $ENV_FILE"
echo "Бэкапы:     $BACKUP_DIR"
echo "Домен:      $DOMAIN"
echo
echo "Следующий шаг для WhatsApp:"
echo "sudo bash $APP_DIR/server/first_whatsapp_login.sh"
echo
echo "Проверка:"
echo "sudo bash $APP_DIR/server/check_server.sh"
