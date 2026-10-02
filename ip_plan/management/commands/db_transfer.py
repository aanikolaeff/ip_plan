"""Шаги переноса данных ip_plan между СУБД (SQLite ↔ PostgreSQL), которые
по очереди вызывает scripts/sqlite_to_postgres.sh — U3 плана
docs/plans/2026-10-03-0059-feat-observations-store-stage1-plan.md.

Команда работает с одной базой — той, что выбрана окружением
(IP_PLAN_DB_ENGINE, IP_PLAN_DB_NAME, IP_PLAN_SQLITE_PATH). Любое нарушение —
CommandError (ненулевой код выхода), скрипт на нём останавливается.

  check-source  источник: миграции на версии кода; в SQLite нет строк
                длиннее max_length, NUL и значений не того типа; нет висячих
                ссылок; пароль LDAP расшифровывается этим SECRET_KEY.
  check-target  цель пуста, кроме засеянного migrate (0003, 0008, contenttypes,
                permissions); таблиц может не быть совсем.
  dump --output FILE
                dumpdata --natural-foreign --natural-primary без contenttypes,
                auth.permission, sessions (и admin.logentry, если админка есть).
  load FILE     одной транзакцией: повторная проверка пустоты цели, удаление
                засеянных строк, loaddata, сброс последовательностей.
  verify        после загрузки: пароль LDAP расшифровывается, у каждой
                LocalAccount/MapLayout (и любой ссылки) есть цель.

Значения полей в сообщения не попадают — только модель, поле, pk и причина.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS

from ip_plan.services import db_transfer


class Command(BaseCommand):
    help = "Шаги переноса данных между СУБД (см. scripts/sqlite_to_postgres.sh)"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["check-source", "check-target", "dump", "load", "verify"])
        parser.add_argument("fixture", nargs="?", help="Файл выгрузки для load")
        parser.add_argument("--output", help="Файл выгрузки для dump")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)

    def handle(self, *args, **options):
        using = options["database"]
        action = options["action"]
        try:
            if action == "check-source":
                self._fail_on(db_transfer.source_problems(using), "Источник не годится для переноса:")
                self.stdout.write("check-source: OK — миграции, данные, ссылки и пароль LDAP в порядке")
            elif action == "check-target":
                self._fail_on(db_transfer.target_problems(using), "Целевая база не пуста:")
                self.stdout.write("check-target: OK — пусто, кроме засеянного миграциями")
            elif action == "dump":
                if not options["output"]:
                    raise CommandError("dump: нужен --output")
                db_transfer.dump(options["output"], using)
                self.stdout.write(f"dump: OK — {options['output']}")
            elif action == "load":
                if not options["fixture"]:
                    raise CommandError("load: нужен файл выгрузки")
                statements = db_transfer.load(options["fixture"], using)
                self.stdout.write(f"load: OK — загружено, последовательностей сброшено: {statements}")
            elif action == "verify":
                self._fail_on(db_transfer.verify_problems(using), "Проверка после переноса не прошла:")
                self.stdout.write("verify: OK — пароль LDAP расшифровывается, висячих ссылок нет")
        except db_transfer.TransferError as e:
            raise CommandError(e.format())

    def _fail_on(self, problems, title):
        if problems:
            raise db_transfer.TransferError(title, problems)
