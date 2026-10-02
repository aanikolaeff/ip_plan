"""Шлюз удалённого доступа — SSH / RDP / VNC в браузере через Apache
Guacamole (сервисы `guacd` и `guacamole` в docker-compose.yml, профиль
`gateway`). Пункты «В браузере» меню строки таблицы адресов.

Guacamole поднят с расширением auth-json и БЕЗ базы данных: пользователей и
соединений в нём нет. На каждый клик ip_plan собирает JSON с одним
соединением, подписывает и шифрует его общим ключом
(`GUACAMOLE_JSON_SECRET_KEY`), обменивает на токен сессии Guacamole
(`POST /api/tokens`, сервер-сервер по сети compose) и отдаёт браузеру адрес
вкладки `…/#/client/<id>?token=…`.

Что важно не сломать:

- **Пароли к хостам ip_plan не знает.** В JSON уходят только адрес, порт и
  (необязательно) имя пользователя — логин и пароль Guacamole спрашивает сам
  в своей вкладке. Два исключения: консоль ВМ RedVirt — там пароль
  одноразовый, его выдаёт гипервизор на две минуты (services/vm_console.py),
  и пункт «SSH по ключу» — вход служебной учётной записью и ключом из
  «Настроек» (services/ssh_access.py). Ключ уходит в Guacamole внутри
  зашифрованного JSON, сервер-сервер; в браузер он не попадает, а параметры
  соединения пользователю Guacamole не отдаёт (проверено запросом
  `…/connections/<id>/parameters` — 403).
- **Адрес берётся из NetBox по id записи**, а не из запроса браузера: шлюз
  ходит к хостам с адреса сервера, и открывать через него произвольный
  адрес нельзя.
- **Токен в адресе вкладки, а не только в хранилище браузера.** Guacamole
  держит текущий токен в localStorage, общем для всех вкладок; вторая
  открытая сессия его заменяет. Адрес с собственным токеном и прямой
  ссылкой на соединение позволяет каждой вкладке пережить обновление
  страницы независимо от соседних.

Формат auth-json (guacamole.apache.org/doc/gug/json-auth.html): HMAC-SHA256
от JSON тем же ключом → подпись + JSON → AES-128-CBC с нулевым IV → base64.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import time

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 5
# Сколько живёт зашифрованный JSON. Обмен на токен идёт сразу же, на сервере,
# так что это запас на расхождение часов контейнеров, а не время на клик.
PAYLOAD_TTL = 60
DATA_SOURCE = "json"

PROTOCOLS = ("ssh", "rdp", "vnc")
DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "vnc": 5900}


class GatewayError(Exception):
    """Понятная пользователю причина, почему сессия не открыта."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def enabled():
    return bool(settings.GUACAMOLE_JSON_SECRET_KEY)


def allowed_for(user):
    """Можно ли пользователю открывать сессии через шлюз. Шлюз ходит к хостам
    с адреса сервера, то есть даёт сетевую досягаемость, которой у рабочей
    станции пользователя может и не быть, — по умолчанию только staff."""
    if not enabled() or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_staff) or not settings.GUACAMOLE_STAFF_ONLY


def encrypt_payload(payload, key_hex):
    """JSON → строка для параметра `data` расширения auth-json."""
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        key = b""
    if len(key) != 16:
        raise GatewayError("GUACAMOLE_JSON_SECRET_KEY должен быть 128-битным ключом — 32 шестнадцатеричных символа",
                           status=503)
    plain = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signed = hmac.new(key, plain, hashlib.sha256).digest() + plain
    padder = padding.PKCS7(128).padder()
    padded = padder.update(signed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def connection_name(protocol, label):
    """Имя соединения — оно же его идентификатор в Guacamole и заголовок
    вкладки. Только ASCII: идентификатор уходит в адрес через base64."""
    label = re.sub(r"[^\x21-\x7e]+", "_", label or "").strip("_") or "host"
    return f"{protocol.upper()} {label}"


def client_url(public_url, name, token):
    """Прямая ссылка на соединение: идентификатор клиента Guacamole — base64
    от «имя NUL c NUL источник» (c — connection)."""
    ident = base64.b64encode("\0".join((name, "c", DATA_SOURCE)).encode("utf-8")).decode("ascii")
    return f"{public_url.rstrip('/')}/#/client/{ident}?token={token}"


def connection_parameters(protocol, host, port=None, username="", password="", private_key="", passphrase=""):
    """Параметры соединения Guacamole. Чего здесь нет (логин, пароль, домен) —
    то Guacamole спросит у пользователя сам."""
    if protocol not in PROTOCOLS:
        raise GatewayError(f"Протокол «{protocol}» шлюзом не поддерживается", status=400)
    params = {"hostname": host, "port": str(port or DEFAULT_PORTS[protocol])}
    username = (username or "").strip()
    if protocol == "rdp":
        # security=any — договориться с хостом о лучшем (NLA, если требует);
        # сертификаты у хостов самоподписанные.
        params.update({"security": "any", "ignore-cert": "true", "resize-method": "display-update"})
        if "\\" in username:
            params["domain"], username = username.split("\\", 1)
    if username and protocol != "vnc":
        params["username"] = username
    if password:
        params["password"] = password
    if private_key and protocol == "ssh":
        params["private-key"] = private_key
        if passphrase:
            params["passphrase"] = passphrase
    return params


def open_session(user, protocol, label, parameters):
    """Выпустить токен Guacamole на одно соединение; возвращает адрес вкладки."""
    if not enabled():
        raise GatewayError("Шлюз удалённого доступа не настроен (GUACAMOLE_JSON_SECRET_KEY)", status=503)
    name = connection_name(protocol, label)
    payload = {
        "username": user,
        "expires": int((time.time() + PAYLOAD_TTL) * 1000),
        "connections": {name: {"protocol": protocol, "parameters": parameters}},
    }
    data = encrypt_payload(payload, settings.GUACAMOLE_JSON_SECRET_KEY)
    url = f"{settings.GUACAMOLE_INTERNAL_URL.rstrip('/')}/api/tokens"
    try:
        r = requests.post(url, data={"data": data}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Шлюз удалённого доступа недоступен (%s): %s", url, exc)
        raise GatewayError("Шлюз удалённого доступа недоступен — контейнер guacamole не запущен "
                           "или не отвечает", status=503)
    if r.status_code == 403:
        # Либо ключи у ip_plan и Guacamole разные, либо часы контейнеров
        # разошлись больше чем на PAYLOAD_TTL.
        logger.error("Шлюз отклонил токен: HTTP 403 %s", r.text[:200])
        raise GatewayError("Шлюз отклонил запрос — ключ GUACAMOLE_JSON_SECRET_KEY не совпадает "
                           "с ключом контейнера guacamole")
    try:
        r.raise_for_status()
        token = r.json()["authToken"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.error("Шлюз вернул неожиданный ответ: HTTP %s %s (%s)", r.status_code, r.text[:200], exc)
        raise GatewayError(f"Шлюз вернул неожиданный ответ (HTTP {r.status_code})")
    return client_url(settings.GUACAMOLE_PUBLIC_URL, name, token)
