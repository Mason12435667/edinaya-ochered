#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Запусти через sudo." >&2
    exit 1
fi

DOMAIN="${DOMAIN:-}"
if [[ -z "$DOMAIN" ]]; then
    echo "Укажи действующий домен: sudo DOMAIN=queue.example.com bash $0" >&2
    exit 1
fi

echo "Домен: $DOMAIN"
echo
echo "Перед продолжением проверь:"
echo "1) домен указывает на внешний IP этого сервера;"
echo "2) у провайдера есть белый IP, а не CGNAT;"
echo "3) TCP 80 и 443 проброшены на Ubuntu."
echo
read -r -p "Продолжить выпуск HTTPS-сертификата? [y/N]: " answer
if [[ ! "${answer:-N}" =~ ^[YyДд]$ ]]; then
    exit 0
fi

apt-get update
apt-get install -y certbot python3-certbot-nginx

certbot --nginx -d "$DOMAIN"

nginx -t
systemctl reload nginx

echo
echo "HTTPS настроен."
echo "Проверка автопродления:"
systemctl status certbot.timer --no-pager 2>/dev/null || true
