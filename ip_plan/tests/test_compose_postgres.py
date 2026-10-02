"""PostgreSQL в compose: статические проверки конфигурации (этап 1 хранилища
наблюдений, U1 плана docs/plans/2026-10-03-0059-feat-observations-store-stage1-plan.md).

Основное доказательство U1 — smoke-проверка на живом compose (роли, права,
публикация порта). Здесь — только то, что легко сломать правкой текста и
не заметить до прода: секреты СУБД не должны попасть в контейнер web, порт —
на внешний интерфейс, а bootstrap — потерять default privileges.

Без Django и без PyYAML (его нет в requirements): проверки по тексту файлов,
запуск и на хосте (`python3 -m unittest ip_plan.tests.test_compose_postgres`),
и в контейнере. Если файлов рядом нет (образ собран без них) — skip.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(name):
    path = ROOT / name
    if not path.exists():
        raise unittest.SkipTest(f"{name} не найден рядом с кодом")
    return path.read_text(encoding="utf-8")


def _service_block(compose, name):
    """Текст сервиса верхнего уровня `services:` — до следующего ключа с тем же отступом."""
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", compose, re.S | re.M)
    if not m:
        raise AssertionError(f"сервис {name} не найден в docker-compose.yml")
    return m.group(1)


class ComposePostgresTests(unittest.TestCase):
    def setUp(self):
        self.compose = _read("docker-compose.yml")
        self.web = _service_block(self.compose, "web")
        self.pg = _service_block(self.compose, "postgres")

    def test_postgres_service_shape(self):
        self.assertRegex(self.pg, r"image:\s*postgres:18\b")
        # Образ 18 отвергает том на .../data — только /var/lib/postgresql.
        self.assertRegex(self.pg, r"-\s*pg_data:/var/lib/postgresql\s*$|-\s*pg_data:/var/lib/postgresql\n")
        self.assertNotIn("/var/lib/postgresql/data", self.pg)
        self.assertIn("pg_isready", self.pg)
        self.assertIn("restart: unless-stopped", self.pg)

    def test_port_published_on_loopback_only(self):
        self.assertIn('"127.0.0.1:${POSTGRES_HOST_PORT:-5433}:5432"', self.pg)
        ports = re.findall(r'^\s*-\s*"([^"]+:5432)"', self.pg, re.M)
        self.assertTrue(ports)
        for p in ports:
            self.assertTrue(p.startswith("127.0.0.1:"), p)

    def test_web_waits_for_healthy_postgres(self):
        self.assertRegex(self.web, r"depends_on:\s*\n\s*postgres:\s*\n\s*condition:\s*service_healthy")

    def test_db_secrets_only_in_postgres_service(self):
        # .env.db (пароль суперпользователя и владельца netobs) — только у postgres.
        self.assertIn(".env.db", self.pg)
        self.assertNotIn(".env.db", self.web)
        self.assertNotIn("POSTGRES_PASSWORD", self.web)
        self.assertNotIn("NETBOX_SYNC_DB_PASSWORD", self.web)

    def test_sqlite_volume_kept_for_migration_and_rollback(self):
        self.assertIn("sqlite_data:/app/data", self.web)
        self.assertRegex(self.compose, r"(?m)^volumes:\n(?:.*\n)*?  sqlite_data:")
        self.assertRegex(self.compose, r"(?m)^volumes:\n(?:.*\n)*?  pg_data:")


class EnvExamplesTests(unittest.TestCase):
    def test_web_env_has_no_db_superuser_or_owner_secret(self):
        env = _read(".env.example")
        self.assertNotRegex(env, r"(?m)^POSTGRES_PASSWORD=")
        self.assertNotRegex(env, r"(?m)^NETBOX_SYNC_DB_PASSWORD=")
        for key in ("IP_PLAN_DB_HOST", "IP_PLAN_DB_PASSWORD", "NETOBS_RO_USER", "NETOBS_RO_PASSWORD"):
            self.assertRegex(env, rf"(?m)^{key}=")

    def test_db_env_example_lists_all_bootstrap_passwords(self):
        env = _read(".env.db.example")
        for key in ("POSTGRES_PASSWORD", "IP_PLAN_DB_PASSWORD",
                    "NETBOX_SYNC_DB_PASSWORD", "NETOBS_RO_PASSWORD"):
            self.assertRegex(env, rf"(?m)^{key}=")

    def test_ci_writes_both_env_files(self):
        ci = _read(".gitlab-ci.yml")
        self.assertIn("cat > .env <<EOF", ci)
        self.assertIn("cat > .env.db <<EOF", ci)
        env_part = ci.split("cat > .env <<EOF", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("POSTGRES_PASSWORD", env_part)
        self.assertNotIn("NETBOX_SYNC_DB_PASSWORD", env_part)
        self.assertIn("NETOBS_RO_PASSWORD=$NETOBS_RO_PASSWORD", env_part)

    def test_env_db_never_committed_or_baked_into_image(self):
        self.assertRegex(_read(".gitignore"), r"(?m)^\.env\.db$")
        self.assertRegex(_read(".dockerignore"), r"(?m)^\.env\.db$")


class BootstrapScriptTests(unittest.TestCase):
    def setUp(self):
        self.sh = _read("db/bootstrap.sh")

    def test_roles_and_databases(self):
        self.assertIn("CREATE DATABASE ip_plan OWNER ip_plan", self.sh)
        self.assertIn("CREATE DATABASE netobs OWNER netbox_sync", self.sh)
        self.assertRegex(self.sh, r"ALTER ROLE ip_plan\s+LOGIN CREATEDB")

    def test_read_only_role_privileges(self):
        self.assertIn("REVOKE ALL ON DATABASE netobs  FROM PUBLIC", self.sh)
        self.assertIn("GRANT CONNECT ON DATABASE netobs TO ip_plan_ro", self.sh)
        self.assertRegex(self.sh, r"ALTER DEFAULT PRIVILEGES FOR ROLE netbox_sync IN SCHEMA public\s+"
                                  r"GRANT SELECT ON TABLES TO ip_plan_ro")
        self.assertIn("ALTER ROLE ip_plan_ro SET default_transaction_read_only = on", self.sh)

    def test_passwords_not_passed_on_command_line(self):
        # Пароли — через \getenv внутри psql, не через -v в argv (видно в ps).
        self.assertNotRegex(self.sh, r"-v\s+\w+_pw=")
        self.assertIn(r"\getenv ip_plan_pw IP_PLAN_DB_PASSWORD", self.sh)

    def test_init_wrapper_runs_bootstrap_in_subprocess(self):
        wrapper = _read("db/initdb/10-ip-plan-bootstrap.sh")
        self.assertIn("bash /opt/ip_plan_db/bootstrap.sh", wrapper)


if __name__ == "__main__":
    unittest.main()
