"""Поиск MAC/IP по хранилищу наблюдений netbox-sync (база netobs) для
страницы «Поиск MAC/IP» (U11 плана хранилища наблюдений, R11, R12, KTD11).

Где виден адрес — по истории наблюдений MAC-таблиц и ARP, которую пишет
netbox-sync. С netobs v2 (netbox-sync netobs/migrations/0002_intervals.sql,
этап 2 U7, KTD7) наблюдения хранятся интервалами, и поиск читает
представление совместимости observations_compat: колонки v1 без seen_count,
одна строка на ключ (последний интервал; first_seen — самое раннее
появление), плюс run_kind (вид опроса last_run_id) и is_current (строка есть
в current_observations). «Последний раз» (last_seen) представление считает
само по правилу v1. Представление current_observations — «текущее содержимое
порта» (то же определение, что у топологии) — нужно только для числа адресов
за портом. Моделей Django у netobs нет: сырой SQL через алиас "netobs"
(роль ip_plan_ro, только SELECT), ввод — только параметрами.

Ввод:
  * полный MAC в любой записи — aa:bb:…, aabb.ccdd.eeff, AA-BB-…, 12 hex;
  * часть MAC от 4 hex-символов — подстрока (значит, и префикс) MAC без
    разделителей;
  * IP (v4/v6) — только точное совпадение: строки ARP с этим IP плюс строки
    MAC-таблиц для MAC, найденных в этих ARP-строках. Чужие IP того же MAC
    (MAC маршрутизатора в ARP ядра стоит за десятками адресов) в выдачу не
    попадают;
  * всё прочее — QueryError с понятным текстом.

Выдача — не больше LIMIT строк и общее число совпадений. Порядок: текущие
строки первыми, внутри — порты с меньшим числом адресов (по текущим строкам:
порт доступа, а не транк), затем свежие. Недоступная или не настроенная
netobs — исключение Unavailable, страница показывает сообщение (R12)."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import Error as DatabaseError, connections
from django.utils import timezone

from ip_plan.templatetags.ip_plan_extras import plural

ALIAS = "netobs"
LIMIT = 200
MIN_PART_HEX = 4
ARP_SOURCES = ("arp-table", "arp-l3")

SOURCE_LABELS = {
    "mac-table": "MAC-таблица",
    "arp-table": "ARP",
    "arp-l3": "ARP ядра",
}

_MAC_SEPARATORS = re.compile(r"[\s:.\-]")
_HEX = re.compile(r"[0-9a-f]+")
_IPV4_LIKE = re.compile(r"\d{1,3}(\.\d{1,3}){1,3}(/\d+)?")


class QueryError(ValueError):
    """Строка запроса — не MAC, не часть MAC и не IP."""


class Unavailable(Exception):
    """netobs не настроена (нет NETOBS_RO_* в .env) или недоступна."""

    def __init__(self, message, configured=True):
        super().__init__(message)
        self.configured = configured


@dataclass(frozen=True)
class Query:
    kind: str   # mac | mac_part | ip
    value: str  # aa:bb:cc:dd:ee:ff | hex без разделителей | IP


# ---------------------------------------------------------------- разбор


def parse_query(text: str) -> Query:
    raw = (text or "").strip()
    if not raw:
        raise QueryError("Введите MAC-адрес, его часть (от 4 символов) или IP-адрес.")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        ip = None
    if ip is not None:
        return Query("ip", str(ip))
    if _IPV4_LIKE.fullmatch(raw):
        raise QueryError(
            f"«{raw}» — не полный IP-адрес. По IP ищется только точное "
            "совпадение, без подсетей и частей адреса."
        )
    hex_part = _MAC_SEPARATORS.sub("", raw.lower())
    if not hex_part or not _HEX.fullmatch(hex_part):
        raise QueryError(
            f"«{raw}» не похоже ни на MAC, ни на IP. MAC — шестнадцатеричные "
            "цифры с разделителями «:», «-», «.» или без них; IP — полный адрес."
        )
    if len(hex_part) == 12:
        return Query("mac", ":".join(hex_part[i:i + 2] for i in range(0, 12, 2)))
    if len(hex_part) > 12:
        raise QueryError(f"«{raw}» длиннее MAC-адреса (12 шестнадцатеричных цифр).")
    if len(hex_part) < MIN_PART_HEX:
        raise QueryError(
            f"Часть MAC должна быть не короче {MIN_PART_HEX} шестнадцатеричных "
            f"цифр — «{raw}» нашла бы слишком много адресов."
        )
    return Query("mac_part", hex_part)


def like_contains(value: str) -> str:
    """Шаблон LIKE «содержит value» с экранированием \\, % и _ (ESCAPE '\\')."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ------------------------------------------------------------------- SQL

_ROWS_SQL = """
WITH hits AS (
    SELECT o.id, o.nb_device, o.interface, o.vlan, o.mac, o.ip, o.source,
           o.first_seen, o.last_seen, o.is_current, o.run_kind
    FROM observations_compat o
    WHERE {where}
),
ports AS (
    -- Сколько адресов за портом сейчас: только текущие строки того же
    -- устройства, порта и источника.
    SELECT c.nb_device, c.interface, c.source,
           count(DISTINCT coalesce(c.mac::text, host(c.ip))) AS n
    FROM current_observations c
    JOIN (SELECT DISTINCT nb_device, interface, source FROM hits) p
      ON p.nb_device = c.nb_device AND p.interface = c.interface AND p.source = c.source
    GROUP BY c.nb_device, c.interface, c.source
)
SELECT h.mac::text, host(h.ip), h.nb_device, h.interface, h.vlan, h.source,
       h.first_seen, h.last_seen, h.is_current,
       coalesce(ports.n, 0) AS port_addrs,
       h.run_kind,
       count(*) OVER () AS total
FROM hits h
LEFT JOIN ports ON ports.nb_device = h.nb_device AND ports.interface = h.interface
               AND ports.source = h.source
ORDER BY h.is_current DESC, port_addrs ASC, h.last_seen DESC, h.id
LIMIT %s
"""

_WHERE = {
    "mac": "o.mac = %s::macaddr",
    "mac_part": "replace(o.mac::text, ':', '') LIKE %s ESCAPE '\\'",
    # ARP-строки — по точному IP; по MAC из них — только строки без IP
    # (MAC-таблицы), не чужие IP того же MAC.
    "ip": (
        "o.ip = %s::inet OR (o.ip IS NULL AND o.mac IN ("
        "SELECT a.mac FROM observations_compat a WHERE a.ip = %s::inet"
        " AND a.source IN ('arp-table', 'arp-l3') AND a.mac IS NOT NULL))"
    ),
}

# Время опроса — started_at, как и last_seen строк: этим временем синк
# помечает весь вывод (netbox-sync, netobs.record_output).
_STATUS_SQL = """
SELECT bool_or(ok), max(started_at) FILTER (WHERE ok AND kind = 'poll')
FROM poll_runs
"""

_ROW_KEYS = ("mac", "ip", "device", "interface", "vlan", "source", "first_seen",
             "last_seen", "is_current", "port_addrs", "run_kind", "total")


def configured() -> bool:
    return ALIAS in connections.databases


class SqlStore:
    """Чтение netobs. cursor — для тестов (подставной курсор); без него
    каждый вызов открывает курсор на алиасе netobs."""

    def __init__(self, cursor=None):
        self._cursor = cursor

    def _run(self, sql, params, one=False):
        if self._cursor is not None:
            self._cursor.execute(sql, params)
            return self._cursor.fetchone() if one else self._cursor.fetchall()
        with connections[ALIAS].cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if one else cur.fetchall()

    def status(self):
        """(был ли хоть один успешный опрос или импорт, время последнего
        успешного опроса)."""
        row = self._run(_STATUS_SQL, [], one=True) or (None, None)
        return bool(row[0]), row[1]

    def rows(self, query: Query, limit: int):
        if query.kind == "mac":
            params = [query.value]
        elif query.kind == "mac_part":
            params = [like_contains(query.value)]
        else:
            params = [query.value, query.value]
        sql = _ROWS_SQL.format(where=_WHERE[query.kind])
        return [dict(zip(_ROW_KEYS, r)) for r in self._run(sql, params + [limit])]


# ---------------------------------------------------------------- выдача


@dataclass
class Status:
    any_ok: bool
    last_poll: datetime | None
    last_poll_text: str = ""


@dataclass
class SearchResult:
    query: Query
    rows: list = field(default_factory=list)
    total: int = 0
    no_polls: bool = False
    last_poll_text: str = ""
    empty_text: str = ""

    @property
    def truncated(self):
        return self.total > len(self.rows)


def _tz():
    return ZoneInfo(getattr(settings, "SYNC_LOG_TZ", "Europe/Moscow") or "Europe/Moscow")


def _date_text(value: datetime | None) -> str:
    if value is None:
        return ""
    if timezone.is_naive(value):
        value = timezone.make_aware(value, ZoneInfo("UTC"))
    return value.astimezone(_tz()).strftime("%d.%m.%Y %H:%M")


def _age_text(value: datetime, now: datetime) -> str:
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} {plural(hours, 'час,часа,часов')} назад"
    days = hours // 24
    if days < 60:
        return f"{days} {plural(days, 'день,дня,дней')} назад"
    months = days // 30
    return f"{months} {plural(months, 'месяц,месяца,месяцев')} назад"


def _sort_key(row):
    # Тот же порядок, что ORDER BY в _ROWS_SQL: SQL решает, какие LIMIT строк
    # попадут на страницу, здесь порядок только закрепляется.
    return (not row["is_current"], row["port_addrs"], -row["last_seen"].timestamp())


def _decorate(row, now):
    imported = row.get("run_kind") == "import"
    first_seen = row.get("first_seen")
    return {
        **row,
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "state": "текущее" if row["is_current"] else "прежнее",
        "imported": imported,
        "muted": imported or not row["is_current"],
        "first_seen_text": _date_text(first_seen) if first_seen else "до переноса",
        "last_seen_age": _age_text(row["last_seen"], now),
        "last_seen_title": _date_text(row["last_seen"]),
    }


def _store(store):
    if store is not None:
        return store
    if not configured():
        raise Unavailable(
            "Хранилище наблюдений не настроено: в .env нет параметров "
            "подключения NETOBS_RO_*.",
            configured=False,
        )
    return SqlStore()


def _unavailable(exc):
    return Unavailable(f"Хранилище наблюдений недоступно: {exc}".strip())


def status(store=None) -> Status:
    """Состояние хранилища для начального экрана страницы."""
    store = _store(store)
    try:
        any_ok, last_poll = store.status()
    except DatabaseError as exc:
        raise _unavailable(exc) from exc
    return Status(any_ok, last_poll, _date_text(last_poll))


def search(text: str, store=None, now: datetime | None = None) -> SearchResult:
    query = parse_query(text)
    store = _store(store)
    now = now or timezone.now()
    try:
        any_ok, last_poll = store.status()
        raw = store.rows(query, LIMIT)
    except DatabaseError as exc:
        raise _unavailable(exc) from exc
    rows = sorted((_decorate(r, now) for r in raw), key=_sort_key)
    result = SearchResult(
        query=query,
        rows=rows,
        total=int(raw[0]["total"]) if raw else 0,
        no_polls=not any_ok,
        last_poll_text=_date_text(last_poll),
    )
    if not rows:
        result.empty_text = _empty_text(query, result)
    return result


def _empty_text(query: Query, result: SearchResult) -> str:
    if result.no_polls:
        return "Опросы ещё не выполнялись — искать пока не в чем."
    if query.kind == "ip":
        return "Для этого IP нет ARP-записей."
    when = (f" Последний успешный опрос — {result.last_poll_text}."
            if result.last_poll_text else " Успешных опросов ещё не было, есть только перенос из NetBox.")
    return "Адрес не встречался в таблицах опрошенных устройств." + when
