"""Реестр платформ netbox-sync (registry/platforms.json, KTD9 этапа 2).

Списки парсеров и возможностей в sync_config строятся из реестра, а не
пишутся в ip_plan второй раз. Здесь — что множества совпадают с прежними
константами поэлементно (R3: ни одно устройство не начинает опрашиваться
иначе), что настоящий реестр соседнего репозитория даёт то же, что фикстура,
и что отсутствующий/битый реестр — громкая ошибка, а не 500 и не тихий
возврат к старым спискам (R5)."""
import json
import os
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from ip_plan.services import sync_config as sc

from .test_settings import _ConfMixin, _device_post

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Константы sync_config.py до перехода на реестр — эталон для R3.
OLD_INTERFACE_PARSERS = ["xfw", "eltex_status", "eltex_mes24", "cisco_ios", "maipu", "huawei_vrp",
                         "continent_snmp"]
OLD_SNMP_PARSERS = {"continent_snmp"}
OLD_MAC_CAPABLE_PARSERS = {"eltex_status", "eltex_mes24", "maipu", "huawei_vrp", "xfw", "continent_snmp"}
OLD_LLDP_CAPABLE_PARSERS = {"eltex_status", "eltex_mes24", "cisco_ios", "maipu", "huawei_vrp"}


def _old_catalog():
    """parser_catalog() в том виде, как его считал код до реестра."""
    return [{"key": p, "transport": "snmp" if p in OLD_SNMP_PARSERS else "ssh",
             "caps": ["интерфейсы"]
                     + (["MAC"] if p in OLD_MAC_CAPABLE_PARSERS else [])
                     + (["LLDP"] if p in OLD_LLDP_CAPABLE_PARSERS else [])}
            for p in OLD_INTERFACE_PARSERS]


def _snapshot():
    return {
        "interface": list(sc.INTERFACE_PARSERS),
        "snmp": set(sc.SNMP_PARSERS),
        "mac": set(sc.MAC_CAPABLE_PARSERS),
        "lldp": set(sc.LLDP_CAPABLE_PARSERS),
        "catalog": sc.parser_catalog(),
    }


@override_settings(NETBOX_SYNC_REGISTRY_DIR=str(FIXTURES))
class RegistryFixtureTests(SimpleTestCase):
    def test_sets_match_previous_constants(self):
        self.assertEqual(sc.INTERFACE_PARSERS, OLD_INTERFACE_PARSERS)
        self.assertEqual(sc.SNMP_PARSERS, OLD_SNMP_PARSERS)
        self.assertEqual(sc.MAC_CAPABLE_PARSERS, OLD_MAC_CAPABLE_PARSERS)
        self.assertEqual(sc.LLDP_CAPABLE_PARSERS, OLD_LLDP_CAPABLE_PARSERS)

    def test_catalog_and_transport_match_previous(self):
        self.assertEqual(sc.parser_catalog(), _old_catalog())
        self.assertEqual(sc.transport("continent_snmp"), "snmp")
        self.assertEqual(sc.transport("xfw"), "ssh")
        # Неизвестный/пустой парсер — как раньше, SSH.
        self.assertEqual(sc.transport(""), "ssh")
        self.assertEqual(sc.transport("nonexistent"), "ssh")

    def test_xfw_macs_without_cluster_same_check(self):
        types = {"xFW": {"manufacturer": "ViPNet", "role": "firewall", "interfaces_parser": "xfw"}}
        cfg = {"model": "xFW", "sync_interfaces": True, "sync_macs": True}
        with self.assertRaises(sc.SyncConfigError) as ctx:
            sc.validate_device(cfg, types)
        self.assertEqual(str(ctx.exception),
                         "Для парсера «xfw» ARP-таблица снимается только в режиме кластера: "
                         "у модели должно быть задано cluster=vipnet_failover.")
        types["xFW"]["cluster"] = "vipnet_failover"
        sc.validate_device(cfg, types)       # с кластером — проходит

    def test_unsupported_flags_keep_messages(self):
        with self.assertRaises(sc.SyncConfigError) as ctx:
            sc.validate_device_type({"interfaces_parser": "cisco_ios", "sync_macs": True,
                                     "manufacturer": "Cisco", "role": "router"})
        self.assertIn("continent_snmp, eltex_mes24, eltex_status, huawei_vrp, maipu, xfw",
                      str(ctx.exception))
        with self.assertRaises(sc.SyncConfigError) as ctx:
            sc.validate_device_type({"interfaces_parser": "bogus"})
        self.assertIn("Допустимо: " + ", ".join(OLD_INTERFACE_PARSERS), str(ctx.exception))


class RealRegistryTests(SimpleTestCase):
    """Настоящий реестр соседнего репозитория даёт те же списки, что фикстура:
    фикстура не должна отставать от netbox-sync."""

    def test_real_registry_matches_fixture(self):
        real = Path(__file__).resolve().parents[3] / "netbox-sync" / "registry"
        if not (real / "platforms.json").exists():
            self.skipTest("netbox-sync рядом не лежит (контейнер)")
        with override_settings(NETBOX_SYNC_REGISTRY_DIR=str(FIXTURES)):
            expected = _snapshot()
        with override_settings(NETBOX_SYNC_REGISTRY_DIR=str(real)):
            self.assertEqual(_snapshot(), expected)


class RegistryErrorTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = override_settings(NETBOX_SYNC_REGISTRY_DIR=str(self.dir))
        patcher.enable()
        self.addCleanup(patcher.disable)

    def write(self, data):
        path = self.dir / "platforms.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def assertRegistryError(self, fragment):
        for get in (lambda: sc.INTERFACE_PARSERS, lambda: sc.MAC_CAPABLE_PARSERS,
                    lambda: sc.LLDP_CAPABLE_PARSERS, lambda: sc.SNMP_PARSERS, sc.parser_catalog,
                    lambda: sc.transport("xfw")):
            with self.assertRaises(sc.SyncConfigError) as ctx:
                get()
            self.assertIn(fragment, str(ctx.exception))

    def test_missing_file(self):
        self.assertRegistryError("platforms.json")
        self.assertRegistryError("registry")

    def test_missing_dir(self):
        with override_settings(NETBOX_SYNC_REGISTRY_DIR=str(self.dir / "nope")):
            self.assertRegistryError("не найден")

    def test_broken_json(self):
        self.write("{ not json")
        self.assertRegistryError("JSON")

    def test_wrong_version(self):
        self.write({"registry_version": 99, "platforms": {"xfw": {"transport": "ssh"}}})
        self.assertRegistryError("registry_version")
        self.write({"platforms": {"xfw": {"transport": "ssh"}}})
        self.assertRegistryError("registry_version")

    def test_bad_platforms(self):
        self.write({"registry_version": 1, "platforms": []})
        self.assertRegistryError("platforms")
        self.write({"registry_version": 1, "platforms": {}})
        self.assertRegistryError("platforms")
        self.write({"registry_version": 1, "platforms": {"xfw": {"transport": "telnet"}}})
        self.assertRegistryError("transport")

    def test_validate_device_reports_registry(self):
        types = {"M": {"interfaces_parser": "eltex_status", "manufacturer": "E", "role": "s"}}
        with self.assertRaises(sc.SyncConfigError) as ctx:
            sc.validate_device({"model": "M", "sync_interfaces": True, "sync_macs": True}, types)
        self.assertIn("platforms.json", str(ctx.exception))
        with self.assertRaises(sc.SyncConfigError):
            sc.validate_device_type(types["M"])

    def test_file_change_is_picked_up(self):
        # Каталог смонтирован, файл в нём меняет git checkout/rsync — ip_plan
        # должен увидеть новую версию без перезапуска.
        data = json.loads((FIXTURES / "platforms.json").read_text(encoding="utf-8"))
        path = self.write(data)
        self.assertIn("cisco_ios", sc.LLDP_CAPABLE_PARSERS)
        data["platforms"]["cisco_ios"]["lldp"] = False
        self.write(data)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
        self.assertNotIn("cisco_ios", sc.LLDP_CAPABLE_PARSERS)


# Страницы рендерятся без collectstatic: манифест whitenoise в тестах не нужен.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class RegistryMissingPagesTests(_ConfMixin, TestCase):
    """Без реестра страницы «Настроек» показывают плашку, а не 500."""

    def setUp(self):
        super().setUp()
        patcher = override_settings(NETBOX_SYNC_REGISTRY_DIR=str(Path(self.tmp.name) / "no-registry"))
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client.force_login(User.objects.create_superuser("root", password="x"))

    def test_model_card(self):
        for url in ("/settings/models/MES2300/edit/", "/settings/models/add/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("platforms.json", r.content.decode())

    def test_device_card(self):
        for url in ("/settings/devices/sw-1/edit/", "/settings/devices/add/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("platforms.json", r.content.decode())

    def test_tabs(self):
        for tab in ("models", "devices"):
            r = self.client.get(f"/settings/tab/{tab}/")
            self.assertEqual(r.status_code, 200, tab)
            self.assertIn("platforms.json", r.content.decode())

    def test_device_save_is_modal_error(self):
        r = self.client.post("/settings/devices/sw-1/save/", _device_post(file_hash=self.hash()))
        self.assertEqual(r.status_code, 400)
        self.assertIn("platforms.json", r.content.decode())
