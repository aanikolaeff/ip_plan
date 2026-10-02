#!/usr/bin/env bash
# Перенос данных ip_plan SQLite → PostgreSQL (и обратно, --reverse) — U3 плана
# docs/plans/2026-10-03-0059-feat-observations-store-stage1-plan.md.
#
# Повторяемая процедура с проверкой полноты. Каждый шаг — отдельный
# `docker compose run` образа web с нужной базой в окружении; логика шагов —
# в management-командах db_transfer и db_counts (ip_plan/services/db_transfer.py).
# Любая ошибка останавливает скрипт.
#
#   1. Предпроверки источника: миграции на версии кода; в SQLite нет строк
#      длиннее max_length, NUL и значений не того типа; нет висячих ссылок;
#      пароль LDAP расшифровывается этим SECRET_KEY. Затем — цель пуста,
#      кроме засеянного миграциями. До этого момента цель только читается.
#   2. (--stop-web) остановить web; снять согласованную копию SQLite в
#      каталог переноса (0700) и выгрузить dumpdata --natural-foreign
#      --natural-primary без contenttypes, auth.permission, sessions.
#   3. migrate на цели с SEED_LOCAL_ADMIN=false.
#   4–5. Удаление засеянных строк (0003, 0008), loaddata и сброс
#      последовательностей — одной транзакцией (db_transfer load).
#   6. Проверки: db_counts цели = db_counts источника по каждой модели,
#      пароль LDAP расшифровывается, у LocalAccount/MapLayout есть пользователь.
#   7. IP_PLAN_DB_ENGINE скрипт НЕ переключает — печатает итог и следующий шаг.
#   8. --reverse: PostgreSQL → новый файл SQLite (путь отката, если после
#      переключения в PostgreSQL уже писали).
#   9. Сессии не переносятся: пользователи входят заново.
#
# Каталог переноса — в томе sqlite_data (/app/data/xfer/<время>-<направление>/):
# копия SQLite, выгрузка dump.json, счётчики источника и цели. Его НЕ удалять:
# по выгрузке откат проверяет, писалось ли что-то после переключения. В
# выгрузке хеши паролей и зашифрованный пароль LDAP — отсюда права 0700.
#
# Рабочий файл SQLite (/app/data/db.sqlite3) скрипт не изменяет никогда: при
# прямом переносе читается его копия, при обратном пишется новый файл.
#
# Usage (из корня репозитория, образ web собран из текущего кода, postgres
# запущен):
#   scripts/sqlite_to_postgres.sh --stop-web                 # рабочий перенос в базу ip_plan
#   scripts/sqlite_to_postgres.sh --sqlite /app/data/db.copy.sqlite3 --pg-db ip_plan_xfer_test
#   scripts/sqlite_to_postgres.sh --reverse --stop-web       # PostgreSQL → новый SQLite
#
# Ключи:
#   --stop-web     остановить web перед снятием копии (для рабочего переноса —
#                  обязательно, иначе записи после копии в цель не попадут).
#                  Обратно web скрипт не запускает: следующий шаг — переключение.
#   --sqlite PATH  путь ВНУТРИ контейнера. Прямой перенос: источник (по
#                  умолчанию /app/data/db.sqlite3). Обратный: новый файл-цель
#                  (по умолчанию /app/data/db.from_postgres.<время>.sqlite3),
#                  существующий файл не перезаписывается.
#   --pg-db NAME   база PostgreSQL (по умолчанию ip_plan).
#   --reverse      направление PostgreSQL → SQLite.
set -Eeuo pipefail

cd "$(dirname "$0")/.."

REVERSE=0
STOP_WEB=0
SQLITE_PATH=""
PG_DB="ip_plan"

usage() { sed -n '2,/^set -Eeuo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --reverse) REVERSE=1 ;;
        --stop-web) STOP_WEB=1 ;;
        --sqlite) SQLITE_PATH="${2:?--sqlite: нужен путь}"; shift ;;
        --pg-db) PG_DB="${2:?--pg-db: нужно имя базы}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Неизвестный ключ: $1 (см. --help)" >&2; exit 2 ;;
    esac
    shift
done

TS="$(date +%Y%m%d-%H%M%S)"
if [ "$REVERSE" = 1 ]; then
    DIRECTION="pg-to-sqlite"
    SQLITE_PATH="${SQLITE_PATH:-/app/data/db.from_postgres.$TS.sqlite3}"
else
    DIRECTION="sqlite-to-pg"
    SQLITE_PATH="${SQLITE_PATH:-/app/data/db.sqlite3}"
fi
XDIR="/app/data/xfer/$TS-$DIRECTION"

STEP="подготовка"
TARGET_LOADED=0
on_error() {
    local code=$?
    echo >&2
    echo "ОСТАНОВ на шаге «$STEP» (код $code). Рабочий файл SQLite не изменялся." >&2
    if [ "$TARGET_LOADED" = 1 ]; then
        echo "Данные в цель уже загружены — повторный запуск откажется (цель не пуста)." >&2
        if [ "$REVERSE" = 1 ]; then
            echo "Удалите файл-цель $SQLITE_PATH и запустите снова." >&2
        else
            echo "Пересоздайте базу $PG_DB (DROP/CREATE DATABASE … OWNER ip_plan) и запустите снова." >&2
        fi
    else
        echo "Цель не заполнялась: после устранения причины скрипт можно запустить снова." >&2
        if [ "$REVERSE" = 1 ]; then
            echo "(при обратном переносе — удалив файл-цель $SQLITE_PATH, если migrate успел его создать)" >&2
        fi
    fi
    if [ "$STOP_WEB" = 1 ]; then
        echo "web остановлен скриптом; вернуть как было: docker compose start web" >&2
    fi
    echo "Каталог переноса (в томе sqlite_data): $XDIR" >&2
    exit "$code"
}
trap on_error ERR

# Один шаг — один контейнер. SEED_LOCAL_ADMIN=false: ни один шаг не должен
# заводить admin/admin (migrate его и не заводит — это делает entrypoint,
# который здесь не запускается, но пусть будет явно).
dc_run() { docker compose run --rm --no-deps -T -e SEED_LOCAL_ADMIN=false "$@"; }
on_sqlite() { local path="$1"; shift
    dc_run -e IP_PLAN_DB_ENGINE=sqlite -e IP_PLAN_SQLITE_PATH="$path" --entrypoint python web manage.py "$@"; }
on_pg() { dc_run -e IP_PLAN_DB_ENGINE=postgres -e IP_PLAN_DB_NAME="$PG_DB" --entrypoint python web manage.py "$@"; }

if [ "$REVERSE" = 1 ]; then
    src() { on_pg "$@"; }
    tgt() { on_sqlite "$SQLITE_PATH" "$@"; }
    SRC_LABEL="PostgreSQL, база $PG_DB"
    TGT_LABEL="SQLite, новый файл $SQLITE_PATH"
else
    src() { on_sqlite "$XDIR/source.sqlite3" "$@"; }
    tgt() { on_pg "$@"; }
    SRC_LABEL="SQLite $SQLITE_PATH (через копию в каталоге переноса)"
    TGT_LABEL="PostgreSQL, база $PG_DB"
fi

say() { echo; echo "== $*"; }

say "Перенос $DIRECTION: $SRC_LABEL → $TGT_LABEL"
echo "Каталог переноса: $XDIR (том sqlite_data)"

STEP="проверка окружения"
docker compose exec -T postgres pg_isready -q -h 127.0.0.1 \
    || { echo "Сервис postgres не отвечает: docker compose up -d postgres" >&2; false; }
if [ "$REVERSE" = 1 ]; then
    if dc_run --entrypoint test web -e "$SQLITE_PATH"; then
        echo "Файл-цель $SQLITE_PATH уже существует — перезаписывать не буду" >&2; false
    fi
else
    dc_run --entrypoint test web -f "$SQLITE_PATH" \
        || { echo "Нет файла SQLite $SQLITE_PATH" >&2; false; }
fi

if [ "$STOP_WEB" = 1 ]; then
    STEP="остановка web"
    say "Останавливаю web"
    docker compose stop web
else
    echo "web не останавливается (--stop-web не задан): записи после снятия копии в цель не попадут."
fi

STEP="каталог переноса"
dc_run --entrypoint sh web -c 'umask 077 && mkdir -p "$1" && chmod 700 /app/data/xfer "$1"' sh "$XDIR"

if [ "$REVERSE" = 0 ]; then
    STEP="копия SQLite"
    say "1. Копия SQLite (backup API: согласованный снимок, исходник открыт только на чтение)"
    dc_run --entrypoint python web -c '
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
print("копия:", sys.argv[2])
' "$SQLITE_PATH" "$XDIR/source.sqlite3"
fi

STEP="предпроверка источника"
say "1. Предпроверка источника"
src db_transfer check-source

STEP="предпроверка цели"
say "1. Предпроверка цели (до migrate)"
tgt db_transfer check-target

STEP="выгрузка"
say "2. Выгрузка и счётчики источника"
src db_transfer dump --output "$XDIR/dump.json"
src db_counts --output "$XDIR/counts_source.tsv" >/dev/null

STEP="migrate цели"
say "3. migrate на цели"
tgt migrate --noinput

STEP="загрузка"
say "4–5. Удаление засеянных строк, loaddata и сброс последовательностей (одна транзакция)"
TARGET_LOADED=1
tgt db_transfer load "$XDIR/dump.json"

STEP="сверка числа строк"
say "6. Сверка числа строк по моделям"
tgt db_counts --compare "$XDIR/counts_source.tsv" --output "$XDIR/counts_target.tsv"

STEP="проверки после загрузки"
say "6. Пароль LDAP и ссылки на пользователей"
tgt db_transfer verify

trap - ERR
say "ГОТОВО: $SRC_LABEL → $TGT_LABEL, все проверки пройдены"
echo "Выгрузка переноса (сохранить — нужна для отката): $XDIR/dump.json"
echo "Счётчики: $XDIR/counts_source.tsv, $XDIR/counts_target.tsv"
echo "Достать на хост: docker compose cp web:$XDIR ./ (web должен существовать как контейнер)"
echo
if [ "$REVERSE" = 1 ]; then
    cat <<EOF
Следующий шаг (вручную), обратное переключение на SQLite:
  1. docker compose stop web
  2. отложить прежний файл и поставить новый на его место:
     docker compose run --rm --no-deps --entrypoint sh web -c \\
       'mv /app/data/db.sqlite3 /app/data/db.sqlite3.before-$TS && mv $SQLITE_PATH /app/data/db.sqlite3'
  3. IP_PLAN_DB_ENGINE=sqlite (локально — .env; на проде — CI-переменная и деплой)
  4. docker compose up -d web
EOF
else
    cat <<EOF
Следующий шаг (вручную), переключение на PostgreSQL:
  1. IP_PLAN_DB_ENGINE=postgres$( [ "$PG_DB" != ip_plan ] && echo " и IP_PLAN_DB_NAME=$PG_DB" )
     (локально — .env; на проде — CI-переменная и деплой: .env там перезаписывает CI)
  2. docker compose up -d web
  3. Вход по LDAP и по локальной учётке, реестр, настройки, карта.
Файл $SQLITE_PATH не изменён — он источник отката (IP_PLAN_DB_ENGINE=sqlite).
Сессии не перенесены: пользователи входят заново.
EOF
    if [ "$STOP_WEB" = 0 ]; then
        echo "ВНИМАНИЕ: web не останавливался — это был пробный перенос; для рабочего нужен --stop-web."
    fi
fi
