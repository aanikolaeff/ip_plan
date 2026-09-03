#!/bin/sh
set -e

# Не на этапе docker build: там .env ещё не примонтирован, а settings.py
# требует SECRET_KEY/NETBOX_URL/NETBOX_TOKEN без значений по умолчанию.
# migrate — теперь нужен реально: с приходом LDAP-логина (django-auth-ldap)
# Django хранит сессии и локальные объекты User в db.sqlite3 (см.
# docker-compose.yml — каталог с этим файлом смонтирован volume'ом, чтобы
# пользователи/сессии не терялись при пересборке контейнера).
python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
