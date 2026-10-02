"""Перенос данных SQLite ↔ PostgreSQL (U3 плана хранилища наблюдений,
03.10.2026) на уровне management-команды db_transfer, которую вызывает
scripts/sqlite_to_postgres.sh.

Перенос моделируется в одной тестовой базе: данные «источника» выгружаются
(dump), база очищается до состояния «только что после migrate» (остаются
лишь строки, засеянные миграциями 0003 и 0008), затем загружается выгрузка
(load). Тот же набор прогоняется и на SQLite, и на PostgreSQL
(IP_PLAN_DB_ENGINE=postgres manage.py test) — последовательности первичных
ключей проверяются только там по-настоящему.
"""
import io
import json
import os
import tempfile
from datetime import date

from cryptography.fernet import Fernet
from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase
from django.utils import timezone

from ip_plan.models import (
    ConnectionTest, FirewallObjectName, LdapConfig, LocalAccount, MapLayout,
    RegulatorDocument, RegulatorEntry, SettingsAudit,
)

SQLITE_ONLY = connection.vendor != "sqlite"


def _run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("db_transfer", *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


class _TransferTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Состояние «после migrate» тестовой базы — ровно то, что засеяли
        # миграции; к нему возвращаемся, изображая пустую целевую базу.
        self.seed_names = dict(FirewallObjectName.objects.values_list("kind", "current_name"))
        self.seed_ldap = LdapConfig.objects.filter(pk=1).values().first()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def wipe_to_seed(self):
        RegulatorEntry.objects.all().delete()
        for model in (RegulatorDocument, LocalAccount, MapLayout, SettingsAudit,
                      ConnectionTest, FirewallObjectName, LdapConfig):
            model.objects.all().delete()
        User.objects.all().delete()
        Group.objects.all().delete()
        for kind, name in self.seed_names.items():
            FirewallObjectName.objects.create(kind=kind, current_name=name)
        if self.seed_ldap:
            LdapConfig.objects.create(**self.seed_ldap)


class RoundTripTests(_TransferTestBase):
    def make_source(self):
        local = User.objects.create_user("localadm", password="pw", is_staff=True)
        mirror = User.objects.create_user("ivanov")
        group = Group.objects.create(name="netops")
        mirror.groups.add(group)
        perm = Permission.objects.get(codename="view_user")
        group.permissions.add(perm)
        local.user_permissions.add(perm)
        LocalAccount.objects.create(user=local, must_change_password=False, created_by="test")
        MapLayout.objects.create(user=mirror, site="mdc", positions={"d1": [1, 2]}, expanded=["g1"])
        doc = RegulatorDocument.objects.create(reference_no="12-34", document_date=date(2026, 9, 1),
                                               note="примечание")
        RegulatorEntry.objects.create(document=doc, kind="ip", value="203.0.113.5")
        FirewallObjectName.objects.filter(kind="ip").update(current_name="BLOCK_BY_IP_7")
        FirewallObjectName.objects.filter(kind="fqdn").update(current_name="BBDNS_4")
        cfg = LdapConfig.load()
        cfg.servers = "ldaps://ipa.example.local:636"
        cfg.bind_dn = "uid=svc,cn=users"
        cfg.user_search_base = "cn=users"
        cfg.set_bind_password("bind-secret")
        cfg.updated_by = "ivanov"
        cfg.save()
        SettingsAudit.objects.create(username="ivanov", scope="devices", target="sw-1",
                                     action="update", details="пароль изменён")
        ConnectionTest.objects.create(target_kind="device", target_key="sw-1", status="ok",
                                      steps=[{"name": "ssh", "status": "ok"}],
                                      finished_at=timezone.now())

    def bump_pks(self, dump_path, by=1000):
        """Сдвигает первичные ключи в выгрузке так, чтобы они оказались
        дальше текущей позиции последовательностей: без сброса последних
        следующая вставка получила бы ключ меньше загруженных (или дубль)."""
        with open(dump_path, encoding="utf-8") as f:
            objects = json.load(f)
        for obj in objects:
            if obj["model"].startswith("ip_plan.") and obj["model"] != "ip_plan.ldapconfig":
                obj["pk"] += by
            if obj["model"] == "ip_plan.regulatorentry":
                obj["fields"]["document"] += by
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(objects, f)
        return objects

    def transfer(self):
        self.make_source()
        dump = self.path("dump.json")
        counts = self.path("counts.tsv")
        _run("dump", "--output", dump)
        call_command("db_counts", "--output", counts, stdout=io.StringIO())
        self.source_ldap = LdapConfig.objects.values(
            "servers", "bind_dn", "user_search_base", "updated_by").get(pk=1)
        objects = self.bump_pks(dump)
        self.wipe_to_seed()
        _run("load", dump)
        return dump, counts, objects

    def test_counts_match_after_transfer(self):
        _, counts, _ = self.transfer()
        err = io.StringIO()
        call_command("db_counts", "--compare", counts, stdout=io.StringIO(), stderr=err)
        self.assertIn("совпадает", err.getvalue())

    def test_firewall_names_and_ldap_are_source_not_seed(self):
        self.transfer()
        self.assertEqual(dict(FirewallObjectName.objects.values_list("kind", "current_name")),
                         {"ip": "BLOCK_BY_IP_7", "fqdn": "BBDNS_4"})
        cfg = LdapConfig.objects.get(pk=1)
        self.assertEqual(LdapConfig.objects.count(), 1)
        got = {k: getattr(cfg, k) for k in self.source_ldap}
        self.assertEqual(got, self.source_ldap)

    def test_ldap_password_decrypts_after_transfer(self):
        self.transfer()
        self.assertEqual(LdapConfig.objects.get(pk=1).bind_password, "bind-secret")
        self.assertIn("пароль LDAP", _run("verify"))

    def test_users_and_relations_survive(self):
        self.transfer()
        local = User.objects.get(username="localadm")
        self.assertTrue(local.check_password("pw"))
        self.assertTrue(LocalAccount.objects.filter(user=local).exists())
        mirror = User.objects.get(username="ivanov")
        self.assertEqual(list(mirror.groups.values_list("name", flat=True)), ["netops"])
        self.assertTrue(MapLayout.objects.filter(user=mirror, site="mdc").exists())
        self.assertEqual(RegulatorEntry.objects.get().document.reference_no, "12-34")

    def test_new_rows_do_not_collide_with_loaded_keys(self):
        _, _, objects = self.transfer()
        max_pk = {}
        for obj in objects:
            if obj.get("pk") is not None:
                max_pk[obj["model"]] = max(max_pk.get(obj["model"], 0), obj["pk"])
        doc = RegulatorDocument.objects.order_by("pk").first()
        user = User.objects.create_user("after-switch")
        creators = {
            "ip_plan.regulatordocument": lambda: RegulatorDocument.objects.create(
                reference_no="new", document_date=date(2026, 10, 3)),
            "ip_plan.regulatorentry": lambda: RegulatorEntry.objects.create(
                document=doc, kind="fqdn", value="example.org"),
            "ip_plan.firewallobjectname": lambda: FirewallObjectName.objects.create(
                kind="url", current_name="X_1"),
            "ip_plan.localaccount": lambda: LocalAccount.objects.create(user=user),
            "ip_plan.settingsaudit": lambda: SettingsAudit.objects.create(
                username="u", scope="s", action="create"),
            "ip_plan.ldapconfig": lambda: LdapConfig.objects.create(
                servers="", user_search_base=""),
            "ip_plan.connectiontest": lambda: ConnectionTest.objects.create(
                target_kind="device", target_key="sw-2", status="running"),
            "ip_plan.maplayout": lambda: MapLayout.objects.create(user=user, site="rdc"),
            "auth.group": lambda: Group.objects.create(name="after-switch"),
        }
        for label, create in creators.items():
            with self.subTest(model=label), transaction.atomic():
                obj = create()
                if label in max_pk:
                    self.assertGreater(obj.pk, max_pk[label])
        user.groups.add(Group.objects.get(name="after-switch"))

    def test_verify_passes_after_transfer(self):
        self.transfer()
        out = _run("verify")
        self.assertIn("OK", out)


class TargetMustBeEmptyTests(_TransferTestBase):
    def test_seed_only_target_is_accepted(self):
        self.assertIn("OK", _run("check-target"))

    def test_target_without_seed_rows_is_accepted(self):
        FirewallObjectName.objects.all().delete()
        LdapConfig.objects.all().delete()
        self.assertIn("OK", _run("check-target"))

    def test_extra_admin_refuses_check_and_load(self):
        dump = self.path("dump.json")
        _run("dump", "--output", dump)
        User.objects.create_user("admin", password="admin")
        with self.assertRaises(CommandError) as ctx:
            _run("check-target")
        self.assertIn("auth.User", str(ctx.exception))
        with self.assertRaises(CommandError) as ctx:
            _run("load", dump)
        self.assertIn("auth.User", str(ctx.exception))
        # Засеянные строки не удалены: удаление и загрузка — одна транзакция,
        # и до них дело не дошло.
        self.assertEqual(dict(FirewallObjectName.objects.values_list("kind", "current_name")),
                         self.seed_names)

    def test_edited_seed_row_is_not_seed(self):
        FirewallObjectName.objects.filter(kind="ip").update(current_name="BLOCK_BY_IP_9")
        with self.assertRaises(CommandError) as ctx:
            _run("check-target")
        self.assertIn("ip_plan.FirewallObjectName", str(ctx.exception))


class SourceCheckTests(_TransferTestBase):
    def test_clean_source_passes(self):
        self.assertIn("OK", _run("check-source"))

    def test_source_newer_than_code_stops(self):
        MigrationRecorder(connection).record_applied("ip_plan", "9999_future")
        with self.assertRaises(CommandError) as ctx:
            _run("check-source")
        self.assertIn("ip_plan.9999_future", str(ctx.exception))

    def test_undecryptable_ldap_password_stops(self):
        foreign = Fernet(Fernet.generate_key()).encrypt(b"x").decode("ascii")
        LdapConfig.load()
        LdapConfig.objects.filter(pk=1).update(bind_password_encrypted=foreign)
        for action in ("check-source", "verify"):
            with self.subTest(action=action), self.assertRaises(CommandError) as ctx:
                _run(action)
            self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_orphan_local_account_and_layout_stop_verify(self):
        # Ограничения внешних ключей отложенные (проверка при COMMIT), а
        # TestCase откатывает транзакцию — так получаются «висячие» строки,
        # какие мог оставить старый SQLite. Перед концом теста их убираем:
        # TestCase сам проверяет ограничения перед откатом.
        LocalAccount.objects.create(user_id=999999)
        MapLayout.objects.create(user_id=999998, site="mdc")
        self.addCleanup(LocalAccount.objects.filter(user_id=999999).delete)
        self.addCleanup(MapLayout.objects.filter(user_id=999998).delete)
        with self.assertRaises(CommandError) as ctx:
            _run("verify")
        self.assertIn("ip_plan.LocalAccount.user", str(ctx.exception))
        self.assertIn("ip_plan.MapLayout.user", str(ctx.exception))

    def _set_raw(self, table, column, value, where="1=1"):
        with connection.cursor() as cur:
            cur.execute(f'UPDATE "{table}" SET "{column}" = %s WHERE {where}', [value])

    def test_string_longer_than_max_length_stops(self):
        if SQLITE_ONLY:
            self.skipTest("SQLite-специфично: PostgreSQL такую строку не примет")
        self._set_raw("ip_plan_firewallobjectname", "current_name", "N" * 101, "kind = 'ip'")
        with self.assertRaises(CommandError) as ctx:
            _run("check-source")
        msg = str(ctx.exception)
        self.assertIn("ip_plan.FirewallObjectName.current_name", msg)
        self.assertIn("max_length 100", msg)
        # Значение (оно может быть секретом) в сообщение не попадает.
        self.assertNotIn("N" * 20, msg)

    def test_nul_character_stops(self):
        if SQLITE_ONLY:
            self.skipTest("SQLite-специфично: PostgreSQL NUL в тексте не примет")
        SettingsAudit.objects.create(username="u", scope="s", action="create", details="a\x00b")
        with self.assertRaises(CommandError) as ctx:
            _run("check-source")
        self.assertIn("ip_plan.SettingsAudit.details", str(ctx.exception))
        self.assertIn("NUL", str(ctx.exception))

    def test_wrong_type_stops(self):
        if SQLITE_ONLY:
            self.skipTest("SQLite-специфично: в PostgreSQL типы проверяет сама СУБД")
        LdapConfig.load()
        self._set_raw("ip_plan_ldapconfig", "cache_timeout", "час", "id = 1")
        RegulatorDocument.objects.create(reference_no="1", document_date=date(2026, 1, 1))
        self._set_raw("ip_plan_regulatordocument", "document_date", "вчера")
        with self.assertRaises(CommandError) as ctx:
            _run("check-source")
        msg = str(ctx.exception)
        self.assertIn("ip_plan.LdapConfig.cache_timeout", msg)
        self.assertIn("ip_plan.RegulatorDocument.document_date", msg)
