"""Перенос данных ip_plan между СУБД: SQLite → PostgreSQL и обратно (U3 плана
docs/plans/2026-10-03-0059-feat-observations-store-stage1-plan.md).

Сам перенос ведёт scripts/sqlite_to_postgres.sh, по шагам через
management-команды db_counts и db_transfer. Здесь — то, что им общее и что
проверяют тесты: какие модели переносятся, подсчёт строк, предпроверки
источника и цели, загрузка одной транзакцией и проверки после неё.

Каждая команда работает с одной базой (той, что выбрана окружением:
IP_PLAN_DB_ENGINE/IP_PLAN_DB_NAME/IP_PLAN_SQLITE_PATH). Сверка двух баз идёт
через файл: счётчики источника сохраняются, цель сравнивается с ними.
"""
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.core.management import call_command
from django.core.management.color import no_style
from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils.dateparse import parse_date, parse_datetime, parse_time

# Не переносятся. contenttypes и auth.Permission заново создаёт migrate на
# цели, а выгрузка (--natural-foreign) ссылается на них по естественным
# ключам; их числа в двух базах законно расходятся (в старой базе остаются
# типы удалённых моделей). Сессии не переносятся намеренно — пользователи
# входят заново. admin.LogEntry — на случай, если админку подключат: сейчас
# django.contrib.admin в INSTALLED_APPS нет. Задания сборщика (SyncJob) —
# журнал работы, который бывает только на PostgreSQL (сборщик на SQLite не
# работает, KTD1 плана 2026-10-04-0248): в SQLite их не переносим, а в
# обратную сторону им взяться неоткуда. Расписания — настройка, переносятся.
EXCLUDED_MODELS = frozenset({
    "contenttypes.contenttype", "auth.permission", "sessions.session", "admin.logentry",
    "ip_plan.syncjob",
})
_DUMP_EXCLUDES = ("contenttypes", "auth.permission", "sessions", "admin.logentry",
                  "ip_plan.syncjob")

# Что засевают миграции и что поэтому может лежать в «пустой» целевой базе
# сразу после migrate: 0003 — имена объектов xFW, 0008 — синглтон LDAP,
# 0013 — расписания ночи «До nmap» / «После nmap». Засеянное расписание
# узнаётся по пометке в updated_by: правка в интерфейсе пишет туда имя, а
# сборщик меняет только next_run — такая строка всё ещё засеянная.
SEEDED_FIREWALL_NAMES = {"ip": "BLOCK_BY_IP_2", "fqdn": "BBDNS_1"}
SEEDED_LDAP_MARK = "migration 0008"
SEEDED_SCHEDULE_MARK = "migration 0013"
SEEDED_SCHEDULE_KINDS = frozenset({"night_before_nmap", "night_after_nmap"})

# Таблицы, наполняемые самим migrate (post_migrate), — их строки в целевой
# базе не делают её «непустой».
_MIGRATE_FILLED = frozenset({"contenttypes.contenttype", "auth.permission"})

# Диапазоны целых в PostgreSQL: SQLite хранит любое 64-битное число в любой
# integer-колонке, PostgreSQL на выходе за диапазон отвергнет всю загрузку.
_INT32 = (-2 ** 31, 2 ** 31 - 1)
_INT_RANGES = {
    "AutoField": _INT32,
    "IntegerField": _INT32,
    "SmallAutoField": (-2 ** 15, 2 ** 15 - 1),
    "SmallIntegerField": (-2 ** 15, 2 ** 15 - 1),
    "BigAutoField": (-2 ** 63, 2 ** 63 - 1),
    "BigIntegerField": (-2 ** 63, 2 ** 63 - 1),
    "PositiveIntegerField": (0, _INT32[1]),
    "PositiveSmallIntegerField": (0, 2 ** 15 - 1),
    "PositiveBigIntegerField": (0, 2 ** 63 - 1),
    "DurationField": (-2 ** 63, 2 ** 63 - 1),
}
_STRING_TYPES = frozenset({
    "CharField", "TextField", "SlugField", "EmailField", "URLField",
    "GenericIPAddressField", "IPAddressField", "FilePathField", "FileField", "ImageField",
})

# Сколько нарушений показывать: остальное — числом, чтобы вывод не утонул.
PROBLEM_LIMIT = 50


class TransferError(Exception):
    """Перенос нельзя продолжать: заголовок и список причин."""

    def __init__(self, title, problems):
        self.title = title
        self.problems = list(problems)
        super().__init__(self.format())

    def format(self):
        shown = self.problems[:PROBLEM_LIMIT]
        lines = [self.title] + [f"  - {p}" for p in shown]
        if len(self.problems) > len(shown):
            lines.append(f"  … и ещё {len(self.problems) - len(shown)}")
        return "\n".join(lines)


def transferred_models():
    """Модели, строки которых переносятся, — включая автоматические таблицы
    many-to-many (группы пользователя и т.п.), отсортированные по метке."""
    models = [m for m in apps.get_models(include_auto_created=True)
              if m._meta.label_lower not in EXCLUDED_MODELS]
    return sorted(models, key=lambda m: m._meta.label)


def dump_excludes():
    """Исключения для dumpdata — только установленных приложений: dumpdata
    отвергает метку неизвестного приложения (admin сейчас не установлен)."""
    installed = {a.label for a in apps.get_app_configs()}
    return [e for e in _DUMP_EXCLUDES if e.split(".")[0] in installed]


def _tables(using):
    return set(connections[using].introspection.table_names())


# --- Счётчики -------------------------------------------------------------------

def count_rows(using="default"):
    """{метка модели: число строк}; None — таблицы в базе нет вовсе."""
    tables = _tables(using)
    result = {}
    for model in transferred_models():
        if model._meta.db_table in tables:
            result[model._meta.label] = model._default_manager.using(using).count()
        else:
            result[model._meta.label] = None
    return result


def format_counts(counts, as_json=False):
    if as_json:
        return json.dumps(counts, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    # «-» — нет таблицы: отличается от нуля, чтобы такая база не сошлась
    # с базой, где таблица есть, но пуста.
    return "".join(f"{label}\t{'-' if n is None else n}\n" for label, n in sorted(counts.items()))


def parse_counts(text):
    """Обратное к format_counts: принимает и JSON, и строки «метка<TAB>число»."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    counts = {}
    for line in text.splitlines():
        label, _, value = line.partition("\t")
        counts[label.strip()] = None if value.strip() == "-" else int(value)
    return counts


def compare_counts(expected, actual):
    """[(метка, ожидалось, в базе)] по моделям с расхождением."""
    return [(label, expected.get(label), actual.get(label))
            for label in sorted(set(expected) | set(actual))
            if expected.get(label) != actual.get(label)]


# --- Предпроверки источника --------------------------------------------------------

def migration_problems(using="default"):
    """Схема базы не на той версии миграций, что код: либо не всё применено,
    либо применено то, чего в коде нет (база новее кода)."""
    executor = MigrationExecutor(connections[using])
    loader = executor.loader
    problems = [f"миграция не применена: {m.app_label}.{m.name}"
                for m, _ in executor.migration_plan(loader.graph.leaf_nodes())]
    # Строки приложений, которых в коде больше нет, переносу не мешают —
    # их таблицы в выгрузку не попадут.
    for app_label, name in sorted(loader.applied_migrations):
        if app_label in loader.migrated_apps and (app_label, name) not in loader.graph.nodes:
            problems.append(f"в базе применена миграция, которой нет в коде: {app_label}.{name}")
    return problems


def _value_problem(field, value):
    """Почему значение из SQLite не загрузится в PostgreSQL (или None).
    Само значение в ответ не попадает: это может быть секрет."""
    if value is None:
        return None if field.null else "NULL в обязательном поле"
    kind_field = field.target_field if field.is_relation else field
    kind = kind_field.get_internal_type()
    if kind in _STRING_TYPES:
        if not isinstance(value, str):
            return f"ожидалась строка, в базе {type(value).__name__}"
        if "\x00" in value:
            return "символ NUL"
        max_length = kind_field.max_length
        if kind != "TextField" and max_length and len(value) > max_length:
            return f"длина {len(value)} > max_length {max_length}"
        return None
    if kind == "JSONField":
        if not isinstance(value, str):
            return f"ожидался JSON-текст, в базе {type(value).__name__}"
        # \u0000 — допустимый JSON, но jsonb PostgreSQL его не принимает.
        if "\x00" in value or "\\u0000" in value:
            return "символ NUL в JSON"
        try:
            json.loads(value)
        except ValueError:
            return "невалидный JSON"
        return None
    if kind in _INT_RANGES:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"ожидалось целое, в базе {type(value).__name__}"
        low, high = _INT_RANGES[kind]
        if not low <= value <= high:
            return f"целое вне диапазона {kind} PostgreSQL"
        return None
    if kind == "BooleanField":
        return None if value in (0, 1) and isinstance(value, int) else "ожидалось 0/1"
    if kind == "FloatField":
        return None if isinstance(value, (int, float)) else f"ожидалось число, в базе {type(value).__name__}"
    if kind == "DecimalField":
        try:
            Decimal(str(value))
        except InvalidOperation:
            return "ожидалось десятичное число"
        return None
    parsers = {"DateTimeField": parse_datetime, "DateField": parse_date, "TimeField": parse_time}
    if kind in parsers:
        if not isinstance(value, str):
            return f"ожидалась дата/время текстом, в базе {type(value).__name__}"
        try:
            ok = parsers[kind](value) is not None
        except ValueError:
            ok = False
        return None if ok else "не разбирается как дата/время"
    if kind == "UUIDField":
        try:
            uuid.UUID(str(value))
        except ValueError:
            return "не UUID"
        return None
    if kind == "BinaryField":
        return None if isinstance(value, (bytes, memoryview)) else "ожидались байты"
    return None


def data_problems(using="default"):
    """Значения, которые SQLite принял, а PostgreSQL не примет: строки длиннее
    max_length, NUL, значения не того типа. loaddata откатился бы целиком —
    лучше узнать до того, как трогать цель.

    Только для SQLite-источника: в PostgreSQL всё это уже проверила сама СУБД
    (а SQLite при обратном переносе примет что угодно)."""
    connection = connections[using]
    if connection.vendor != "sqlite":
        return []
    qn = connection.ops.quote_name
    tables = _tables(using)
    problems = []
    for model in transferred_models():
        opts = model._meta
        if opts.db_table not in tables:
            continue
        fields = list(opts.concrete_fields)
        pk_index = fields.index(opts.pk)
        # Унарный «+» в SQLite ничего не меняет в значении, но у выражения
        # нет объявленного типа — и модуль sqlite3 не применяет к нему
        # конвертеры Django (date/datetime/bool). Так видно то, что лежит в
        # файле на самом деле, а не результат разбора.
        columns = ", ".join(f"+{qn(f.column)}" for f in fields)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {columns} FROM {qn(opts.db_table)}")
            for row in cursor.fetchall():
                for field, value in zip(fields, row):
                    problem = _value_problem(field, value)
                    if problem:
                        problems.append(f"{opts.label}.{field.name} (pk={row[pk_index]}): {problem}")
    return problems


def orphan_problems(using="default"):
    """Строки со ссылкой на несуществующую запись. В PostgreSQL ограничение
    внешнего ключа их не пропустит, а dumpdata --natural-foreign на них
    падает; заодно это проверка «у каждой LocalAccount и MapLayout есть
    пользователь»."""
    connection = connections[using]
    qn = connection.ops.quote_name
    tables = _tables(using)
    problems = []
    for model in transferred_models():
        opts = model._meta
        if opts.db_table not in tables:
            continue
        for field in opts.concrete_fields:
            if not (field.is_relation and (field.many_to_one or field.one_to_one)):
                continue
            target = field.related_model._meta
            if target.db_table not in tables:
                continue
            sql = (
                f"SELECT COUNT(*) FROM {qn(opts.db_table)} t "
                f"WHERE t.{qn(field.column)} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {qn(target.db_table)} r "
                f"WHERE r.{qn(field.target_field.column)} = t.{qn(field.column)})"
            )
            with connection.cursor() as cursor:
                cursor.execute(sql)
                missing = cursor.fetchone()[0]
            if missing:
                problems.append(f"{opts.label}.{field.name}: {missing} строк ссылаются "
                                f"на несуществующие {target.label}")
    return problems


def ldap_problems(using="default"):
    from ip_plan.models import LdapConfig
    from ip_plan.services import secretbox
    if LdapConfig._meta.db_table not in _tables(using):
        return []
    problems = []
    for cfg in LdapConfig.objects.using(using).all():
        if not secretbox.is_readable(cfg.bind_password_encrypted):
            problems.append(
                f"пароль LDAP (LdapConfig pk={cfg.pk}) не расшифровывается: SECRET_KEY "
                f"не тот, с которым он сохранён, — перенос с другим ключом оставит LDAP без пароля")
    return problems


def source_problems(using="default"):
    return (migration_problems(using) + data_problems(using)
            + orphan_problems(using) + ldap_problems(using))


# --- Цель -------------------------------------------------------------------------

def target_problems(using="default"):
    """Целевая база должна быть пуста, кроме того, что засеял migrate:
    contenttypes/permissions, имена объектов xFW (0003), синглтон LDAP (0008)
    и расписания ночи (0013) — причём ровно в засеянном виде. Таблиц может не быть вовсе (migrate ещё
    не выполнялся) — это тоже пустая база."""
    from ip_plan.models import FirewallObjectName, LdapConfig, SyncSchedule
    tables = _tables(using)
    problems = []
    for model in apps.get_models(include_auto_created=True):
        opts = model._meta
        if opts.label_lower in _MIGRATE_FILLED or opts.db_table not in tables:
            continue
        manager = model._default_manager.using(using)
        if model is FirewallObjectName:
            rows = dict(manager.values_list("kind", "current_name"))
            if rows and rows != SEEDED_FIREWALL_NAMES:
                problems.append(f"{opts.label}: строки отличаются от засеянных миграцией 0003")
            continue
        if model is LdapConfig:
            rows = list(manager.values_list("pk", "updated_by"))
            seeded = len(rows) == 1 and rows[0][0] == 1 and (rows[0][1] or "").startswith(SEEDED_LDAP_MARK)
            if rows and not seeded:
                problems.append(f"{opts.label}: настройки уже менялись после миграции 0008")
            continue
        if model is SyncSchedule:
            rows = list(manager.values_list("kind", "updated_by"))
            if any(kind not in SEEDED_SCHEDULE_KINDS or not (by or "").startswith(SEEDED_SCHEDULE_MARK)
                   for kind, by in rows):
                problems.append(f"{opts.label}: расписания отличаются от засеянных миграцией 0013")
            continue
        count = manager.count()
        if count:
            problems.append(f"{opts.label}: {count} строк")
    return problems


def dump(output, using="default"):
    call_command(
        "dumpdata", natural_foreign=True, natural_primary=True,
        exclude=dump_excludes(), database=using, output=output, verbosity=0,
    )


def reset_sequences(using="default"):
    """Выполняет SQL, который печатает sqlsequencereset (сама команда только
    печатает). loaddata сбрасывает последовательности загруженных моделей и
    сам — здесь все модели, включая таблицы many-to-many, на всякий случай.
    В SQLite SQL пустой: там счётчик сам идёт от максимального ключа."""
    connection = connections[using]
    statements = connection.ops.sequence_reset_sql(no_style(), apps.get_models())
    with connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)
    return len(statements)


def load(fixture, using="default"):
    """Удаление засеянных строк и загрузка — одной транзакцией: при сбое
    загрузки не останется базы без синглтона LDAP, имён объектов xFW и
    расписаний ночи."""
    from ip_plan.models import FirewallObjectName, LdapConfig, SyncSchedule
    problems = migration_problems(using)
    if problems:
        raise TransferError("Целевая база не на версии миграций кода (сначала migrate):", problems)
    with transaction.atomic(using=using):
        problems = target_problems(using)
        if problems:
            raise TransferError("Целевая база не пуста — перенос в неё отменён:", problems)
        FirewallObjectName.objects.using(using).all().delete()
        LdapConfig.objects.using(using).all().delete()
        # Задания на цели пусты (target_problems), так что ссылок на
        # засеянные расписания нет; у источника свои — с теми же видами.
        SyncSchedule.objects.using(using).all().delete()
        call_command("loaddata", fixture, database=using, verbosity=0)
        return reset_sequences(using)


def verify_problems(using="default"):
    """Проверки после загрузки: пароль LDAP расшифровывается этим SECRET_KEY,
    висячих ссылок (LocalAccount/MapLayout без пользователя и т.п.) нет."""
    return ldap_problems(using) + orphan_problems(using)
