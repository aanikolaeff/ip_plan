"""Шлюз удалённого доступа: формат auth-json (подпись + AES-128-CBC), адрес
вкладки, параметры соединений, отказы шлюза."""
import base64
import hashlib
import hmac
import json
from unittest import mock

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.test import SimpleTestCase, override_settings

from ip_plan.services import remote_gateway as gw

KEY = "000102030405060708090a0b0c0d0e0f"


def _decrypt(data, key_hex=KEY):
    key = bytes.fromhex(key_hex)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    padded = decryptor.update(base64.b64decode(data)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    signed = unpadder.update(padded) + unpadder.finalize()
    return signed[:32], signed[32:]


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class PayloadTests(SimpleTestCase):
    def test_signature_and_json_survive_round_trip(self):
        payload = {"username": "ivanov", "expires": 1, "connections": {}}
        signature, plain = _decrypt(gw.encrypt_payload(payload, KEY))
        self.assertEqual(json.loads(plain), payload)
        self.assertEqual(signature, hmac.new(bytes.fromhex(KEY), plain, hashlib.sha256).digest())

    def test_key_must_be_128_bit_hex(self):
        for bad in ("", "abc", "zz" * 16, "00" * 32):
            with self.assertRaises(gw.GatewayError) as ctx:
                gw.encrypt_payload({}, bad)
            self.assertEqual(ctx.exception.status, 503)


class ParametersTests(SimpleTestCase):
    def test_ssh_has_no_password_and_default_port(self):
        self.assertEqual(gw.connection_parameters("ssh", "10.0.0.1", username=" root "),
                         {"hostname": "10.0.0.1", "port": "22", "username": "root"})

    def test_rdp_splits_domain_from_username(self):
        params = gw.connection_parameters("rdp", "10.0.0.2", username="CORP\\ivanov")
        self.assertEqual((params["domain"], params["username"]), ("CORP", "ivanov"))
        self.assertEqual(params["ignore-cert"], "true")
        self.assertNotIn("password", params)

    def test_vnc_console_carries_one_time_password_and_port(self):
        self.assertEqual(gw.connection_parameters("vnc", "10.0.0.3", 5902, username="x", password="t1cket"),
                         {"hostname": "10.0.0.3", "port": "5902", "password": "t1cket"})

    def test_unknown_protocol_is_rejected(self):
        with self.assertRaises(gw.GatewayError) as ctx:
            gw.connection_parameters("telnet", "10.0.0.1")
        self.assertEqual(ctx.exception.status, 400)


class NamingTests(SimpleTestCase):
    def test_connection_name_is_ascii(self):
        self.assertEqual(gw.connection_name("ssh", "сервер db-01"), "SSH db-01")
        self.assertEqual(gw.connection_name("vnc", ""), "VNC host")

    def test_client_url_points_at_the_connection(self):
        url = gw.client_url("http://gw/guacamole/", "SSH db-01", "TOKEN")
        ident = url.split("/#/client/")[1].split("?")[0]
        self.assertEqual(base64.b64decode(ident), b"SSH db-01\0c\0json")
        self.assertTrue(url.startswith("http://gw/guacamole/#/client/"))
        self.assertTrue(url.endswith("?token=TOKEN"))


@override_settings(GUACAMOLE_JSON_SECRET_KEY=KEY, GUACAMOLE_INTERNAL_URL="http://guacamole:8080/guacamole",
                   GUACAMOLE_PUBLIC_URL="https://ipplan.local/guac", GUACAMOLE_STAFF_ONLY=True)
class OpenSessionTests(SimpleTestCase):
    def test_exchanges_one_connection_for_token(self):
        with mock.patch.object(gw.requests, "post", return_value=_Resp({"authToken": "ABC"})) as post:
            url = gw.open_session("ivanov", "ssh", "db-01", {"hostname": "10.0.0.1", "port": "22"})
        self.assertEqual(post.call_args.args[0], "http://guacamole:8080/guacamole/api/tokens")
        _, plain = _decrypt(post.call_args.kwargs["data"]["data"])
        payload = json.loads(plain)
        self.assertEqual(payload["username"], "ivanov")
        self.assertEqual(payload["connections"],
                         {"SSH db-01": {"protocol": "ssh", "parameters": {"hostname": "10.0.0.1", "port": "22"}}})
        self.assertTrue(url.startswith("https://ipplan.local/guac/#/client/"))
        self.assertTrue(url.endswith("?token=ABC"))

    def test_gateway_down_is_503(self):
        with mock.patch.object(gw.requests, "post", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(gw.GatewayError) as ctx:
                gw.open_session("ivanov", "ssh", "db-01", {})
        self.assertEqual(ctx.exception.status, 503)

    def test_rejected_key_names_the_setting(self):
        with mock.patch.object(gw.requests, "post", return_value=_Resp(status=403, text="Permission denied")):
            with self.assertRaises(gw.GatewayError) as ctx:
                gw.open_session("ivanov", "ssh", "db-01", {})
        self.assertIn("GUACAMOLE_JSON_SECRET_KEY", str(ctx.exception))

    def test_staff_only_by_default(self):
        staff = mock.Mock(is_authenticated=True, is_staff=True)
        plain = mock.Mock(is_authenticated=True, is_staff=False)
        self.assertTrue(gw.allowed_for(staff))
        self.assertFalse(gw.allowed_for(plain))
        self.assertFalse(gw.allowed_for(None))
        with override_settings(GUACAMOLE_STAFF_ONLY=False):
            self.assertTrue(gw.allowed_for(plain))
        with override_settings(GUACAMOLE_JSON_SECRET_KEY=""):
            self.assertFalse(gw.allowed_for(staff))
