"""Доступ к хостам по SSH: хранение ключа (0600, без истории, проверка хеша),
разбор ключей разных типов и понятные отказы, разбор сведений об ОС."""
import stat
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from django.test import SimpleTestCase, override_settings

from ip_plan.services import remote_gateway, ssh_access
from ip_plan.services import sync_config as sc


def _pem(key, passphrase=None, fmt=serialization.PrivateFormat.OpenSSH):
    enc = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
           else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc).decode()


ED_KEY = _pem(ed25519.Ed25519PrivateKey.generate())
RSA_KEY = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048),
               fmt=serialization.PrivateFormat.TraditionalOpenSSL)
LOCKED_KEY = _pem(ed25519.Ed25519PrivateKey.generate(), passphrase="s3cret")


class KeyParsingTests(SimpleTestCase):
    def test_ed25519_and_rsa_are_described_without_revealing_the_key(self):
        for text, kind in ((ED_KEY, "Ed25519"), (RSA_KEY, "RSA")):
            info = ssh_access.describe_key(ssh_access.parse_key(text))
            self.assertEqual(info["kind"], kind)
            self.assertTrue(info["fingerprint"].startswith("SHA256:"))
            self.assertNotIn("PRIVATE", info["public"])

    def test_windows_line_endings_and_missing_final_newline(self):
        mangled = ED_KEY.strip().replace("\n", "\r\n")
        self.assertEqual(ssh_access.normalize_key(mangled), ED_KEY)
        ssh_access.parse_key(mangled)

    def test_passphrase_required_wrong_and_right(self):
        with self.assertRaises(ssh_access.SshAccessError) as ctx:
            ssh_access.parse_key(LOCKED_KEY)
        self.assertIn("укажите", str(ctx.exception))
        with self.assertRaises(ssh_access.SshAccessError) as ctx:
            ssh_access.parse_key(LOCKED_KEY, "wrong")
        self.assertIn("не подошла", str(ctx.exception))
        ssh_access.parse_key(LOCKED_KEY, "s3cret")

    def test_public_key_putty_key_and_garbage_get_specific_messages(self):
        public = ssh_access.describe_key(ssh_access.parse_key(ED_KEY))["public"]
        for text, expected in ((public, "открытый ключ"), ("PuTTY-User-Key-File-3: ssh-ed25519\n", "PuTTY"),
                               ("hello", "Не похоже")):
            with self.assertRaises(ssh_access.SshAccessError) as ctx:
                ssh_access.parse_key(text)
            self.assertIn(expected, str(ctx.exception))

    def test_username_rules(self):
        self.assertEqual(ssh_access.validate_username(" svc_ipplan "), "svc_ipplan")
        for bad in ("", "root; rm -rf /", "-oProxyCommand=x", "пользователь"):
            with self.assertRaises(ssh_access.SshAccessError):
                ssh_access.validate_username(bad)


class StorageTests(SimpleTestCase):
    def test_file_is_private_has_no_history_and_checks_hash(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(NETBOX_SYNC_CONF_DIR=tmp):
            self.assertEqual(ssh_access.load(), ({}, ""))
            self.assertFalse(ssh_access.configured())
            ssh_access.save({"username": "svc", "port": 22, "private_key": ED_KEY, "passphrase": ""}, "")
            cfg, digest = ssh_access.load()
            self.assertTrue(ssh_access.configured(cfg))
            self.assertEqual(stat.S_IMODE(Path(tmp, ssh_access.FILENAME).stat().st_mode), 0o600)
            # перезапись — без копии прежнего ключа в .history
            ssh_access.save({**cfg, "username": "svc2"}, digest)
            self.assertFalse(Path(tmp, ".history").exists())
            # форма, открытая до чужой правки, не затирает её
            with self.assertRaises(sc.ConfigConflict):
                ssh_access.save(cfg, digest)
            ssh_access.delete()
            self.assertEqual(ssh_access.load(), ({}, ""))

    def test_summary_never_contains_the_key(self):
        text = repr(ssh_access.summary({"username": "svc", "private_key": LOCKED_KEY, "passphrase": "s3cret"}))
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("s3cret", text)
        self.assertIn("SHA256:", text)

    def test_key_is_not_in_the_shared_config_registry(self):
        # иначе он попал бы в экспорт настроек и в conf/.history
        self.assertNotIn(ssh_access.FILENAME, sc.FILES.values())


class OsReleaseTests(SimpleTestCase):
    def test_os_release(self):
        out = ('NAME="ALT Server"\nVERSION="10.2 (Mendelevium)"\nID=altlinux\nVERSION_ID=10.2\n'
               'PRETTY_NAME="ALT Server 10.2 (Mendelevium)"\n---kernel\nLinux 5.10.212-std-def-alt1\n---host\nmdc-git01\n')
        self.assertEqual(ssh_access.parse_os_release(out), {
            "os": "ALT Server 10.2 (Mendelevium)", "id": "altlinux", "version": "10.2",
            "kernel": "Linux 5.10.212-std-def-alt1", "hostname": "mdc-git01"})

    def test_centos6_has_only_redhat_release(self):
        out = "CentOS release 6.10 (Final)\n---kernel\nLinux 2.6.32-754.el6.x86_64\n---host\nold01\n"
        info = ssh_access.parse_os_release(out)
        self.assertEqual((info["os"], info["id"]), ("CentOS release 6.10 (Final)", ""))

    def test_not_a_linux_shell(self):
        self.assertEqual(ssh_access.parse_os_release("")["os"], "")


class GatewayKeyTests(SimpleTestCase):
    def test_private_key_goes_only_into_ssh_connections(self):
        ssh = remote_gateway.connection_parameters("ssh", "10.0.0.1", username="svc", private_key=ED_KEY,
                                                   passphrase="p")
        self.assertEqual((ssh["private-key"], ssh["passphrase"], ssh["username"]), (ED_KEY, "p", "svc"))
        rdp = remote_gateway.connection_parameters("rdp", "10.0.0.1", private_key=ED_KEY)
        self.assertNotIn("private-key", rdp)
