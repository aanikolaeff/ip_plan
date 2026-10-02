"""Число строк по каждой переносимой модели — для сверки двух баз при
переносе SQLite ↔ PostgreSQL (scripts/sqlite_to_postgres.sh, U3 плана
docs/plans/2026-10-03-0059-feat-observations-store-stage1-plan.md).

Вывод машиночитаемый и стабильный (отсортирован по метке): «метка<TAB>число»
построчно или JSON, «-» — таблицы нет. В stdout — только числа, сообщения о
сверке идут в stderr, чтобы вывод можно было сохранить и сравнить.

Не считаются contenttypes, auth.Permission и сессии: первые два заново
создаёт migrate, сессии не переносятся (см. ip_plan/services/db_transfer.py).

Usage:
  python manage.py db_counts
  python manage.py db_counts --json --output /app/data/xfer/counts.json
  python manage.py db_counts --compare /app/data/xfer/counts_source.tsv
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS

from ip_plan.services import db_transfer


class Command(BaseCommand):
    help = "Число строк по каждой переносимой модели (для сверки двух баз)"

    def add_arguments(self, parser):
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument("--json", action="store_true", help="JSON вместо «метка<TAB>число»")
        parser.add_argument("--output", help="Записать вывод ещё и в файл")
        parser.add_argument("--compare", metavar="FILE",
                            help="Сверить с ранее сохранённым выводом; при расхождении — ошибка")

    def handle(self, *args, **options):
        counts = db_transfer.count_rows(options["database"])
        text = db_transfer.format_counts(counts, as_json=options["json"])
        self.stdout.write(text, ending="")
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as f:
                f.write(text)
        if options["compare"]:
            with open(options["compare"], encoding="utf-8") as f:
                expected = db_transfer.parse_counts(f.read())
            diffs = db_transfer.compare_counts(expected, counts)
            if diffs:
                for label, want, got in diffs:
                    self.stderr.write(f"РАСХОЖДЕНИЕ {label}: в файле {want}, в базе {got}")
                raise CommandError(
                    "Число строк не совпадает: " + ", ".join(label for label, _, _ in diffs))
            self.stderr.write(f"Число строк совпадает по всем {len(counts)} моделям")
