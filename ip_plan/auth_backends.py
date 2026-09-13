"""LDAP-бэкенд, берущий настройки из БД, а не из settings.py.

Зачем: настройки FreeIPA правятся из раздела "Настройки", и они должны
применяться сразу, без перезапуска контейнера. Штатный LDAPBackend читает
AUTH_LDAP_* из django.conf.settings, то есть значения, зафиксированные при
импорте settings.py.

Как это работает (проверено по исходникам django-auth-ldap 4.8):
LDAPBackend.settings — обычное property с ленивым self._settings НА
ЭКЗЕМПЛЯРЕ, а django.contrib.auth создаёт новый экземпляр бэкенда на
каждый вызов authenticate()/get_user(). Значит кеша между запросами нет и
инвалидировать нечего: достаточно переопределить property. Это внутреннее
устройство библиотеки, а не публичный API — при её обновлении место надо
перепроверять.

Что по-прежнему берётся из settings.py: AUTH_LDAP_GLOBAL_OPTIONS (путь к
CA-сертификату и режим проверки TLS). Опции OPT_X_TLS_* в python-ldap
глобальны для процесса и применяются до первого соединения, поэтому смена
сертификата из UI невозможна в принципе — только файл и перезапуск.
"""
import logging

import ldap
from django_auth_ldap.backend import LDAPBackend
from django_auth_ldap.config import GroupOfNamesType, LDAPSearch, LDAPSettings

from .models import LdapConfig

logger = logging.getLogger(__name__)


class DbLDAPBackend(LDAPBackend):
    """LDAPBackend с настройками из модели LdapConfig."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._db_config = None
        self._db_config_loaded = False

    # ── Настройки из БД ────────────────────────────────────────────────────

    def db_config(self):
        """Одно обращение к БД на экземпляр бэкенда (то есть на запрос):
        settings трогается несколько раз за аутентификацию."""
        if not self._db_config_loaded:
            try:
                self._db_config = LdapConfig.load()
            except Exception:
                # БД может быть ещё не мигрирована (первый старт, collectstatic
                # и т.п.) — вход через LDAP тогда просто недоступен, локальный
                # работает.
                logger.exception("Не удалось прочитать настройки LDAP из БД")
                self._db_config = None
            self._db_config_loaded = True
        return self._db_config

    @property
    def settings(self):
        if self._settings is None:
            s = LDAPSettings(self.settings_prefix, self.default_settings)
            cfg = self.db_config()
            if cfg and cfg.server_list():
                # python-ldap сам перебирает серверы из списка через пробел,
                # если первый недоступен.
                s.SERVER_URI = " ".join(cfg.server_list())
                s.BIND_DN = cfg.bind_dn
                s.BIND_PASSWORD = cfg.bind_password
                s.USER_SEARCH = LDAPSearch(
                    cfg.user_search_base, ldap.SCOPE_SUBTREE, cfg.user_search_filter
                )
                if cfg.group_search_base:
                    s.GROUP_SEARCH = LDAPSearch(
                        cfg.group_search_base, ldap.SCOPE_SUBTREE, "(objectClass=groupOfNames)"
                    )
                    s.GROUP_TYPE = GroupOfNamesType(name_attr="cn")
                s.REQUIRE_GROUP = cfg.require_group or None
                s.USER_FLAGS_BY_GROUP = cfg.flags_by_group()
                s.CACHE_TIMEOUT = cfg.cache_timeout
            self._settings = s
        return self._settings

    @settings.setter
    def settings(self, value):
        # Сеттер родителя не наследуется при переопределении property —
        # определяем свой, иначе присваивание backend.settings = ... упало бы
        # с AttributeError.
        self._settings = value

    # ── Аутентификация ─────────────────────────────────────────────────────

    def authenticate_ldap_user(self, ldap_user, password):
        cfg = self.db_config()
        if cfg is None or not cfg.enabled:
            # Выключено в настройках — молча уступаем следующему бэкенду
            # (ModelBackend), не открывая соединение.
            return None
        if not cfg.server_list():
            logger.warning("Вход через LDAP включён, но не задан ни один сервер")
            return None
        return super().authenticate_ldap_user(ldap_user, password)
