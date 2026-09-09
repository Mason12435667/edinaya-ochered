# Секреты проекта

Рабочие токены, API-ключи, пароли и приватные ключи не должны храниться в `/opt/edinaya-ochered` и не должны попадать в Git.

На Ubuntu секреты хранятся в:

`/etc/edinaya-ochered/secrets.env`

Файл принадлежит root и имеет права `0600`. systemd загружает его отдельно для сайта и WhatsApp-коннектора.

Пример переменных без значений находится в `secrets.env.example`.

Проверка исходников перед публикацией:

`python3 tools/check_source_secrets.py /opt/edinaya-ochered`
