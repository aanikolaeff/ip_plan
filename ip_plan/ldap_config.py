"""Глобальные опции LDAP-соединения — то немногое, что осталось в settings.

Сами параметры подключения (серверы, bind DN и пароль, базы поиска,
группы) переехали в БД, в модель LdapConfig: их правят из раздела
"Настройки", и они применяются со следующего входа без перезапуска
контейнера (см. ip_plan/auth_backends.py). Здесь остаётся только то, что
из UI менять физически нельзя.

TLS: опции OPT_X_TLS_* в python-ldap глобальны для процесса и должны быть
выставлены ДО первого соединения — библиотека применяет их один раз при
инициализации. Поэтому CA-сертификат задаётся файлом (или переменной
окружения LDAP_TLS_CA_CERT) и меняется только с перезапуском контейнера.

Импортируется через `from ip_plan.ldap_config import *` в config/settings.py.
"""
import json
from pathlib import Path

import ldap
from decouple import config

# Файл остался как сид для миграции 0008 (первичный перенос настроек в БД)
# и как источник пути к сертификату. Отсутствие файла больше не ошибка:
# приложение поднимется, а LDAP настраивается в интерфейсе.
_cfg = {}
_cfg_path = Path(__file__).parent / "ldap_settings.json"
if _cfg_path.exists():
    try:
        with open(_cfg_path, encoding="utf-8") as _f:
            _cfg = json.load(_f)
    except (OSError, json.JSONDecodeError):
        _cfg = {}

LDAP_TLS_CA_CERT = config(
    "LDAP_TLS_CA_CERT",
    default=_cfg.get("tls_ca_cert", str(Path(__file__).parent / "certs" / "ca.crt")),
)

AUTH_LDAP_GLOBAL_OPTIONS = {
    ldap.OPT_X_TLS_CACERTFILE: LDAP_TLS_CA_CERT,
    ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_DEMAND,
    # Без таймаутов недоступный сервер вешает воркер gunicorn до его
    # собственного таймаута. 15 секунд на каждый из серверов.
    ldap.OPT_NETWORK_TIMEOUT: 15,
    ldap.OPT_TIMEOUT: 15,
}

# Статическое поведение, не зависящее от конкретного каталога: какие
# атрибуты переносить в Django-пользователя и обновлять ли их при каждом
# входе. Из UI это менять незачем.
AUTH_LDAP_USER_ATTR_MAP = {
    "first_name": "givenName",
    "last_name": "sn",
    "email": "mail",
}
AUTH_LDAP_ALWAYS_UPDATE_USER = True
