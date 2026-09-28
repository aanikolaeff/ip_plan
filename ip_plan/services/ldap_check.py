"""Проверка настроек LDAP до их сохранения.

Нужна потому, что цена ошибки высокая: неверный bind DN или база поиска —
и вход через FreeIPA перестаёт работать для всех сразу. Поэтому форма
настроек не сохраняется, пока bind не прошёл (обойти можно только явной
галочкой), и есть отдельная проверка "найдётся ли конкретный человек" —
она ловит самый частый случай, когда bind проходит, а логин нет.

Работаем напрямую через python-ldap, а не через django_auth_ldap: проверка
не должна ничего создавать в базе (django-auth-ldap на успешном входе
завёл бы зеркало пользователя), и должна работать со значениями из формы,
которые ещё нигде не сохранены.
"""
import logging

import ldap
# ldap.filter — отдельный подмодуль: сам по себе «import ldap» его не тянет,
# и обращение к ldap.filter.escape_filter_chars падало с AttributeError.
import ldap.filter
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 10


class LdapCheckError(Exception):
    """Текст пригоден для показа пользователю."""


def _connect(uri):
    # Те же глобальные TLS-опции, что и у боевого бэкенда: иначе проверка
    # соединялась бы на других условиях, чем реальный вход, и её результат
    # ничего не значил бы.
    #
    # OPT_X_TLS_NEWCTX здесь сознательно НЕ вызывается. Опция пересоздаёт
    # TLS-контекст соединения из опций, выставленных на самом соединении, а
    # путь к CA у нас глобальный (AUTH_LDAP_GLOBAL_OPTIONS) — в новом
    # контексте его не оказывается, и bind к FreeIPA падает с SERVER_DOWN
    # «certificate verify failed», хотя реальный вход в это же время
    # работает. Именно из-за этого проверка раньше всегда отвечала «ни один
    # сервер не отвечает». django_auth_ldap NEWCTX тоже не вызывает: он
    # применяет глобальные опции один раз за процесс и полагается на то,
    # что соединение наследует контекст при initialize.
    for option, value in (settings.AUTH_LDAP_GLOBAL_OPTIONS or {}).items():
        try:
            ldap.set_option(option, value)
        except ldap.LDAPError:
            logger.warning("Не удалось выставить глобальную опцию LDAP %s", option)
    conn = ldap.initialize(uri)
    conn.set_option(ldap.OPT_NETWORK_TIMEOUT, TIMEOUT)
    conn.set_option(ldap.OPT_TIMEOUT, TIMEOUT)
    return conn


def _describe(e):
    if isinstance(e, ldap.INVALID_CREDENTIALS):
        return "неверный DN или пароль"
    if isinstance(e, ldap.SERVER_DOWN):
        return "сервер недоступен"
    if isinstance(e, ldap.TIMEOUT):
        return f"таймаут ({TIMEOUT}с)"
    args = e.args[0] if e.args and isinstance(e.args[0], dict) else {}
    return args.get("desc") or args.get("info") or str(e)


def test_bind(servers, bind_dn, bind_password):
    """Проверка сервисной учётки. Возвращает список строк-результатов по
    каждому серверу, чтобы было видно, какой именно не отвечает."""
    if not servers:
        raise LdapCheckError("Не задан ни один сервер")

    results, ok_any = [], False
    for uri in servers:
        conn = None
        try:
            conn = _connect(uri)
            if bind_dn:
                conn.simple_bind_s(bind_dn, bind_password)
            else:
                conn.simple_bind_s()
            results.append(f"{uri}: подключение и bind успешны")
            ok_any = True
        except ldap.LDAPError as e:
            results.append(f"{uri}: {_describe(e)}")
        finally:
            if conn is not None:
                try:
                    conn.unbind_s()
                except ldap.LDAPError:
                    pass
    if not ok_any:
        raise LdapCheckError("Ни один сервер не отвечает:\n" + "\n".join(results))
    return results


def find_user(servers, bind_dn, bind_password, search_base, search_filter, username):
    """DN пользователя или None. Используется и проверкой входа, и запретом
    заводить локальную учётку с именем, которое уже есть в каталоге."""
    if not servers or not search_base:
        return None
    query = search_filter.replace("%(user)s", ldap.filter.escape_filter_chars(username))
    for uri in servers:
        conn = None
        try:
            conn = _connect(uri)
            conn.simple_bind_s(bind_dn, bind_password) if bind_dn else conn.simple_bind_s()
            found = conn.search_s(search_base, ldap.SCOPE_SUBTREE, query, ["dn"])
            for dn, attrs in found:
                if dn:
                    return dn
            return None
        except ldap.LDAPError as e:
            logger.info("Поиск пользователя на %s не удался: %s", uri, _describe(e))
        finally:
            if conn is not None:
                try:
                    conn.unbind_s()
                except ldap.LDAPError:
                    pass
    raise LdapCheckError("Ни один сервер не ответил на поисковый запрос")


def test_user(cfg_values, username, password):
    """Полная имитация входа: найти пользователя, забиндиться его паролем,
    проверить членство в require_group. Ничего не пишет в базу."""
    servers = cfg_values["servers"]
    dn = find_user(servers, cfg_values["bind_dn"], cfg_values["bind_password"],
                   cfg_values["user_search_base"], cfg_values["user_search_filter"], username)
    if not dn:
        raise LdapCheckError(
            f"Пользователь «{username}» не найден базой «{cfg_values['user_search_base']}» "
            f"и фильтром «{cfg_values['user_search_filter']}»"
        )

    steps = [f"найден: {dn}"]

    conn = None
    try:
        conn = _connect(servers[0])
        conn.simple_bind_s(dn, password)
        steps.append("пароль принят")
    except ldap.LDAPError as e:
        raise LdapCheckError(f"Найден ({dn}), но вход не прошёл: {_describe(e)}")
    finally:
        if conn is not None:
            try:
                conn.unbind_s()
            except ldap.LDAPError:
                pass

    require_group = cfg_values.get("require_group")
    if require_group:
        if _in_group(cfg_values, dn, require_group):
            steps.append(f"состоит в {require_group} — вход разрешён")
        else:
            raise LdapCheckError(
                f"Пользователь найден и пароль верный, но он НЕ состоит в группе "
                f"{require_group} — вход будет отклонён"
            )

    for label, group in (("is_staff", cfg_values.get("staff_group")),
                         ("is_superuser", cfg_values.get("superuser_group"))):
        if group:
            has = _in_group(cfg_values, dn, group)
            steps.append(f"{label}: {'да' if has else 'нет'} ({group})")

    return steps


def _in_group(cfg_values, user_dn, group_dn):
    conn = None
    try:
        conn = _connect(cfg_values["servers"][0])
        if cfg_values["bind_dn"]:
            conn.simple_bind_s(cfg_values["bind_dn"], cfg_values["bind_password"])
        else:
            conn.simple_bind_s()
        # Тот же тип групп, что использует бэкенд (GroupOfNamesType): членство
        # выражено атрибутом member со значением DN пользователя.
        found = conn.search_s(
            group_dn, ldap.SCOPE_BASE,
            f"(member={ldap.filter.escape_filter_chars(user_dn)})", ["dn"],
        )
        return bool(found)
    except ldap.NO_SUCH_OBJECT:
        raise LdapCheckError(f"Группа {group_dn} не найдена в каталоге")
    except ldap.LDAPError as e:
        raise LdapCheckError(f"Не удалось проверить членство в {group_dn}: {_describe(e)}")
    finally:
        if conn is not None:
            try:
                conn.unbind_s()
            except ldap.LDAPError:
                pass
