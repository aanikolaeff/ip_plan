"""Консоль виртуальной машины из гипервизора — для пункта «Консоль ВМ» в
меню строки таблицы адресов.

Источники — те же, что у синхронизации (`conf/sources_redvirt.json`,
`conf/sources_vcenter.json`, читаются через sync_config), поэтому отдельных
учёток у ip_plan нет. Что отдаём браузеру:

- RedVirt/oVirt: `POST .../graphicsconsoles/<id>/remoteviewerconnectionfile`
  возвращает готовый файл .vv (INI с хостом, портом и одноразовым билетом) —
  его открывает remote-viewer (virt-viewer). Протокол — какой настроен у
  машины: SPICE предпочтительнее, иначе VNC. Файл выдаётся только у
  запущенной ВМ.
- vCenter: ищем ВМ по имени через легаси REST (`/rest/vcenter/vm`, как в
  netbox_vcenter_sync.py — новый /api/session на нашем сервере отвечает 500)
  и отдаём ссылку `vmrc://<vcenter>/?moid=<vm-id>` для VMware Remote Console.
  Билет клонирования сессии не берём: пользователь входит в VMRC под своей
  учёткой, а не под сервисной учёткой синка.

Какой гипервизор спрашивать, решает тип кластера ВМ в NetBox (`red-virt` /
`vcenter`, slug'и из синков); без кластера перебираем оба списка.

Второй режим (`console_for_vm(..., web=True)`) — консоль в браузере через
шлюз Guacamole: вместо файла .vv отдаём адрес, порт и одноразовый пароль
VNC-консоли RedVirt (`ovirt_vnc`), сессию открывает services/remote_gateway.py.
"""
import logging
import re
from urllib.parse import urlparse

import requests

from . import sync_config

logger = logging.getLogger(__name__)

TIMEOUT = 10
VNC_TICKET_TTL = 120
OVIRT_TYPE = "red-virt"
VCENTER_TYPE = "vcenter"


class ConsoleError(Exception):
    """Понятная пользователю причина, почему консоль не выдана."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _safe_name(name):
    return re.sub(r"[^\w.-]+", "_", name or "vm") or "vm"


def _sources(key):
    try:
        data, _ = sync_config.load(key)
    except Exception as exc:  # файла нет / битый JSON — источник просто пропускаем
        logger.warning("Не удалось прочитать %s: %s", key, exc)
        return []
    return [e for e in (data or []) if isinstance(e, dict) and e.get("url")]


# ---------------------------------------------------------------- oVirt

def _ovirt_get(base, auth, path, **params):
    r = requests.get(f"{base}{path}", params=params or None, auth=auth, verify=False, timeout=TIMEOUT,
                     headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _ovirt_running_vm(base, auth, vm_name):
    """ВМ по имени; None — в этом источнике её нет. Незапущенная — отказ:
    консоль есть только у работающей машины."""
    vms = _ovirt_get(base, auth, "/vms", search=f"name={vm_name}").get("vm") or []
    vm = next((v for v in vms if v.get("name") == vm_name), None)
    if vm is None:
        return None
    status = vm.get("status") or ""
    if status != "up":
        raise ConsoleError(f"ВМ «{vm_name}» не запущена (состояние {status or 'неизвестно'}) — "
                           "консоль есть только у работающей машины", status=409)
    return vm


def ovirt_console(source, vm_name):
    """Файл .vv для ВМ `vm_name` из одного RedVirt/oVirt-источника; None —
    ВМ в этом источнике нет."""
    base = source["url"].rstrip("/")
    auth = (source.get("user") or "", source.get("password") or "")
    vm = _ovirt_running_vm(base, auth, vm_name)
    if vm is None:
        return None
    consoles = _ovirt_get(base, auth, f"/vms/{vm['id']}/graphicsconsoles").get("graphics_console") or []
    if not consoles:
        raise ConsoleError(f"У ВМ «{vm_name}» нет графической консоли (headless)", status=409)
    console = sorted(consoles, key=lambda c: 0 if c.get("protocol") == "spice" else 1)[0]
    r = requests.post(
        f"{base}/vms/{vm['id']}/graphicsconsoles/{console['id']}/remoteviewerconnectionfile",
        json={}, auth=auth, verify=False, timeout=TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    content = (r.json() or {}).get("remote_viewer_connection_file") or ""
    if "[virt-viewer]" not in content:
        raise ConsoleError(f"Гипервизор не вернул файл подключения для «{vm_name}»")
    protocol = (console.get("protocol") or "spice").upper()
    return {
        "kind": "file",
        "filename": f"{_safe_name(vm_name)}.vv",
        "mime": "application/x-virt-viewer",
        "content": content,
        "protocol": protocol,
        "source": source.get("name") or urlparse(base).hostname,
        "message": f"Консоль {protocol} «{vm_name}»: файл .vv скачан, откройте его в remote-viewer",
    }


def ovirt_vnc(source, vm_name):
    """Реквизиты VNC-консоли ВМ для шлюза (services/remote_gateway.py): адрес
    и порт на хосте гипервизора плюс одноразовый пароль. None — ВМ в этом
    источнике нет.

    Адрес и порт есть только в «текущем» представлении консоли
    (`?current=true`). Пароль выдаёт `…/ticket`; он годится для подключения
    VNC_TICKET_TTL секунд — уже открытая сессия живёт дальше, но обновить
    вкладку шлюза позже этого срока не выйдет, консоль открывают заново."""
    base = source["url"].rstrip("/")
    auth = (source.get("user") or "", source.get("password") or "")
    vm = _ovirt_running_vm(base, auth, vm_name)
    if vm is None:
        return None
    consoles = _ovirt_get(base, auth, f"/vms/{vm['id']}/graphicsconsoles",
                          current="true").get("graphics_console") or []
    console = next((c for c in consoles if c.get("protocol") == "vnc"), None)
    if console is None:
        if consoles:
            raise ConsoleError(f"У ВМ «{vm_name}» только консоль SPICE — в браузере её не открыть; "
                               "используйте «Консоль ВМ» (файл .vv) либо включите у машины графику VNC",
                               status=409)
        raise ConsoleError(f"У ВМ «{vm_name}» нет графической консоли (headless)", status=409)
    if not console.get("address") or not console.get("port"):
        raise ConsoleError(f"Гипервизор не сообщил адрес VNC-консоли «{vm_name}»")
    r = requests.post(
        f"{base}/vms/{vm['id']}/graphicsconsoles/{console['id']}/ticket",
        json={"ticket": {"expiry": VNC_TICKET_TTL}}, auth=auth, verify=False, timeout=TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    password = ((r.json() or {}).get("ticket") or {}).get("value") or ""
    if not password:
        raise ConsoleError(f"Гипервизор не выдал билет VNC для «{vm_name}»")
    return {
        "kind": "vnc",
        "host": console["address"],
        "port": int(console["port"]),
        "password": password,
        "protocol": "VNC",
        "source": source.get("name") or urlparse(base).hostname,
    }


# -------------------------------------------------------------- vCenter

def vcenter_console(source, vm_name):
    """Ссылка vmrc:// для ВМ `vm_name` из одного vCenter; None — ВМ нет."""
    base = source["url"].rstrip("/")
    auth = (source.get("user") or "", source.get("password") or "")
    r = requests.post(f"{base}/rest/com/vmware/cis/session", auth=auth, verify=False, timeout=TIMEOUT)
    r.raise_for_status()
    token = (r.json() or {}).get("value")
    headers = {"vmware-api-session-id": token}
    try:
        r = requests.get(f"{base}/rest/vcenter/vm", params={"filter.names": vm_name},
                         headers=headers, verify=False, timeout=TIMEOUT)
        r.raise_for_status()
        vms = (r.json() or {}).get("value") or []
    finally:
        try:
            requests.delete(f"{base}/rest/com/vmware/cis/session", headers=headers, verify=False, timeout=TIMEOUT)
        except requests.RequestException:
            pass
    vm = next((v for v in vms if v.get("name") == vm_name), None)
    if vm is None:
        return None
    host = urlparse(base).hostname
    return {
        "kind": "url",
        "url": f"vmrc://{host}/?moid={vm['vm']}",
        "protocol": "VMRC",
        "source": source.get("name") or host,
        "message": f"Консоль «{vm_name}»: открываю VMware Remote Console (вход под своей учёткой)",
    }


# ---------------------------------------------------------------- общее

def _ordered(sources, site):
    """Источники площадки ВМ — первыми."""
    return sorted(sources, key=lambda s: 0 if site and s.get("site") == site else 1)


def console_for_vm(vm, cluster=None, web=False):
    """Консоль для ВМ NetBox (dict из API). cluster — объект кластера с
    type.slug и site.slug, если удалось получить. Возвращает dict для
    браузера либо None, если ВМ не нашлась ни в одном источнике.

    web=True — реквизиты VNC для шлюза (ovirt_vnc) вместо файла/ссылки для
    локального клиента. Только RedVirt: консоль vCenter через шлюз не
    открывается (там свой протокол, WebMKS)."""
    name = vm.get("name") or ""
    if not name:
        raise ConsoleError("У виртуальной машины нет имени", status=400)
    ctype = (((cluster or {}).get("type") or {}).get("slug") or "").lower()
    site = ((cluster or {}).get("site") or vm.get("site") or {}) or {}
    site = site.get("slug") if isinstance(site, dict) else None

    if web and ctype == VCENTER_TYPE:
        raise ConsoleError(f"ВМ «{name}» — на vCenter: её консоль в браузере не открывается, "
                           "используйте «Консоль ВМ» (VMware Remote Console)", status=409)
    plan = []
    if ctype in ("", OVIRT_TYPE):
        plan += [(ovirt_vnc if web else ovirt_console, s) for s in _ordered(_sources("sources_redvirt"), site)]
    if ctype in ("", VCENTER_TYPE) and not web:
        plan += [(vcenter_console, s) for s in _ordered(_sources("sources_vcenter"), site)]
    if not plan:
        raise ConsoleError("Источники гипервизоров не настроены (Настройки → Гипервизоры)", status=503)

    last_error = None
    for fn, source in plan:
        try:
            result = fn(source, name)
        except ConsoleError:
            raise
        except requests.exceptions.Timeout:
            last_error = f"{source.get('name') or source['url']}: таймаут"
            logger.warning("Консоль %s: %s", name, last_error)
            continue
        except requests.RequestException as exc:
            last_error = f"{source.get('name') or source['url']}: {exc}"
            logger.warning("Консоль %s: %s", name, last_error)
            continue
        if result:
            return result
    if last_error:
        raise ConsoleError(f"ВМ «{name}» не найдена; часть источников недоступна ({last_error})")
    return None
