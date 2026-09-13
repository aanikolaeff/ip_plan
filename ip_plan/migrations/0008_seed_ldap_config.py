"""Перенос настроек LDAP из ip_plan/ldap_settings.json в БД.

Разовый сид: после него файл больше не читается (см. auth_backends.py), а
правка идёт через раздел "Настройки". Смысл миграции — чтобы прод и любой
существующий стенд после выкатки продолжили работать ровно как раньше, без
ручного переноса девяти полей и пароля.

Пароль bind берётся из той же переменной окружения, что и раньше
(LDAP_BIND_PASSWORD по умолчанию, имя задано в самом JSON) и кладётся в БД
уже зашифрованным.
"""
import json
from pathlib import Path

from django.db import migrations


def seed(apps, schema_editor):
    LdapConfig = apps.get_model("ip_plan", "LdapConfig")
    if LdapConfig.objects.exists():
        return

    # Модель из apps.get_model() — историческая, без наших методов
    # (set_bind_password), поэтому шифруем напрямую.
    from decouple import config as env
    from ip_plan.services import secretbox

    path = Path(__file__).resolve().parent.parent / "ldap_settings.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        # Нет файла — заводим выключённую пустую конфигурацию. Приложение
        # останется рабочим через локальный вход, а настроить LDAP можно
        # будет в интерфейсе.
        LdapConfig.objects.create(
            pk=1, enabled=False, servers="", user_search_base="",
            updated_by="migration 0008 (файл ldap_settings.json не найден)",
        )
        return

    password = env(cfg.get("bind_password_env_var", "LDAP_BIND_PASSWORD"), default="")
    LdapConfig.objects.create(
        pk=1,
        enabled=True,
        servers="\n".join(cfg.get("servers") or []),
        bind_dn=cfg.get("bind_dn", ""),
        bind_password_encrypted=secretbox.encrypt(password),
        user_search_base=cfg.get("user_search_base", ""),
        user_search_filter=cfg.get("user_search_filter", "(uid=%(user)s)"),
        group_search_base=cfg.get("group_search_base", ""),
        require_group=cfg.get("require_group", ""),
        staff_group=cfg.get("staff_group", ""),
        superuser_group=cfg.get("superuser_group", ""),
        cache_timeout=cfg.get("cache_timeout", 3600),
        updated_by="migration 0008 (перенос из ldap_settings.json)",
    )


def unseed(apps, schema_editor):
    apps.get_model("ip_plan", "LdapConfig").objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ip_plan', '0007_ldapconfig'),
    ]

    operations = [migrations.RunPython(seed, unseed)]
