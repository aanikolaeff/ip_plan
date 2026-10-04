"""Поиск MAC/IP по хранилищу наблюдений netobs (U11 плана хранилища
наблюдений). Схема netobs принадлежит netbox-sync, отдельная тестовая база
под неё не создаётся (KTD11): разбор ввода проверяется напрямую, работа с
базой — через подставное хранилище (FakeStore) или подставной курсор,
который запоминает SQL и параметры. Сам SQL против живой схемы проверяется
вручную на временной базе (см. отчёт U11)."""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ip_plan.services import netobs_search as ns

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=dt_timezone.utc)


def _row(mac="aa:bb:cc:dd:ee:ff", ip=None, device="sw1", interface="Gi0/1",
         source="mac-table", current=True, port_addrs=1, last_seen=None,
         first_seen=None, run_kind="poll", total=1):
    return {
        "mac": mac, "ip": ip, "device": device, "interface": interface,
        "vlan": None, "source": source,
        "first_seen": first_seen if first_seen is not None else NOW - timedelta(days=5),
        "last_seen": last_seen or NOW - timedelta(hours=2),
        "is_current": current, "port_addrs": port_addrs,
        "run_kind": run_kind, "total": total,
    }


class FakeStore:
    """Хранилище с заранее заданным ответом; запоминает, о чём спросили."""

    def __init__(self, rows=(), any_ok=True, last_poll=NOW - timedelta(hours=1)):
        self._rows = list(rows)
        self.any_ok = any_ok
        self.last_poll = last_poll
        self.calls = []

    def status(self):
        return self.any_ok, self.last_poll

    def rows(self, query, limit):
        self.calls.append((query, limit))
        return self._rows[:limit]


# ------------------------------------------------------------------ разбор


class ParseQueryTests(SimpleTestCase):
    def test_four_mac_notations_give_same_query(self):
        forms = ["aa:bb:cc:dd:ee:ff", "aabb.ccdd.eeff", "AA-BB-CC-DD-EE-FF", "AABBCCDDEEFF"]
        queries = {ns.parse_query(f) for f in forms}
        self.assertEqual(queries, {ns.Query("mac", "aa:bb:cc:dd:ee:ff")})

    def test_partial_mac_from_four_hex(self):
        self.assertEqual(ns.parse_query("90:54"), ns.Query("mac_part", "9054"))
        self.assertEqual(ns.parse_query(" B7.34 "), ns.Query("mac_part", "b734"))
        self.assertEqual(ns.parse_query("06c0"), ns.Query("mac_part", "06c0"))

    def test_three_hex_rejected(self):
        with self.assertRaises(ns.QueryError) as cm:
            ns.parse_query("aab")
        self.assertIn("4", str(cm.exception))

    def test_percent_and_underscore_are_not_patterns(self):
        for text in ("%", "aa%bb", "aa_bb", "____", "%%%%"):
            with self.subTest(text=text), self.assertRaises(ns.QueryError):
                ns.parse_query(text)

    def test_like_pattern_escapes_wildcards(self):
        # Сервис пропускает в LIKE только hex, но экранирование — вторая
        # линия защиты: % и _ никогда не становятся шаблоном.
        self.assertEqual(ns.like_contains("ab%c_d\\"), "%ab\\%c\\_d\\\\%")

    def test_ip_exact(self):
        self.assertEqual(ns.parse_query("10.1.141.166"), ns.Query("ip", "10.1.141.166"))
        self.assertEqual(ns.parse_query(" 2001:db8::1 "), ns.Query("ip", "2001:db8::1"))

    def test_incomplete_ip_and_garbage_rejected(self):
        for text in ("", "10.1.141", "10.1.141.0/24", "hello", "aa:bb:cc:dd:ee:ff:00", "zz:zz"):
            with self.subTest(text=text), self.assertRaises(ns.QueryError):
                ns.parse_query(text)


# ------------------------------------------------------------------- SQL


class FakeCursor:
    def __init__(self, fetch=()):
        self.executed = []
        self._fetch = list(fetch)
        self.description = []

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params or [])))

    def fetchall(self):
        return self._fetch

    def fetchone(self):
        return self._fetch[0] if self._fetch else None


class SqlTests(SimpleTestCase):
    def test_reads_compat_view_not_v1_tables(self):
        # netobs v2 (netbox-sync 0002): таблица observations — интервалы,
        # seen_count нет. Поиск читает представление совместимости
        # observations_compat (KTD7): колонки v1, одна строка на ключ,
        # run_kind и is_current уже посчитаны.
        for text in ("aabbccddeeff", "9054b7", "10.2.22.17"):
            with self.subTest(text=text):
                sql, _ = self._sql_for(text)
                self.assertIn("FROM observations_compat o", sql)
                self.assertNotIn("FROM observations o", sql)
                self.assertNotIn("seen_count", sql)
                self.assertNotIn("poll_runs", sql)
                self.assertNotIn("FROM observations a", sql)

    def _sql_for(self, text):
        cur = FakeCursor()
        ns.SqlStore(cursor=cur).rows(ns.parse_query(text), 200)
        self.assertEqual(len(cur.executed), 1)
        return cur.executed[0]

    def test_user_input_only_in_params(self):
        sql, params = self._sql_for("aa-bb-cc-dd-ee-ff")
        self.assertNotIn("aa:bb", sql)
        self.assertIn("aa:bb:cc:dd:ee:ff", params)
        self.assertEqual(params[-1], 200)

    def test_partial_mac_uses_escaped_like(self):
        sql, params = self._sql_for("9054b7")
        self.assertIn("LIKE %s ESCAPE", sql)
        self.assertIn("%9054b7%", params)

    def test_ip_search_keeps_only_this_ip_of_shared_mac(self):
        # За MAC маршрутизатора в ARP ядра стоят десятки IP: строки ARP
        # отбираются по точному IP, по найденным MAC добавляются только
        # строки без IP (MAC-таблицы), а не чужие IP того же MAC.
        sql, params = self._sql_for("10.2.22.17")
        self.assertIn("o.ip = %s::inet", sql)
        self.assertIn("o.ip IS NULL", sql)
        self.assertIn("'arp-table'", sql)
        self.assertIn("'arp-l3'", sql)
        self.assertEqual(params.count("10.2.22.17"), 2)
        self.assertNotIn("LIKE", sql)


class V2CursorTests(SimpleTestCase):
    """Подставной курсор с ответом в форме netobs v2: строки выдачи — как их
    отдаёт _ROWS_SQL поверх observations_compat (без seen_count)."""

    LAST = NOW - timedelta(minutes=7)

    def _v2_rows(self):
        # mac, ip, device, interface, vlan, source, first_seen, last_seen,
        # is_current, port_addrs, run_kind, total
        return [
            ("aa:bb:cc:dd:ee:ff", None, "sw1", "Gi0/7", None, "mac-table",
             NOW - timedelta(days=5), self.LAST, True, 1, "poll", 3),
            ("aa:bb:cc:dd:ee:ff", "10.2.22.17", "core", "Vlan22", None, "arp-l3",
             None, NOW - timedelta(hours=2), True, 30, "import", 3),
            ("aa:bb:cc:dd:ee:ff", None, "sw1", "Gi0/1", None, "mac-table",
             NOW - timedelta(days=9), NOW - timedelta(days=2), False, 0, "poll", 3),
        ]

    def _search(self, text):
        cur = FakeCursor(self._v2_rows())
        # status() читает тот же курсор: первая строка — (any_ok, last_poll).
        cur.fetchone = lambda: (True, NOW - timedelta(minutes=7))
        return ns.search(text, store=ns.SqlStore(cursor=cur), now=NOW)

    def test_same_rows_for_mac_ip_and_partial_mac(self):
        expected = [
            ("sw1", "Gi0/7", "mac-table", "текущее", False),
            ("core", "Vlan22", "arp-l3", "текущее", True),
            ("sw1", "Gi0/1", "mac-table", "прежнее", False),
        ]
        for text in ("aa:bb:cc:dd:ee:ff", "10.2.22.17", "ccdd"):
            with self.subTest(text=text):
                result = self._search(text)
                self.assertEqual(result.total, 3)
                self.assertEqual(
                    [(r["device"], r["interface"], r["source"], r["state"], r["imported"])
                     for r in result.rows],
                    expected)
                self.assertNotIn("seen_count", result.rows[0])

    def test_last_seen_comes_from_view(self):
        # Открытый интервал без пропусков: last_seen в observations_compat —
        # started_at последнего успешного опроса пары (правило KTD7), сервис
        # берёт его как есть.
        row = self._search("aabbccddeeff").rows[0]
        self.assertEqual(row["last_seen"], self.LAST)
        self.assertEqual(row["last_seen_age"], "7 мин назад")
        self.assertEqual(row["first_seen_text"], "28.09.2026 15:00")

    def test_database_error_from_cursor_becomes_unavailable(self):
        cur = FakeCursor()
        cur.execute = mock.Mock(side_effect=OperationalError("relation \"observations\" does not exist"))
        with self.assertRaises(ns.Unavailable):
            ns.search("aabbccddeeff", store=ns.SqlStore(cursor=cur), now=NOW)


# ---------------------------------------------------------------- сервис


class SearchServiceTests(SimpleTestCase):
    def test_limit_and_total(self):
        rows = [_row(interface=f"Gi0/{i}", total=350) for i in range(200)]
        store = FakeStore(rows)
        result = ns.search("aa:bb:cc:dd:ee:ff", store=store, now=NOW)
        self.assertEqual(store.calls[0][1], ns.LIMIT)
        self.assertEqual(ns.LIMIT, 200)
        self.assertEqual(len(result.rows), 200)
        self.assertEqual(result.total, 350)
        self.assertTrue(result.truncated)

    def test_moved_mac_new_port_current_old_port_previous(self):
        store = FakeStore([
            _row(interface="Gi0/1", current=False, last_seen=NOW - timedelta(days=2), total=2),
            _row(interface="Gi0/7", current=True, last_seen=NOW - timedelta(hours=1), total=2),
        ])
        result = ns.search("aabb.ccdd.eeff", store=store, now=NOW)
        by_port = {r["interface"]: r for r in result.rows}
        self.assertEqual(by_port["Gi0/7"]["state"], "текущее")
        self.assertEqual(by_port["Gi0/1"]["state"], "прежнее")
        self.assertTrue(by_port["Gi0/1"]["muted"])
        self.assertFalse(by_port["Gi0/7"]["muted"])
        self.assertEqual(result.rows[0]["interface"], "Gi0/7")

    def test_order_current_first_then_fewer_addresses_then_fresh(self):
        store = FakeStore([
            _row(interface="uplink", port_addrs=40, last_seen=NOW - timedelta(minutes=5)),
            _row(interface="old", current=False, port_addrs=1),
            _row(interface="access-old", port_addrs=1, last_seen=NOW - timedelta(hours=5)),
            _row(interface="access-new", port_addrs=1, last_seen=NOW - timedelta(hours=1)),
        ])
        result = ns.search("aabbccddeeff", store=store, now=NOW)
        self.assertEqual([r["interface"] for r in result.rows],
                         ["access-new", "access-old", "uplink", "old"])

    def test_imported_rows_muted_and_first_seen_before_transfer(self):
        store = FakeStore([_row(first_seen=None, run_kind="import")])
        row = ns.search("aabbccddeeff", store=store, now=NOW).rows[0]
        # first_seen пуст у строк, перенесённых из NetBox (schema 0001).
        row_first = ns.search("aabbccddeeff", store=FakeStore([dict(_row(), first_seen=None)]), now=NOW).rows[0]
        self.assertEqual(row_first["first_seen_text"], "до переноса")
        self.assertTrue(row["imported"])
        self.assertTrue(row["muted"])

    def test_last_seen_relative_with_date(self):
        row = ns.search("aabbccddeeff", store=FakeStore([_row(last_seen=NOW - timedelta(hours=3))]),
                        now=NOW).rows[0]
        self.assertEqual(row["last_seen_age"], "3 часа назад")
        self.assertIn("03.10.2026", row["last_seen_title"])

    def test_ip_without_arp(self):
        result = ns.search("10.9.9.9", store=FakeStore([]), now=NOW)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.empty_text, "Для этого IP нет ARP-записей.")

    def test_empty_mac_mentions_last_poll(self):
        result = ns.search("aabbccddeeff", store=FakeStore([]), now=NOW)
        self.assertIn("не встречался в таблицах опрошенных устройств", result.empty_text)
        self.assertIn("03.10.2026", result.empty_text)

    def test_no_polls_yet(self):
        result = ns.search("aabbccddeeff", store=FakeStore([], any_ok=False, last_poll=None), now=NOW)
        self.assertTrue(result.no_polls)
        self.assertEqual(result.empty_text, "Опросы ещё не выполнялись — искать пока не в чем.")

    def test_database_error_becomes_unavailable(self):
        store = FakeStore()
        store.status = mock.Mock(side_effect=OperationalError("connection refused"))
        with self.assertRaises(ns.Unavailable):
            ns.search("aabbccddeeff", store=store, now=NOW)

    def test_not_configured(self):
        with mock.patch.object(ns, "configured", return_value=False), \
             self.assertRaises(ns.Unavailable) as cm:
            ns.search("aabbccddeeff")
        self.assertFalse(cm.exception.configured)


# ---------------------------------------------------------------- страница


class ObsSearchViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="x")
        self.url = reverse("ip_plan:obs_search")

    def _get(self, q=None):
        params = {"q": q} if q is not None else {}
        return self.client.get(self.url, params, HTTP_HOST="localhost")

    def _patch_store(self, store):
        return mock.patch.object(ns, "SqlStore", return_value=store)

    def test_anonymous_redirected_to_login(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("ip_plan:login"), resp["Location"])

    def test_any_logged_in_user_sees_page_and_menu(self):
        self.client.force_login(self.user)
        with mock.patch.object(ns, "configured", return_value=True), self._patch_store(FakeStore()):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Поиск MAC/IP")
        self.assertContains(resp, "aabb.ccdd.eeff")  # примеры форматов

    def test_rows_rendered_with_shown_of_total(self):
        self.client.force_login(self.user)
        rows = [_row(interface=f"Gi0/{i}", total=350) for i in range(200)]
        with mock.patch.object(ns, "configured", return_value=True), self._patch_store(FakeStore(rows)):
            resp = self._get("aa:bb:cc:dd:ee:ff")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "показано 200 из 350")
        self.assertContains(resp, "Gi0/199")

    def test_query_error_shown(self):
        self.client.force_login(self.user)
        with mock.patch.object(ns, "configured", return_value=True), self._patch_store(FakeStore()):
            resp = self._get("aab")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "не короче 4")

    def test_no_polls_message(self):
        self.client.force_login(self.user)
        store = FakeStore([], any_ok=False, last_poll=None)
        with mock.patch.object(ns, "configured", return_value=True), self._patch_store(store):
            resp = self._get("aabbccddeeff")
        self.assertContains(resp, "Опросы ещё не выполнялись")
        self.assertNotContains(resp, "не встречался")

    def test_ip_without_arp_message(self):
        self.client.force_login(self.user)
        with mock.patch.object(ns, "configured", return_value=True), self._patch_store(FakeStore([])):
            resp = self._get("10.9.9.9")
        self.assertContains(resp, "Для этого IP нет ARP-записей.")

    def test_unavailable_netobs_is_200_with_message(self):
        self.client.force_login(self.user)
        store = FakeStore()
        store.status = mock.Mock(side_effect=OperationalError("connection refused"))
        with mock.patch.object(ns, "configured", return_value=True), self._patch_store(store):
            resp = self._get("aabbccddeeff")
            start = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Хранилище наблюдений недоступно")
        self.assertEqual(start.status_code, 200)
        self.assertContains(start, "Хранилище наблюдений недоступно")

    def test_unavailable_v2_cursor_is_200_with_message(self):
        self.client.force_login(self.user)
        cur = FakeCursor()
        cur.execute = mock.Mock(side_effect=OperationalError("connection refused"))
        with mock.patch.object(ns, "configured", return_value=True), \
             self._patch_store(ns.SqlStore(cursor=cur)):
            resp = self._get("10.2.22.17")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Хранилище наблюдений недоступно")

    def test_not_configured_message(self):
        self.client.force_login(self.user)
        with mock.patch.object(ns, "configured", return_value=False):
            resp = self._get("aabbccddeeff")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "не настроено")
