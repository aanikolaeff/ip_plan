"""LDAP/FreeIPA-аутентификация (django-auth-ldap). Настройки серверов/DN —
из ldap_settings.json (без пароля); пароль bind-аккаунта — из переменной
окружения, имя которой указано в JSON ("bind_password_env"), через тот же
python-decouple, что и остальной config/settings.py (читает .env напрямую,
без необходимости реально экспортировать переменную в shell).

Импортируется через `from ip_plan.ldap_config import *` в config/settings.py.
"""
import json
from pathlib import Path

import ldap
from decouple import config
from django_auth_ldap.config import GroupOfNamesType, LDAPSearch

_cfg_path = Path(__file__).parent / "ldap_settings.json"
with open(_cfg_path, encoding="utf-8") as _f:
    _cfg = json.load(_f)

# Несколько серверов через пробел — python-ldap сам пробует по очереди, если
# первый недоступен.
AUTH_LDAP_SERVER_URI = " ".join(_cfg["servers"])

AUTH_LDAP_BIND_DN = _cfg["bind_dn"]
AUTH_LDAP_BIND_PASSWORD = config(_cfg["bind_password_env_var"], default="")

# OPT_X_TLS_* — глобальные для процесса (особенность python-ldap, не per-
# connection), должны быть выставлены до первого соединения.
AUTH_LDAP_GLOBAL_OPTIONS = {
    ldap.OPT_X_TLS_CACERTFILE: _cfg["tls_ca_cert"],
    ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_DEMAND,
    ldap.OPT_NETWORK_TIMEOUT: 15,
    ldap.OPT_TIMEOUT: 15,
}

AUTH_LDAP_USER_SEARCH = LDAPSearch(
    _cfg["user_search_base"], ldap.SCOPE_SUBTREE, _cfg["user_search_filter"]
)
AUTH_LDAP_GROUP_SEARCH = LDAPSearch(
    _cfg["group_search_base"], ldap.SCOPE_SUBTREE, "(objectClass=groupOfNames)"
)
AUTH_LDAP_GROUP_TYPE = GroupOfNamesType(name_attr="cn")

# Вход разрешён только членам этой группы FreeIPA.
AUTH_LDAP_REQUIRE_GROUP = _cfg["require_group"]

AUTH_LDAP_USER_ATTR_MAP = {
    "first_name": "givenName",
    "last_name": "sn",
    "email": "mail",
}

AUTH_LDAP_ALWAYS_UPDATE_USER = True
AUTH_LDAP_CACHE_TIMEOUT = _cfg["cache_timeout"]
