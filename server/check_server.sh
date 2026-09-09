#!/usr/bin/env bash
set -u

echo "=== Единая очередь: проверка ==="
echo

for service in edinaya-ochered nginx edinaya-ochered-whatsapp; do
    printf "%-32s " "$service"
    if systemctl is-active --quiet "$service"; then
        echo "OK"
    else
        echo "НЕ ЗАПУЩЕН"
    fi
done

echo
echo "HTTP health:"
if curl -fsS --max-time 5 http://127.0.0.1:8000/health; then
    echo
else
    echo "Ошибка обращения к Python-серверу"
fi

echo
echo "База:"
ls -lh /var/lib/edinaya-ochered/tickets.db 2>/dev/null || echo "tickets.db ещё не создана"

echo
echo "Свободное место:"
df -h /var/lib/edinaya-ochered 2>/dev/null | tail -n 1 || true

echo
echo "Последние бэкапы:"
ls -lht /var/backups/edinaya-ochered/tickets-*.db.gz 2>/dev/null | head -n 5 || echo "Бэкапов пока нет"

echo
echo "Последние ошибки Python:"
journalctl -u edinaya-ochered --no-pager -n 10 -p warning 2>/dev/null || true
