"""Консоль ВМ: выбор гипервизора по типу кластера, SPICE прежде VNC, отказ
для незапущенной машины, ссылка VMRC для vCenter."""
from unittest import mock

from django.test import SimpleTestCase

from ip_plan.services import vm_console


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


OVIRT = [{"name": "rv", "url": "https://rv.local/ovirt-engine/api", "user": "u", "password": "p", "site": "mdc"}]
VCENTER = [{"name": "vc", "url": "https://vc.local", "user": "u", "password": "p", "site": "mdc"}]


def _sources(key):
    return ({"sources_redvirt": OVIRT, "sources_vcenter": VCENTER}[key], "hash")


@mock.patch.object(vm_console.sync_config, "load", side_effect=_sources)
class OvirtConsoleTests(SimpleTestCase):
    def _requests(self, status="up", consoles=None):
        consoles = consoles if consoles is not None else [
            {"id": "c-vnc", "protocol": "vnc"}, {"id": "c-spice", "protocol": "spice"}]

        def get(url, **kw):
            if url.endswith("/vms"):
                return _Resp({"vm": [{"id": "vm-1", "name": "srv-01", "status": status}]})
            if url.endswith("/graphicsconsoles"):
                return _Resp({"graphics_console": consoles})
            raise AssertionError(url)

        def post(url, **kw):
            self.posted = url
            return _Resp({"remote_viewer_connection_file": "[virt-viewer]\ntype=spice\nhost=h\nport=5900\n"})
        return get, post

    def test_spice_preferred_and_file_returned(self, _load):
        get, post = self._requests()
        with mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "post", side_effect=post):
            vm = {"name": "srv-01"}
            cluster = {"type": {"slug": "red-virt"}, "site": {"slug": "mdc"}}
            result = vm_console.console_for_vm(vm, cluster)
        self.assertEqual(result["kind"], "file")
        self.assertEqual(result["filename"], "srv-01.vv")
        self.assertEqual(result["protocol"], "SPICE")
        self.assertIn("/graphicsconsoles/c-spice/remoteviewerconnectionfile", self.posted)
        self.assertIn("[virt-viewer]", result["content"])

    def test_vm_not_running_is_a_clear_error(self, _load):
        get, post = self._requests(status="down")
        with mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "post", side_effect=post):
            with self.assertRaises(vm_console.ConsoleError) as ctx:
                vm_console.console_for_vm({"name": "srv-01"}, {"type": {"slug": "red-virt"}})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("не запущена", str(ctx.exception))

    def test_headless_vm(self, _load):
        get, post = self._requests(consoles=[])
        with mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "post", side_effect=post):
            with self.assertRaises(vm_console.ConsoleError):
                vm_console.console_for_vm({"name": "srv-01"}, {"type": {"slug": "red-virt"}})


@mock.patch.object(vm_console.sync_config, "load", side_effect=_sources)
class OvirtWebConsoleTests(SimpleTestCase):
    """web=True — реквизиты VNC для шлюза вместо файла .vv."""

    def _requests(self, consoles):
        def get(url, **kw):
            if url.endswith("/vms"):
                return _Resp({"vm": [{"id": "vm-1", "name": "srv-01", "status": "up"}]})
            if url.endswith("/graphicsconsoles"):
                self.console_params = kw.get("params")
                return _Resp({"graphics_console": consoles})
            raise AssertionError(url)

        def post(url, **kw):
            self.posted, self.post_json = url, kw.get("json")
            return _Resp({"ticket": {"value": "s3cret", "expiry": "120"}})
        return get, post

    def test_vnc_ticket_with_host_and_port(self, _load):
        get, post = self._requests([
            {"id": "c-spice", "protocol": "spice", "address": "10.0.0.5", "port": "5900"},
            {"id": "c-vnc", "protocol": "vnc", "address": "10.0.0.5", "port": "5902"}])
        with mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "post", side_effect=post):
            result = vm_console.console_for_vm({"name": "srv-01"}, {"type": {"slug": "red-virt"}}, web=True)
        self.assertEqual((result["host"], result["port"], result["password"]), ("10.0.0.5", 5902, "s3cret"))
        self.assertEqual(self.console_params, {"current": "true"})
        self.assertTrue(self.posted.endswith("/graphicsconsoles/c-vnc/ticket"))
        self.assertEqual(self.post_json, {"ticket": {"expiry": vm_console.VNC_TICKET_TTL}})

    def test_spice_only_vm_points_to_the_vv_file(self, _load):
        get, post = self._requests([{"id": "c-spice", "protocol": "spice", "address": "10.0.0.5", "port": "5900"}])
        with mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "post", side_effect=post):
            with self.assertRaises(vm_console.ConsoleError) as ctx:
                vm_console.console_for_vm({"name": "srv-01"}, {"type": {"slug": "red-virt"}}, web=True)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("SPICE", str(ctx.exception))

    def test_vcenter_vm_is_refused_without_calling_hypervisors(self, _load):
        with mock.patch.object(vm_console.requests, "get", side_effect=AssertionError), \
             mock.patch.object(vm_console.requests, "post", side_effect=AssertionError):
            with self.assertRaises(vm_console.ConsoleError) as ctx:
                vm_console.console_for_vm({"name": "win-01"}, {"type": {"slug": "vcenter"}}, web=True)
        self.assertEqual(ctx.exception.status, 409)


@mock.patch.object(vm_console.sync_config, "load", side_effect=_sources)
class VcenterConsoleTests(SimpleTestCase):
    def test_vmrc_url(self, _load):
        calls = []

        def post(url, **kw):
            calls.append(("post", url))
            return _Resp({"value": "tok"})

        def get(url, **kw):
            calls.append(("get", url, kw.get("params")))
            return _Resp({"value": [{"vm": "vm-42", "name": "win-01"}]})

        def delete(url, **kw):
            calls.append(("delete", url))
            return _Resp({})
        with mock.patch.object(vm_console.requests, "post", side_effect=post), \
             mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "delete", side_effect=delete):
            result = vm_console.console_for_vm({"name": "win-01"}, {"type": {"slug": "vcenter"}})
        self.assertEqual(result, {**result, "kind": "url", "url": "vmrc://vc.local/?moid=vm-42"})
        self.assertEqual(calls[1][2], {"filter.names": "win-01"})
        self.assertEqual(calls[-1][0], "delete", "сессия vCenter закрывается")
        # к oVirt не ходили — тип кластера vcenter
        self.assertTrue(all("ovirt" not in c[1] for c in calls))

    def test_unknown_cluster_type_tries_both_and_reports_not_found(self, _load):
        def get(url, **kw):
            if url.endswith("/vms"):
                return _Resp({"vm": []})
            return _Resp({"value": []})
        with mock.patch.object(vm_console.requests, "get", side_effect=get), \
             mock.patch.object(vm_console.requests, "post", return_value=_Resp({"value": "tok"})), \
             mock.patch.object(vm_console.requests, "delete", return_value=_Resp({})):
            self.assertIsNone(vm_console.console_for_vm({"name": "ghost"}, None))
