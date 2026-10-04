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
                    "NETBOX_SYNC_DB_PASSWORD", "NETOBS_RO_PASSWORD",
                    "SYNC_WORKER_DB_PASSWORD"):
            self.assertRegex(env, rf"(?m)^{key}=")

    def test_ci_writes_both_env_files(self):
        ci = _read(".gitlab-ci.yml")
        self.assertIn("cat > .env <<EOF", ci)
        self.assertIn("cat > .env.db <<EOF", ci)
        env_part = ci.split("cat > .env <<EOF", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("POSTGRES_PASSWORD", env_part)
        self.assertNotIn("NETBOX_SYNC_DB_PASSWORD", env_part)
        self.assertIn("NETOBS_RO_PASSWORD=$NETOBS_RO_PASSWORD", env_part)

    def test_ci_keeps_local_admin_seeding_on_by_default(self):
        # Пустое значение decouple читает как false — засев выключался бы молча.
        self.assertIn("SEED_LOCAL_ADMIN=${SEED_LOCAL_ADMIN:-true}", _read(".gitlab-ci.yml"))

    def test_ci_refuses_empty_db_passwords_before_writing_env_db(self):
        ci = _read(".gitlab-ci.yml")
        guard = ci.index("SYNC_WORKER_DB_PASSWORD; do")
        self.assertLess(guard, ci.index("cat > .env.db <<EOF"))
        line_start = ci.rindex("for var in", 0, guard)
        for key in ("POSTGRES_PASSWORD", "IP_PLAN_DB_PASSWORD",
                    "NETBOX_SYNC_DB_PASSWORD", "NETOBS_RO_PASSWORD",
                    "SYNC_WORKER_DB_PASSWORD"):
            self.assertIn(key, ci[line_start:guard + 30])

    def test_ci_writes_sync_worker_password_to_env_db_only(self):
        # Пароль сборщика нужен bootstrap.sh (контейнер postgres); в .env,
        # который целиком уходит в web, его нет.
        ci = _read(".gitlab-ci.yml")
        env_db_part = ci.split("cat > .env.db <<EOF", 1)[1].split("EOF", 1)[0]
        self.assertIn("SYNC_WORKER_DB_PASSWORD=$SYNC_WORKER_DB_PASSWORD", env_db_part)
        env_part = ci.split("cat > .env <<EOF", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("SYNC_WORKER_DB_PASSWORD", env_part)

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

    def test_sync_worker_role(self):
        # Роль сборщика: вход и CONNECT только в базу ip_plan; права на
        # таблицы заданий — блок, общий с миграцией 0012 (test_sync_jobs_models).
        self.assertIn(': "${SYNC_WORKER_DB_PASSWORD:?', self.sh)
        self.assertIn("GRANT CONNECT ON DATABASE ip_plan TO sync_worker", self.sh)
        self.assertIn("GRANT SELECT, INSERT, UPDATE ON TABLE sync_job TO sync_worker", self.sh)
        self.assertIn("GRANT UPDATE (next_run) ON TABLE sync_schedule TO sync_worker", self.sh)

    def test_passwords_not_passed_on_command_line(self):
        # Пароли — через \getenv внутри psql, не через -v в argv (видно в ps).
        self.assertNotRegex(self.sh, r"-v\s+\w+_pw=")
        self.assertIn(r"\getenv ip_plan_pw IP_PLAN_DB_PASSWORD", self.sh)

    def test_init_wrapper_runs_bootstrap_in_subprocess(self):
        wrapper = _read("db/initdb/10-ip-plan-bootstrap.sh")
        self.assertIn("bash /opt/ip_plan_db/bootstrap.sh", wrapper)


# Корень netbox-sync в compose: «${NETBOX_SYNC_DIR:-../netbox-sync}:<куда>».
_SYNC_ROOT_MOUNT = re.compile(r"\$\{NETBOX_SYNC_DIR:-\.\./netbox-sync\}:")
# Каталоги вывода синка, которые сборщик пишет поверх кода (KTD2).
_WORKER_OUTPUT_DIRS = ("config_backups", "lldp_snapshots", "firewall_blocklists",
                       "sync_logs", "reports")


def _mounts(block):
    """Строки `- источник:назначение[:режим]` из volumes сервиса."""
    m = re.search(r"^    volumes:\n((?:      .*\n|\s*\n)+)", block, re.M)
    if not m:
        return []
    return [ln.strip()[2:].strip().strip('"') for ln in m.group(1).splitlines()
            if ln.strip().startswith("- ")]


class ComposeWorkerTests(unittest.TestCase):
    """Сервис worker — сборщик заданий netbox-sync (U10 плана
    docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md, KTD1,
    KTD2, KTD16). Доказательство самого контейнера — дымовой прогон шагов;
    здесь то, что ломается правкой текста: секреты, режимы монтирования,
    публикация /health."""

    def setUp(self):
        self.compose = _read("docker-compose.yml")
        self.worker = _service_block(self.compose, "worker")
        # Без строк-комментариев: в них .env.db упоминается словами.
        self.worker_code = "\n".join(ln for ln in self.worker.splitlines()
                                     if not ln.lstrip().startswith("#"))
        self.web = _service_block(self.compose, "web")
        self.mounts = _mounts(self.worker)

    def test_worker_only_under_its_profile(self):
        # Обычный `docker compose up -d` (и деплой из CI) сборщик не поднимает.
        self.assertRegex(self.worker, r'profiles:\s*\["worker"\]')

    def test_worker_image_built_from_netbox_sync(self):
        self.assertIn("context: ${NETBOX_SYNC_DIR:-../netbox-sync}", self.worker)
        self.assertIn("dockerfile: docker/worker.Dockerfile", self.worker)
        # Свой entrypoint образа, никаких migrate/collectstatic Django.
        self.assertNotIn("entrypoint.sh", self.worker.replace("worker-entrypoint.sh", ""))
        self.assertNotIn("manage.py", self.worker)

    def test_worker_process_shape(self):
        self.assertRegex(self.worker, r"(?m)^    init:\s*true\s*$")
        self.assertIn("restart: unless-stopped", self.worker)
        self.assertRegex(self.worker, r'PYTHONDONTWRITEBYTECODE:\s*"1"')
        self.assertRegex(self.worker, r"depends_on:\s*\n\s*postgres:\s*\n\s*condition:\s*service_healthy")

    def test_stop_grace_longer_than_step_kill_pause(self):
        # worker.py: SIGTERM шагу, пауза 15 с, SIGKILL — compose должен ждать дольше.
        m = re.search(r"stop_grace_period:\s*(\d+)s", self.worker)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 30)

    def test_health_not_published(self):
        self.assertNotRegex(self.worker, r"(?m)^    ports:")

    def test_code_mounted_read_only(self):
        self.assertIn("${NETBOX_SYNC_DIR:-../netbox-sync}:/opt/scan:ro", self.mounts)
        # conf/ — внутри кода, только на чтение: отдельного монтирования на запись нет.
        for m in self.mounts:
            if re.search(r":/opt/scan/conf(:|$)", m):
                self.assertTrue(m.endswith(":ro"), m)
        # Монтирования на запись — только каталоги вывода.
        writable = [m for m in self.mounts if not m.endswith(":ro")]
        self.assertEqual(sorted(m.rsplit("/", 1)[-1] for m in writable),
                         sorted(_WORKER_OUTPUT_DIRS))

    def test_output_dirs_writable_over_code(self):
        for name in _WORKER_OUTPUT_DIRS:
            want = f"${{NETBOX_SYNC_DIR:-../netbox-sync}}/{name}:/opt/scan/{name}"
            self.assertIn(want, self.mounts, name)
        self.assertRegex(self.worker, r"NETBOX_SYNC_REPORTS_DIR:\s*/opt/scan/reports")

    def test_worker_gets_no_foreign_secrets(self):
        # Ни .env (секреты web), ни .env.db (суперпользователь, владельцы баз).
        self.assertNotRegex(self.worker_code, r"(?m)^\s*-\s*(path:\s*)?\.env\s*$")
        self.assertNotIn(".env.db", self.worker_code)
        for key in ("POSTGRES_PASSWORD", "IP_PLAN_DB_PASSWORD", "NETBOX_SYNC_DB_PASSWORD",
                    "NETOBS_RO_PASSWORD", "LDAP_BIND_PASSWORD", "SECRET_KEY", "NETBOX_TOKEN",
                    "GUACAMOLE_JSON_SECRET_KEY"):
            self.assertNotIn(key, self.worker_code, key)
        # Свой пароль — из отдельного файла только с ним.
        self.assertIn(".env.worker", self.worker)

    def test_worker_env_file_is_ignored_by_git_and_docker(self):
        self.assertRegex(_read(".gitignore"), r"(?m)^\.env\.worker$")
        self.assertRegex(_read(".dockerignore"), r"(?m)^\.env\.worker$")

    def test_worker_addresses_for_container(self):
        # KTD16: netobs и NetBox — по адресам изнутри compose, не localhost хоста.
        self.assertRegex(self.worker, r"NETOBS_HOST:\s*postgres")
        self.assertRegex(self.worker, r'NETOBS_PORT:\s*"5432"')
        self.assertIn("NETBOX_SYNC_NETBOX_URL", self.worker)
        self.assertIn("host.docker.internal:host-gateway", self.worker)
        # Без IPv6: корпоративный DNS молчит на AAAA, и каждое имя гипервизора
        # разрешалось бы ~4 с (RedVirt шёл больше часа вместо ~7 минут).
        self.assertRegex(self.worker, r"net\.ipv6\.conf\.all\.disable_ipv6:\s*1")

    def test_web_still_does_not_mount_sync_root(self):
        for m in _mounts(self.web):
            self.assertFalse(_SYNC_ROOT_MOUNT.match(m), f"web монтирует корень netbox-sync: {m}")

    def test_env_example_describes_worker_variables(self):
        env = _read(".env.example")
        for key in ("NETBOX_SYNC_ENV", "NETBOX_SYNC_NETBOX_URL"):
            self.assertRegex(env, rf"(?m)^{key}=")
        self.assertNotRegex(env, r"(?m)^SYNC_WORKER_DB_PASSWORD=")


if __name__ == "__main__":
    unittest.main()
