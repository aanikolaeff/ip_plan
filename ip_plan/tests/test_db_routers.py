"""Подключение к хранилищу наблюдений netobs (U11 плана хранилища
наблюдений, KTD11): алиас netobs объявляется только при заданных
NETOBS_RO_*, с короткими таймаутами; маршрутизатор не пускает в netobs ни
миграции, ни запись — схемой владеет netbox-sync, роль ip_plan_ro только
читает."""
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase

from config import settings as project_settings
from ip_plan.db_routers import NetobsRouter


def _getter(env):
    def get(name, default=""):
        return env.get(name, default)
    return get


_NETOBS_ENV = {
    "NETOBS_RO_HOST": "db.example",
    "NETOBS_RO_PORT": "6543",
    "NETOBS_RO_NAME": "netobs",
    "NETOBS_RO_USER": "ip_plan_ro",
    "NETOBS_RO_PASSWORD": "s3cret",
}


class NetobsDatabaseSettingsTests(SimpleTestCase):
    def test_netobs_alias_from_env(self):
        netobs = project_settings.build_databases(_getter(_NETOBS_ENV))["netobs"]
        self.assertEqual(netobs["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(netobs["HOST"], "db.example")
        self.assertEqual(netobs["PORT"], "6543")
        self.assertEqual(netobs["NAME"], "netobs")
        self.assertEqual(netobs["USER"], "ip_plan_ro")
        self.assertEqual(netobs["PASSWORD"], "s3cret")
        self.assertEqual(netobs["OPTIONS"]["connect_timeout"], 3)
        self.assertIn("statement_timeout=5000", netobs["OPTIONS"]["options"])

    def test_netobs_alias_alongside_postgres_default(self):
        env = dict(_NETOBS_ENV, IP_PLAN_DB_ENGINE="postgres")
        dbs = project_settings.build_databases(_getter(env))
        self.assertEqual(set(dbs), {"default", "netobs"})
        self.assertEqual(dbs["default"]["NAME"], "ip_plan")

    def test_no_netobs_alias_without_env(self):
        # Нет NETOBS_RO_* — алиаса нет, страница поиска пишет «не настроено».
        self.assertNotIn("netobs", project_settings.build_databases(_getter({})))
        partial = {"NETOBS_RO_HOST": "postgres", "NETOBS_RO_NAME": ""}
        self.assertNotIn("netobs", project_settings.build_databases(_getter(partial)))

    def test_router_is_installed(self):
        self.assertIn("ip_plan.db_routers.NetobsRouter", settings.DATABASE_ROUTERS)


class NetobsRouterTests(SimpleTestCase):
    def setUp(self):
        self.router = NetobsRouter()

    def test_migrations_only_on_default(self):
        self.assertFalse(self.router.allow_migrate("netobs", "ip_plan"))
        self.assertFalse(self.router.allow_migrate("netobs", "auth", model_name="user"))
        self.assertTrue(self.router.allow_migrate("default", "ip_plan"))

    def test_write_never_goes_to_netobs(self):
        self.assertNotEqual(self.router.db_for_write(User), "netobs")
        # Даже объект, прочитанный через .using("netobs"), сохраняется не туда:
        # без явного ответа роутера Django взял бы базу из instance._state.db.
        instance = SimpleNamespace(_state=SimpleNamespace(db="netobs"))
        self.assertEqual(self.router.db_for_write(User, instance=instance), "default")
