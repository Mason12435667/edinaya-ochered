#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запусти: sudo $0" >&2
    exit 1
fi

APP_DIR="/opt/edinaya-ochered"
ENV_FILE="/etc/edinaya-ochered/queue.env"

if [[ ! -f "$APP_DIR/whatsapp_qr_connector.js" ]]; then
    echo "Не найден $APP_DIR/whatsapp_qr_connector.js" >&2
    exit 1
fi

systemctl stop edinaya-ochered-whatsapp.service 2>/dev/null || true

echo
echo "Сейчас QR-коннектор будет запущен в этом терминале."
echo "Отсканируй QR рабочим WhatsApp."
echo "После успешного подключения нажми Ctrl+C."
echo

set +e
runuser -u queueapp -- bash -c "
    set -a
    source '$ENV_FILE'
    set +a
    cd '$APP_DIR'
    exec /usr/bin/node whatsapp_qr_connector.js
"
STATUS=$?
set -e

echo
read -r -p "Запустить WhatsApp-коннектор как постоянную службу? [Y/n]: " answer
answer="${answer:-Y}"
if [[ "$answer" =~ ^[YyДд]$ ]]; then
    systemctl enable --now edinaya-ochered-whatsapp.service
    echo "Сервис запущен."
    echo "Логи: journalctl -u edinaya-ochered-whatsapp -f"
else
    echo "Можно запустить позже:"
    echo "sudo systemctl enable --now edinaya-ochered-whatsapp"
fi

exit "$STATUS"
