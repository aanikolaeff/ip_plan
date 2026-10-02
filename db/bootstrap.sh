#!/usr/bin/env bash
# Роли и базы PostgreSQL для ip_plan и хранилища наблюдений netobs
# (docs/plans/2026-10-03-0059-feat-observations-store-stage1-plan.md, KTD1, KTD4).
#
# Одна СУБД, две базы с разными владельцами:
#   ip_plan     — владелец роль ip_plan (Django). CREATEDB — только ради
#                 тестовой базы `manage.py test`, других прав сверх своей базы нет;
#   netobs      — владелец роль netbox_sync: под ней netbox-sync пишет
#                 наблюдения и выполняет все миграции схемы netobs;
#   ip_plan_ro  — веб-приложение читает netobs (поиск MAC/IP) и ничего больше:
#                 CONNECT + USAGE + SELECT, плюс default privileges, чтобы
#                 таблицы, которые netbox_sync создаст будущими миграциями,
#                 тоже оставались читаемыми без повторного запуска скрипта.
#                 default_transaction_read_only — вторая защита на случай,
#                 если кто-то когда-нибудь выдаст ей лишнее право.
#
# Скрипт идемпотентный и запускается двумя путями:
#   1) init-скриптом образа (db/initdb/10-ip-plan-bootstrap.sh) — ТОЛЬКО на
#      пустом томе pg_data, при первом старте контейнера;
#   2) вручную на уже существующем томе (init-скрипты образа второй раз не
#      выполняются, а добавить роль/право или сменить пароль может понадобиться):
#        docker compose exec postgres bash /opt/ip_plan_db/bootstrap.sh
#
# Повторный запуск ничего не пересоздаёт и не удаляет: роли и базы создаются
# только если их нет, GRANT/REVOKE/ALTER DEFAULT PRIVILEGES приводят права к
# одному и тому же итоговому состоянию. Единственное, что переписывается при
# каждом запуске, — пароли ролей (из переменных окружения): источник истины
# для паролей — .env.db, и ручной запуск — штатный способ применить новый
# пароль после его смены в .env.db / CI-переменных. Хеш SCRAM при этом
# меняется (новая соль), но пароль и все права остаются прежними.
#
# Пароли приходят из окружения контейнера postgres (env_file: .env.db).
# psql читает их сам через \getenv (не через -v в аргументах — те видны в
# `ps`), а в SQL они попадают только через format(%L), то есть экранируются
# сервером. Ни в командной строке, ни в выводе скрипта их нет.
set -euo pipefail

: "${IP_PLAN_DB_PASSWORD:?IP_PLAN_DB_PASSWORD не задан (см. .env.db.example)}"
: "${NETBOX_SYNC_DB_PASSWORD:?NETBOX_SYNC_DB_PASSWORD не задан (см. .env.db.example)}"
: "${NETOBS_RO_PASSWORD:?NETOBS_RO_PASSWORD не задан (см. .env.db.example)}"

# Подключение суперпользователем через unix-сокет внутри контейнера: в
# официальном образе локальные подключения — trust, пароль не нужен. Во время
# init-фазы сервер тоже слушает только сокет, так что один и тот же вызов
# работает в обоих путях запуска.
PG_SUPERUSER="${POSTGRES_USER:-postgres}"

run_psql() {
    local db="$1"
    shift
    psql -X -q -v ON_ERROR_STOP=1 --username "$PG_SUPERUSER" --dbname "$db" \
        "$@"
}

# --- Роли и базы (кластерный уровень, подключение к служебной базе) ---
# CREATE ROLE / CREATE DATABASE нельзя завернуть в DO-блок (CREATE DATABASE не
# работает внутри транзакции, а переменные psql не подставляются внутрь
# $$-строк), поэтому «создать, если нет» сделано через SELECT ... \gexec:
# запрос возвращает текст команды только когда объекта ещё нет.
run_psql postgres <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', r)
  FROM unnest(ARRAY['ip_plan', 'netbox_sync', 'ip_plan_ro']) AS r
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

-- Атрибуты задаются явно при каждом запуске: если роль когда-то создали
-- руками с другими флагами, скрипт вернёт её к ожидаемому виду.
ALTER ROLE ip_plan     LOGIN CREATEDB   NOSUPERUSER NOCREATEROLE;
ALTER ROLE netbox_sync LOGIN NOCREATEDB NOSUPERUSER NOCREATEROLE;
ALTER ROLE ip_plan_ro  LOGIN NOCREATEDB NOSUPERUSER NOCREATEROLE;

\getenv ip_plan_pw IP_PLAN_DB_PASSWORD
\getenv netbox_sync_pw NETBOX_SYNC_DB_PASSWORD
\getenv netobs_ro_pw NETOBS_RO_PASSWORD
SELECT format('ALTER ROLE ip_plan PASSWORD %L', :'ip_plan_pw') \gexec
SELECT format('ALTER ROLE netbox_sync PASSWORD %L', :'netbox_sync_pw') \gexec
SELECT format('ALTER ROLE ip_plan_ro PASSWORD %L', :'netobs_ro_pw') \gexec

-- Вторая защита ip_plan_ro: даже если права на запись появятся по ошибке,
-- каждая транзакция этой роли по умолчанию только читающая.
ALTER ROLE ip_plan_ro SET default_transaction_read_only = on;

SELECT 'CREATE DATABASE ip_plan OWNER ip_plan'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'ip_plan')
\gexec
SELECT 'CREATE DATABASE netobs OWNER netbox_sync'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'netobs')
\gexec

-- Владельцы — и на уже существующих базах (например, созданных руками).
ALTER DATABASE ip_plan OWNER TO ip_plan;
ALTER DATABASE netobs  OWNER TO netbox_sync;

-- По умолчанию PUBLIC (то есть любая роль) имеет CONNECT и TEMP на каждую
-- базу. Убираем: в netobs подключаются только владелец и ip_plan_ro, в
-- ip_plan — только сам ip_plan (ни netbox_sync, ни ip_plan_ro туда не ходят).
REVOKE ALL ON DATABASE netobs  FROM PUBLIC;
REVOKE ALL ON DATABASE ip_plan FROM PUBLIC;
GRANT CONNECT ON DATABASE netobs TO ip_plan_ro;
SQL

# --- Права внутри netobs (уровень схемы — нужно подключение к самой базе) ---
# С PostgreSQL 15 схема public принадлежит pg_database_owner (то есть
# netbox_sync), а CREATE в ней у PUBLIC нет — ip_plan_ro создать таблицу не
# может без всяких дополнительных REVOKE.
run_psql netobs <<'SQL'
GRANT USAGE ON SCHEMA public TO ip_plan_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ip_plan_ro;
-- Таблицы, которые netbox_sync создаст ПОЗЖЕ (миграции netobs_migrate.py),
-- автоматически получают SELECT для ip_plan_ro. Действует только на объекты,
-- созданные именно ролью netbox_sync, — поэтому миграции netobs обязаны идти
-- под ней, а не под суперпользователем.
ALTER DEFAULT PRIVILEGES FOR ROLE netbox_sync IN SCHEMA public
    GRANT SELECT ON TABLES TO ip_plan_ro;
SQL

echo "bootstrap: роли ip_plan, netbox_sync, ip_plan_ro и базы ip_plan, netobs приведены к ожидаемому виду"
