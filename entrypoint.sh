#!/bin/sh
set -e

# Не на этапе docker build: там .env ещё не примонтирован, а settings.py
# требует SECRET_KEY/NETBOX_URL/NETBOX_TOKEN без значений по умолчанию.
# migrate — теперь нужен реально: с приходом LDAP-логина (django-auth-ldap)
# Django хранит сессии и локальные объекты User в db.sqlite3 (см.
# docker-compose.yml — каталог с этим файлом смонтирован volume'ом, чтобы
# пользователи/сессии не терялись при пересборке контейнера).
#
# При IP_PLAN_DB_ENGINE=postgres база — отдельный контейнер, и после
# перезапуска хоста web может стартовать раньше, чем postgres начнёт
# принимать соединения: контейнеры с restart: unless-stopped поднимает сам
# демон Docker, и depends_on/service_healthy при этом не учитывается. Без
# ожидания migrate падает, контейнер уходит в цикл рестартов. Ждём через те
# же settings, что и приложение (параметры берутся из одного места), до
# DB_WAIT_SECONDS секунд; на SQLite проверка сразу пропускается.
python - <<'PY'
import os
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.db import connection
from django.db.utils import OperationalError

if connection.vendor != "postgresql":
    sys.exit(0)
deadline = time.monotonic() + int(os.environ.get("DB_WAIT_SECONDS") or 60)
while True:
    try:
        connection.ensure_connection()
        connection.close()
        print("PostgreSQL доступна", flush=True)
        break
    except OperationalError as e:
        connection.close()
        if time.monotonic() >= deadline:
            print(f"PostgreSQL недоступна: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        print("Ждём PostgreSQL...", flush=True)
        time.sleep(2)
PY
python manage.py migrate --noinput
# Локальный администратор для аварийного входа: создаётся только если
# локальных учёток ещё нет, повторные старты пароль не сбрасывают. Нужен
# как страховка — настройки LDAP правятся из UI, и неудачная правка не
# должна оставлять снаружи всех (см. design doc, "Аутентификация").
python manage.py ensure_local_admin
python manage.py collectstatic --noinput

exec "$@"
