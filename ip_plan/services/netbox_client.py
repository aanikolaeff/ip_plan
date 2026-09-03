"""Клиент для работы с NetBox REST API (раздел IPAM / IP-адреса)."""
import logging

import requests
import urllib3
from django.conf import settings
from django.utils.text import slugify

logger = logging.getLogger(__name__)

# NETBOX_VERIFY_SSL=False — осознанный выбор (самоподписанный сертификат
# или, как при подключении через host.docker.internal, заведомое несовпадение
# имени с сертификатом). urllib3 на каждый такой запрос пишет
# InsecureRequestWarning — глушим его явно, а не получаем флудом в логах.
if not settings.NETBOX_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NetBoxAPIError(Exception):
    """Ошибка при обращении к NetBox API с понятным для пользователя сообщением."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class NetBoxClient:
    IP_ADDRESSES_PATH = "/api/ipam/ip-addresses/"
    PREFIXES_PATH = "/api/ipam/prefixes/"
    TAGS_PATH = "/api/extras/tags/"
    DEVICES_PATH = "/api/dcim/devices/"
    VIRTUAL_MACHINES_PATH = "/api/virtualization/virtual-machines/"

    # Защита от бесконечного цикла, если NetBox вернёт некорректную пагинацию.
    MAX_PAGES = 500

    def __init__(self):
        self.base_url = settings.NETBOX_URL.rstrip("/")
        self.verify_ssl = settings.NETBOX_VERIFY_SSL
        self.timeout = settings.NETBOX_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {settings.NETBOX_TOKEN}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # --- низкоуровневый HTTP-вызов с единой обработкой ошибок ---

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        logger.info("NetBox API %s %s", method, url)
        try:
            response = self.session.request(
                method, url, timeout=self.timeout, verify=self.verify_ssl, **kwargs
            )
        except requests.exceptions.SSLError as exc:
            logger.error("NetBox SSL error: %s", exc)
            raise NetBoxAPIError(
                "Ошибка проверки SSL-сертификата NetBox. "
                "Проверьте настройку NETBOX_VERIFY_SSL."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("NetBox connection error: %s", exc)
            raise NetBoxAPIError(
                "NetBox недоступен. Проверьте NETBOX_URL и сетевое соединение."
            ) from exc
        except requests.exceptions.Timeout as exc:
            logger.error("NetBox timeout: %s", exc)
            raise NetBoxAPIError("Превышено время ожидания ответа от NetBox.") from exc

        if response.status_code == 403:
            logger.error("NetBox 403 Forbidden: %s", response.text)
            raise NetBoxAPIError(
                "Доступ к NetBox запрещён (403). Проверьте NETBOX_TOKEN и права токена.",
                status_code=403,
            )
        if response.status_code == 404:
            logger.error("NetBox 404 Not Found: %s", url)
            raise NetBoxAPIError("Запись не найдена в NetBox (404).", status_code=404)
        if response.status_code == 400:
            logger.warning("NetBox 400 Bad Request: %s", response.text)
            raise NetBoxAPIError(self._format_validation_errors(response), status_code=400)
        if not response.ok:
            logger.error("NetBox error %s: %s", response.status_code, response.text)
            raise NetBoxAPIError(
                f"Ошибка NetBox API ({response.status_code}).", status_code=response.status_code
            )

        return response

    @staticmethod
    def _format_validation_errors(response):
        try:
            data = response.json()
        except ValueError:
            return "Некорректные данные (400)."
        if isinstance(data, dict):
            parts = []
            for field, errors in data.items():
                text = "; ".join(str(e) for e in errors) if isinstance(errors, list) else str(errors)
                parts.append(f"{field}: {text}")
            return "; ".join(parts)
        return str(data)

    # --- постраничная выборка (общая для любого list-эндпоинта NetBox) ---

    def _list_all(self, path, params=None):
        params = dict(params or {})
        # 1000 — максимум, который отдаёт NetBox за раз (MAX_PAGE_SIZE), запрос
        # большего значения NetBox просто обрежет сам. Чем меньше round-trip'ов,
        # тем меньше риск, что gunicorn-воркер не успеет в свой --timeout.
        limit = params.setdefault("limit", 1000)
        params["offset"] = 0

        results = []
        for _ in range(self.MAX_PAGES):
            response = self._request("GET", path, params=params)
            payload = response.json()
            results.extend(payload.get("results", []))
            if not payload.get("next"):
                break
            params["offset"] += limit
        else:
            logger.warning("Достигнут предел пагинации NetBox (%s страниц) для %s", self.MAX_PAGES, path)

        return results

    # --- IP-адреса ---

    def list_ip_addresses(self, limit=1000):
        """Получить все IP-адреса, обходя постранично пагинацию NetBox (limit/offset)."""
        params = {"limit": limit}
        if settings.NETBOX_SITE_FILTER:
            params["site"] = settings.NETBOX_SITE_FILTER
        if settings.NETBOX_TAG_FILTER:
            params["tag"] = settings.NETBOX_TAG_FILTER
        return self._list_all(self.IP_ADDRESSES_PATH, params)

    def get_ip_address(self, ip_id):
        response = self._request("GET", f"{self.IP_ADDRESSES_PATH}{ip_id}/")
        return response.json()

    def create_ip_address(self, data):
        response = self._request("POST", self.IP_ADDRESSES_PATH, json=data)
        return response.json()

    def update_ip_address(self, ip_id, data):
        response = self._request("PATCH", f"{self.IP_ADDRESSES_PATH}{ip_id}/", json=data)
        return response.json()

    def delete_ip_address(self, ip_id):
        self._request("DELETE", f"{self.IP_ADDRESSES_PATH}{ip_id}/")

    # --- устройства и ВМ (только для колонки "Роль": nested
    # assigned_object.device/virtual_machine в ответе ip-addresses — краткая
    # репрезентация NetBox без поля role, его нужно подтягивать отдельно) ---

    def list_devices(self, limit=1000):
        return self._list_all(self.DEVICES_PATH, {"limit": limit})

    def list_virtual_machines(self, limit=1000):
        return self._list_all(self.VIRTUAL_MACHINES_PATH, {"limit": limit})

    def get_device(self, device_id):
        response = self._request("GET", f"{self.DEVICES_PATH}{device_id}/")
        return response.json()

    def get_virtual_machine(self, vm_id):
        response = self._request("GET", f"{self.VIRTUAL_MACHINES_PATH}{vm_id}/")
        return response.json()

    # --- подсети (префиксы) ---

    def list_prefixes(self, limit=1000):
        """Получить все подсети (IPAM Prefixes) — для фильтра по подсети в таблице."""
        return self._list_all(self.PREFIXES_PATH, {"limit": limit})

    # --- тэги ---

    def resolve_tags(self, tag_names):
        """Найти тэги по имени (создав недостающие) и вернуть список их id."""
        return [self._get_or_create_tag(name) for name in tag_names]

    def _get_or_create_tag(self, name):
        response = self._request("GET", self.TAGS_PATH, params={"name": name})
        results = response.json().get("results", [])
        if results:
            return results[0]["id"]
        response = self._request(
            "POST", self.TAGS_PATH, json={"name": name, "slug": slugify(name)}
        )
        return response.json()["id"]
