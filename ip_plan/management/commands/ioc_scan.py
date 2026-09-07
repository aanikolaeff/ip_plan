"""CLI-вход для ip_plan/services/ioc_parser.py (см. design doc) — не трогает
NetBox и не пишет в БД, только читает PDF/DOCX/CSV из папки на диске и
печатает JSON (см. --out). Django management-команда, а не голый
argparse-скрипт —
add_arguments/handle используют тот же argparse под капотом, а команда сразу
доступна и внутри контейнера (тот же venv, куда уже ставится pdfplumber/
python-docx), без отдельного standalone-скрипта.

Usage:
  python manage.py ioc_scan /path/to/folder
  python manage.py ioc_scan /path/to/folder --recursive
  python manage.py ioc_scan /path/to/folder --per-file
  python manage.py ioc_scan /path/to/folder --out result.json
"""
import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ip_plan.services.ioc_parser import scan_folder


class Command(BaseCommand):
    help = "Извлечение IOC (IP/домен/URL/SHA256) из PDF/DOCX (депрофанированные) и CSV (чистые списки) в папке"

    def add_arguments(self, parser):
        parser.add_argument("folder", help="Путь к папке с документами (.pdf/.docx/.csv)")
        parser.add_argument(
            "--recursive", action="store_true",
            help="Сканировать вложенные папки (по умолчанию — только сама папка)",
        )
        parser.add_argument(
            "--per-file", action="store_true",
            help="Дополнительно вывести результат отдельно по каждому файлу",
        )
        parser.add_argument("--out", help="Куда сохранить JSON (по умолчанию — вывод в stdout)")

    def handle(self, *args, **options):
        folder = Path(options["folder"])
        if not folder.is_dir():
            raise CommandError(f"Не найдена папка: {folder}")

        result = scan_folder(folder, recursive=options["recursive"], per_file=options["per_file"])
        output = json.dumps(result, ensure_ascii=False, indent=2)

        if options["out"]:
            Path(options["out"]).write_text(output + "\n", encoding="utf-8")
            self.stderr.write(self.style.SUCCESS(f"Сохранено: {options['out']}"))
        else:
            self.stdout.write(output)

        # Короткая сводка в stderr — чтобы не мешала JSON на stdout, если
        # результат перенаправляют в файл через shell (`> result.json`).
        counts = {kind: len(items) for kind, items in result["indicators"].items()}
        self.stderr.write(
            f"Файлов просканировано: {result['files_scanned']}, "
            f"с индикаторами: {result['files_with_indicators']}, "
            f"найдено уникальных: {counts}"
        )
        if result["warnings"]:
            self.stderr.write(f"Предупреждений: {len(result['warnings'])}")
            for w in result["warnings"]:
                self.stderr.write(f"  {w['file']}: {w['message']}")
