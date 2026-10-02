#!/usr/bin/env bash
# Ежедневный бэкап PostgreSQL из compose: pg_dump -Fc по каждой базе и
# pg_dumpall --globals-only (роли и их пароли-хеши) в каталог на хосте ВНЕ
# тома pg_data — иначе потеря тома забирала бы и копии.
#
# Дамп идёт через `docker compose exec -T postgres` под пользователем
# postgres по unix-сокету внутри контейнера: пароль не нужен и в командной
# строке/окружении не светится.
#
# Параметры (переменные окружения):
#   BACKUP_DIR      куда писать (по умолчанию /var/backups/ip_plan_pg)
#   RETENTION_DAYS  сколько дней хранить (по умолчанию 14)
#   PG_DATABASES    базы через пробел (по умолчанию "ip_plan netobs")
#
# Содержимое дампов — вся база целиком (хеши паролей, шифрованный
# bind-пароль LDAP), поэтому umask 077, каталог 0700, файлы 0600.
#
# Код возврата: 0 — всё снято; не 0 — упал любой дамп (его полупустой файл
# не остаётся: пишем во временный .tmp и переименовываем только после успеха).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/ip_plan_pg}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
PG_DATABASES="${PG_DATABASES:-ip_plan netobs}"

# Работаем из каталога compose, откуда бы скрипт ни запустили (cron: cwd = /).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_FILES=()
# Любой выход (в том числе по ошибке) убирает недописанные .tmp.
cleanup() { [ "${#TMP_FILES[@]}" -eq 0 ] || rm -f "${TMP_FILES[@]}"; }
trap cleanup EXIT

# dump <имя-файла> <команда внутри контейнера...>
dump() {
  local name="$1"; shift
  local tmp="$BACKUP_DIR/.$name.tmp"
  TMP_FILES+=("$tmp")
  docker compose exec -T -u postgres postgres "$@" > "$tmp"
  [ -s "$tmp" ] || { echo "ПУСТОЙ дамп: $name" >&2; return 1; }
  chmod 600 "$tmp"
  mv "$tmp" "$BACKUP_DIR/$name"
  echo "ok: $BACKUP_DIR/$name ($(wc -c < "$BACKUP_DIR/$name") байт)"
}

for db in $PG_DATABASES; do
  dump "${db}_${STAMP}.dump" pg_dump -Fc -U postgres -d "$db"
done
dump "globals_${STAMP}.sql" pg_dumpall -U postgres --globals-only

# Ротация — только после успешных дампов, чтобы сбой не съел старые копии.
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name '*_[0-9]*-[0-9]*.dump' -o -name 'globals_*.sql' \) \
  -mtime +"$RETENTION_DAYS" -print -delete
