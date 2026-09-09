# Серверные файлы

- `install_ubuntu.sh` предназначен только для первой установки.
- `update_ubuntu.sh` обновляет действующий сервер и сохраняет базу с WhatsApp-сессией.
- `check_server.sh` показывает состояние служб, базы и резервных копий.
- `backup_sqlite.sh` создаёт резервную копию SQLite.
- `restore_sqlite.sh` восстанавливает выбранную резервную копию.
- `first_whatsapp_login.sh` оставлен как запасной вход через QR в терминале.
- `enable_https.sh` подключает Certbot для реального домена.

Основные пути на Ubuntu:

```text
/opt/edinaya-ochered                 код
/var/lib/edinaya-ochered/tickets.db база
/var/lib/edinaya-ochered/whatsapp-auth сессия WhatsApp
/etc/edinaya-ochered/queue.env      настройки и токен
/var/backups/edinaya-ochered        резервные копии
```

Обычное обновление:

```bash
cd ПАПКА_С_РАСПАКОВАННЫМ_ОБНОВЛЕНИЕМ
sudo bash server/update_ubuntu.sh
```

Обновление не меняет домен и существующие пароли Nginx.
