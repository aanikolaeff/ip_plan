"""Выбор СУБД (U2 плана хранилища наблюдений, 03.10.2026): по умолчанию —
прежний SQLite, IP_PLAN_DB_ENGINE=postgres переключает на PostgreSQL с
параметрами из окружения; фоновый поток проверки подключения не оставляет
за собой открытого соединения с базой (на PostgreSQL у каждого потока своё)."""
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config import settings as project_settings
from ip_plan.services import connection_test


def _getter(env):
    """Подставное окружение в виде decouple.config: get(name, default=...)."""
    def get(name, default=""):
        return env.get(name, default)
    return get


class BuildDatabasesTests(SimpleTestCase):
    def test_default_is_sqlite_in_data_dir(self):
        dbs = project_settings.build_databases(_getter({}))
        default = dbs["default"]
        self.assertEqual(default["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(Path(default["NAME"]),
                         project_settings.BASE_DIR / "data" / "db.sqlite3")

    def test_empty_engine_is_sqlite(self):
        # Пустая строка из шаблона .env (CI-переменная не задана) = SQLite.
        dbs = project_settings.build_databases(_getter({"IP_PLAN_DB_ENGINE": ""}))
        self.assertEqual(dbs["default"]["ENGINE"], "django.db.backends.sqlite3")

    def test_postgres_from_env(self):
        env = {
            "IP_PLAN_DB_ENGINE": "postgres",
            "IP_PLAN_DB_HOST": "db.example",
            "IP_PLAN_DB_PORT": "6543",
            "IP_PLAN_DB_NAME": "ipp",
            "IP_PLAN_DB_USER": "ipp_user",
            "IP_PLAN_DB_PASSWORD": "s3cret",
        }
        default = project_settings.build_databases(_getter(env))["default"]
        self.assertEqual(default["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(default["HOST"], "db.example")
        self.assertEqual(default["PORT"], "6543")
        self.assertEqual(default["NAME"], "ipp")
        self.assertEqual(default["USER"], "ipp_user")
        self.assertEqual(default["PASSWORD"], "s3cret")
        self.assertIn("connect_timeout", default["OPTIONS"])

    def test_postgres_defaults_match_compose(self):
        default = project_settings.build_databases(
            _getter({"IP_PLAN_DB_ENGINE": "Postgres"}))["default"]
        self.assertEqual(default["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(default["HOST"], "postgres")
        self.assertEqual(default["PORT"], "5432")
        self.assertEqual(default["NAME"], "ip_plan")
        self.assertEqual(default["USER"], "ip_plan")

    def test_unknown_engine_fails_loudly(self):
        # Опечатка в CI-переменной не должна молча поднять web на SQLite.
        with self.assertRaises(ImproperlyConfigured):
            project_settings.build_databases(_getter({"IP_PLAN_DB_ENGINE": "mysql"}))


class ConnectionTestThreadClosesDbTests(SimpleTestCase):
    def _run_worker(self, **patches):
        with mock.patch.object(connection_test, "_finish"), \
             mock.patch.object(connection_test.connection, "close") as close:
            for name, value in patches.items():
                setattr_patch = mock.patch.object(connection_test, name, value)
                setattr_patch.start()
                self.addCleanup(setattr_patch.stop)
            connection_test._worker(1, "device", "sw-1")
        return close

    def test_closes_connection_after_success(self):
        close = self._run_worker(_run_device=mock.Mock(return_value=([], "")))
        close.assert_called_once()

    def test_closes_connection_after_failure(self):
        close = self._run_worker(_run_device=mock.Mock(side_effect=LookupError("нет")))
        close.assert_called_once()

    def test_closes_connection_after_unexpected_error(self):
        close = self._run_worker(_run_device=mock.Mock(side_effect=RuntimeError("x")))
        close.assert_called_once()
