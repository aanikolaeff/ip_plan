"""Счётчик строк по моделям (U3 плана хранилища наблюдений, 03.10.2026):
db_counts печатает машиночитаемые числа по каждой переносимой модели, два
вывода для одинаковых данных совпадают, а сверка с сохранённым файлом
называет модель с расхождением."""
import io
import json
import os
import tempfile

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from ip_plan.models import SettingsAudit


def _counts(*args):
    out = io.StringIO()
    call_command("db_counts", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def _parse_tsv(text):
    rows = {}
    for line in text.splitlines():
        label, count = line.split("\t")
        rows[label] = count
    return rows


class DbCountsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_lists_auth_and_every_ip_plan_model(self):
        user = User.objects.create_user("u1")
        user.groups.add(Group.objects.create(name="g1"))
        rows = _parse_tsv(_counts())
        self.assertEqual(rows["auth.User"], "1")
        self.assertEqual(rows["auth.Group"], "1")
        self.assertEqual(rows["auth.User_groups"], "1")
        for label in ("ip_plan.RegulatorDocument", "ip_plan.RegulatorEntry",
                      "ip_plan.FirewallObjectName", "ip_plan.LocalAccount",
                      "ip_plan.SettingsAudit", "ip_plan.LdapConfig",
                      "ip_plan.ConnectionTest", "ip_plan.MapLayout"):
            self.assertIn(label, rows)
        # Сид миграции 0003 — две строки имён объектов xFW.
        self.assertEqual(rows["ip_plan.FirewallObjectName"], "2")

    def test_skips_models_that_are_not_transferred(self):
        # contenttypes/permissions создаёт migrate, сессии не переносятся —
        # их числа в двух базах законно расходятся, сверять их нельзя.
        rows = _parse_tsv(_counts())
        for label in ("contenttypes.ContentType", "auth.Permission", "sessions.Session"):
            self.assertNotIn(label, rows)

    def test_json_output_is_same_numbers(self):
        SettingsAudit.objects.create(username="u", scope="s", action="create")
        as_json = json.loads(_counts("--json"))
        as_tsv = {k: int(v) for k, v in _parse_tsv(_counts()).items()}
        self.assertEqual(as_json, as_tsv)

    def test_same_data_compares_equal(self):
        path = os.path.join(self.tmp.name, "counts.tsv")
        first = _counts("--output", path)
        # Второй вывод по тем же данным — те же байты, и сверка проходит.
        self.assertEqual(_counts(), first)
        err = io.StringIO()
        call_command("db_counts", "--compare", path, stdout=io.StringIO(), stderr=err)
        self.assertIn("совпадает", err.getvalue())

    def test_compare_names_model_with_difference(self):
        path = os.path.join(self.tmp.name, "counts.json")
        _counts("--json", "--output", path)
        SettingsAudit.objects.create(username="u", scope="s", action="create")
        err = io.StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("db_counts", "--compare", path, stdout=io.StringIO(), stderr=err)
        self.assertIn("ip_plan.SettingsAudit", str(ctx.exception))
        self.assertIn("ip_plan.SettingsAudit", err.getvalue())
        self.assertNotIn("ip_plan.MapLayout", str(ctx.exception))
