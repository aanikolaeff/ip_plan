"""Проверка доступности устройств и гипервизоров по кнопке «Тест».

Проверка идёт в отдельном потоке, потому что в HTTP-запросе её делать
нельзя: gunicorn запущен с --timeout 60, а SSH к недоступному устройству
съедает этот лимит один. Состояние и шаги пишутся в ConnectionTest — в
память процесса их класть нельзя, воркеров несколько.

Очередь задач (Celery, RQ) для этого не заводится сознательно: проверка
длится секунды и запускается руками. Плата за простоту — оборванный
перезапуском контейнера тест остаётся в базе со статусом «выполняется»,
поэтому такие записи считаются сорванными по таймауту (см. STALE_AFTER).

Секреты в шаги не попадают: в detail пишутся адрес, порт, код ответа и текст
ошибки, но никогда — пароль, токен или заголовок авторизации.
"""
import logging
import socket
import threading
import time
from datetime import timedelta
from urllib.parse import urlparse

import requests
from django.db import connection
from django.utils import timezone

from . import network_tools, sync_config as sc
from ..models import ConnectionTest

logger = logging.getLogger(__name__)

# Сколько всего отводится на проверку одного объекта.
TOTAL_TIMEOUT = 60
# Шаговые таймауты.
PING_TIMEOUT_MS = 1000
PING_RETRIES = 2
TCP_TIMEOUT = 5
SSH_TIMEOUT = 15
API_TIMEOUT = 15
# SNMPv3 (Континент): агент молчит и на неверные реквизиты, и на адрес не из
# списка разрешённых хостов, так что таймаут здесь — единственный сигнал.
SNMP_TIMEOUT = 8

# Запись «выполняется» старше этого времени считается сорванной: скорее всего
# контейнер перезапустили в середине проверки и поток умер вместе с ним.
STALE_AFTER = timedelta(seconds=TOTAL_TIMEOUT * 3)

# Сколько проверок разрешено одновременно. Каждая держит поток и открывает
# сетевые соединения; на веб-процессе это не должно превращаться в нагрузку.
MAX_PARALLEL = 4

STEP_OK = "ok"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


class TestBusy(Exception):
    """Запуск отклонён: тест уже идёт или превышен лимит параллельных."""


# ── Шаги ─────────────────────────────────────────────────────────────────────

def _step(name, status, detail, elapsed):
    return {"name": name, "status": status, "detail": detail, "elapsed": round(elapsed, 2)}


def _check_icmp(host):
    started = time.monotonic()
    try:
        alive, ms = network_tools.ping_check(host, timeout_ms=PING_TIMEOUT_MS, retries=PING_RETRIES)
    except Exception as e:                      # fping может отсутствовать
        return _step("ICMP", STEP_SKIPPED, f"проверка недоступна: {type(e).__name__}",
                     time.monotonic() - started)
    if alive:
        detail = f"{host} отвечает" + (f", {ms:.1f} мс" if ms else "")
        return _step("ICMP", STEP_OK, detail, time.monotonic() - started)
    # Закрытый ICMP — обычная политика, не повод считать устройство мёртвым.
    return _step("ICMP", STEP_SKIPPED, f"{host} не отвечает на ping (может быть закрыт политикой)",
                 time.monotonic() - started)


def _check_tcp(host, port, label):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            pass
    except socket.timeout:
        return _step(label, STEP_FAILED, f"{host}:{port} — таймаут {TCP_TIMEOUT} с",
                     time.monotonic() - started)
    except OSError as e:
        return _step(label, STEP_FAILED, f"{host}:{port} — {e.strerror or type(e).__name__}",
                     time.monotonic() - started)
    return _step(label, STEP_OK, f"{host}:{port} открыт", time.monotonic() - started)


def _check_ssh(cfg):
    """Вход по SSH с учётными данными устройства.

    Только подключение и закрытие сессии: никаких команд на боевом железе
    проверка не выполняет.
    """
    started = time.monotonic()
    try:
        import paramiko
    except ImportError:
        return _step("SSH", STEP_SKIPPED, "модуль paramiko недоступен",
                     time.monotonic() - started)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=cfg["host"], port=int(cfg.get("port") or 22),
        username=cfg.get("username") or "", password=cfg.get("password") or "",
        look_for_keys=False, allow_agent=False, timeout=SSH_TIMEOUT,
        banner_timeout=SSH_TIMEOUT, auth_timeout=SSH_TIMEOUT,
    )
    # Старое оборудование (xFW, Eltex) не умеет современные алгоритмы — тот же
    # приём, что в netbox-sync.
    if cfg.get("legacy_crypto"):
        kwargs["disabled_algorithms"] = dict(pubkeys=["rsa-sha2-256", "rsa-sha2-512"])
    try:
        client.connect(**kwargs)
    except paramiko.AuthenticationException:
        return _step("SSH", STEP_FAILED, "учётные данные отклонены устройством",
                     time.monotonic() - started)
    except paramiko.SSHException as e:
        return _step("SSH", STEP_FAILED, f"ошибка протокола: {e}", time.monotonic() - started)
    except OSError as e:
        return _step("SSH", STEP_FAILED, f"нет соединения: {e.strerror or type(e).__name__}",
                     time.monotonic() - started)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return _step("SSH", STEP_OK, f"вход под «{cfg.get('username')}» принят",
                 time.monotonic() - started)


def _check_snmp(cfg, host, port):
    """SNMP GET sysName/sysDescr с реквизитами из раздела «SNMP» устройства.

    v2c — первая community из списка (у кластеров ViPNet она адресует активную
    ноду), v3 — пользователь с паролями. Только чтение двух скаляров.
    Различить «нет маршрута», «адрес не в списке разрешённых хостов» и
    «неверные реквизиты» по ответу нельзя: агент молчит во всех трёх случаях
    (у Континента — по документации, у Cisco с ACL — по факту), и текст шага
    об этом говорит прямо.
    """
    started = time.monotonic()
    try:
        import asyncio
        import warnings
        from puresnmp import Client, PyWrapper, V2C
        from puresnmp.credentials import V3, Auth, Priv
    except ImportError:
        return _step("SNMP", STEP_SKIPPED, "модуль puresnmp недоступен", time.monotonic() - started)
    # DES в SNMPv3 — это TripleDES с 8-байтным ключом по RFC 3414; cryptography
    # предупреждает об устаревшем API на каждый пакет. Шум в логе, не проблема.
    warnings.filterwarnings("ignore", module=r"puresnmp_plugins\.priv\.des")

    version = sc.snmp_version(cfg)
    if version == "v2c":
        communities = cfg.get("snmp_communities") or []
        if isinstance(communities, str):
            communities = [c.strip() for c in communities.split(",") if c.strip()]
        if not communities:
            return _step("SNMP", STEP_FAILED, "SNMP v2c: community не задана", time.monotonic() - started)
        creds, label = V2C(communities[0]), "v2c"
    elif version == "v3":
        user = cfg.get("snmp_user") or ""
        if not user or not cfg.get("snmp_auth_password"):
            return _step("SNMP", STEP_FAILED, "SNMP v3: не заданы имя безопасности или пароль аутентификации",
                         time.monotonic() - started)
        auth = Auth(cfg["snmp_auth_password"].encode(), (cfg.get("snmp_auth_proto") or "md5").lower())
        priv = None
        if cfg.get("snmp_priv_password"):
            priv = Priv(cfg["snmp_priv_password"].encode(), (cfg.get("snmp_priv_proto") or "des").lower())
        creds, label = V3(user, auth=auth, priv=priv), f"v3 {'authPriv' if priv else 'authNoPriv'} «{user}»"
    else:
        return _step("SNMP", STEP_SKIPPED, "раздел SNMP не заполнен", time.monotonic() - started)

    async def _get():
        client = PyWrapper(Client(host, creds, port=port))
        return await client.multiget(["1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.1.0"])   # sysName, sysDescr

    def _text(value):
        return value.decode("utf-8", "replace").strip() if isinstance(value, bytes) else str(value).strip()

    try:
        sysname, sysdescr = asyncio.run(asyncio.wait_for(_get(), timeout=SNMP_TIMEOUT))
    except asyncio.TimeoutError:
        return _step("SNMP", STEP_FAILED,
                     f"{host}:{port}/udp — нет ответа за {SNMP_TIMEOUT} с: нет маршрута, адрес "
                     f"проверяющего не в списке разрешённых хостов SNMP на устройстве или "
                     f"неверные реквизиты (агент молчит во всех трёх случаях)",
                     time.monotonic() - started)
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        if "Digest" in text or "UnknownUser" in text or "Decrypt" in text:
            text = "реквизиты SNMP v3 отклонены устройством"
        return _step("SNMP", STEP_FAILED, text, time.monotonic() - started)
    detail = f"{label}: {_text(sysname) or '(sysName пуст)'}"
    descr = _text(sysdescr)
    if descr:
        detail += f" — {descr[:70]}"
    return _step("SNMP", STEP_OK, detail, time.monotonic() - started)


def _check_api(url, user, password, label, verify=False):
    """HTTP-проверка API гипервизора. Пароль в шаги не попадает."""
    started = time.monotonic()
    try:
        r = requests.get(url, auth=(user, password), timeout=API_TIMEOUT,
                         verify=verify, headers={"Accept": "application/json"})
    except requests.exceptions.SSLError as e:
        return _step(label, STEP_FAILED, f"TLS: {str(e)[:90]}", time.monotonic() - started)
    except requests.exceptions.Timeout:
        return _step(label, STEP_FAILED, f"таймаут {API_TIMEOUT} с", time.monotonic() - started)
    except requests.RequestException as e:
        return _step(label, STEP_FAILED, f"{type(e).__name__}", time.monotonic() - started)

    if r.status_code in (401, 403):
        return _step(label, STEP_FAILED, f"HTTP {r.status_code}: учётные данные отклонены",
                     time.monotonic() - started)
    if r.status_code >= 400:
        return _step(label, STEP_FAILED, f"HTTP {r.status_code}", time.monotonic() - started)
    return _step(label, STEP_OK, f"HTTP {r.status_code}", time.monotonic() - started)


# ── Сценарии по типам объектов ───────────────────────────────────────────────

def _run_device(key):
    data, _ = sc.load("devices")
    raw = (data.get("devices") or {}).get(key)
    if raw is None:
        raise LookupError(f"Устройства «{key}» нет в конфигурации")
    cfg = sc.effective(raw, data.get("device_types") or {})

    host = cfg.get("host")

    # Континент: SNMPv3 вместо TCP+SSH. UDP-порт нечем «прощупать» отдельно,
    # так что после ICMP сразу GET с реквизитами.
    if sc.transport(cfg.get("interfaces_parser", "")) == "snmp":
        if cfg.get("cluster_peer_of") and not host:
            return [_step("Проверка", STEP_SKIPPED,
                          f"инвентарная запись резервной платформы для «{cfg['cluster_peer_of']}»: "
                          f"своего подключения нет, данные приходят с активной ноды", 0)], ""
        if not host:
            return [_step("Проверка", STEP_FAILED, "у устройства не задан адрес", 0)], "адрес не задан"
        port = int(cfg.get("snmp_port") or 161)
        steps = [_check_icmp(host), _check_snmp(cfg, host, port)]
        if steps[-1]["status"] == STEP_FAILED:
            return steps, steps[-1]["detail"]
        return steps, ""

    port = int(cfg.get("port") or 22)
    if not host:
        return [_step("Проверка", STEP_FAILED, "у устройства не задан адрес", 0)], \
               "адрес не задан"

    steps = [_check_icmp(host)]
    tcp = _check_tcp(host, port, "TCP")
    steps.append(tcp)
    if tcp["status"] != STEP_OK:
        return steps, f"порт {port} недоступен"

    ssh = _check_ssh(cfg)
    steps.append(ssh)
    if ssh["status"] == STEP_FAILED:
        return steps, ssh["detail"]
    # Раздел «SNMP» заполнен — значит, на него рассчитывают, и он проверяется
    # наравне с SSH: молчащий агент при настроенной community — ошибка, а не
    # «ну и ладно».
    if sc.snmp_version(cfg):
        snmp = _check_snmp(cfg, host, int(cfg.get("snmp_port") or 161))
        steps.append(snmp)
        if snmp["status"] == STEP_FAILED:
            return steps, snmp["detail"]
    return steps, ""


def _run_source(kind, key):
    conf_key = f"sources_{kind}"
    data, _ = sc.load(conf_key)
    entry = next((e for e in data if _source_label(e) == key), None)
    if entry is None:
        raise LookupError(f"Источника «{key}» нет в конфигурации")

    url = entry.get("url") or ""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return [_step("Проверка", STEP_FAILED, f"адрес не разбирается: {url}", 0)], "неверный адрес"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    steps = [_check_icmp(host), _check_tcp(host, port, "TCP")]
    if steps[-1]["status"] != STEP_OK:
        return steps, f"порт {port} недоступен"

    api = _check_api(url, entry.get("user") or "", entry.get("password") or "", "API")
    steps.append(api)
    if api["status"] == STEP_FAILED:
        return steps, api["detail"]
    return steps, ""


def _source_label(entry):
    return entry.get("name") or entry.get("url") or ""


# ── Запуск и состояние ───────────────────────────────────────────────────────

def _finish(pk, status, steps, summary):
    ConnectionTest.objects.filter(pk=pk).update(
        status=status, steps=steps, summary=summary[:300], finished_at=timezone.now(),
    )


def _worker(pk, kind, key):
    """Тело фонового потока. Django открывает соединение с базой на поток и
    сам его не закрывает (это делает только обработчик конца HTTP-запроса),
    поэтому закрываем явно: на PostgreSQL каждый завершившийся поток иначе
    оставлял бы за собой открытое соединение до перезапуска процесса."""
    try:
        _run_and_finish(pk, kind, key)
    finally:
        connection.close()


def _run_and_finish(pk, kind, key):
    try:
        if kind == "device":
            steps, failure = _run_device(key)
        else:
            steps, failure = _run_source(kind, key)
    except LookupError as e:
        _finish(pk, ConnectionTest.STATUS_FAILED, [], str(e))
        return
    except sc.SyncConfigError as e:
        _finish(pk, ConnectionTest.STATUS_FAILED, [], str(e))
        return
    except Exception as e:                       # неожиданное — не теряем
        logger.exception("Проверка %s:%s упала", kind, key)
        _finish(pk, ConnectionTest.STATUS_FAILED, [], f"{type(e).__name__}: {e}")
        return

    status = ConnectionTest.STATUS_FAILED if failure else ConnectionTest.STATUS_OK
    _finish(pk, status, steps, failure or "проверка пройдена")


def sweep_stale():
    """Помечает сорванными проверки, пережившие перезапуск контейнера."""
    edge = timezone.now() - STALE_AFTER
    stale = ConnectionTest.objects.filter(status=ConnectionTest.STATUS_RUNNING,
                                          started_at__lt=edge)
    for test in stale:
        test.status = ConnectionTest.STATUS_FAILED
        test.summary = "проверка прервана (перезапуск приложения) — запустите заново"
        test.finished_at = timezone.now()
        test.save(update_fields=["status", "summary", "finished_at"])


def start(kind, key, username=""):
    """Запускает проверку в фоне. Возвращает запись ConnectionTest."""
    sweep_stale()

    existing = ConnectionTest.objects.filter(target_kind=kind, target_key=key).first()
    if existing and existing.is_running:
        raise TestBusy("Проверка этого объекта уже идёт")

    running = ConnectionTest.objects.filter(status=ConnectionTest.STATUS_RUNNING).count()
    if running >= MAX_PARALLEL:
        raise TestBusy(
            f"Одновременно выполняется {running} проверок — дождитесь окончания"
        )

    if existing:
        existing.status = ConnectionTest.STATUS_RUNNING
        existing.started_at = timezone.now()
        existing.finished_at = None
        existing.started_by = username
        # steps и summary прошлого прогона намеренно не чистим: пока идёт новая
        # проверка, кнопка продолжает показывать предыдущий результат.
        existing.save(update_fields=["status", "started_at", "finished_at", "started_by"])
        test = existing
    else:
        test = ConnectionTest.objects.create(
            target_kind=kind, target_key=key,
            status=ConnectionTest.STATUS_RUNNING, started_by=username,
        )

    thread = threading.Thread(target=_worker, args=(test.pk, kind, key),
                              name=f"conn-test-{kind}-{key}", daemon=True)
    thread.start()
    return test


def state_for(kind, keys):
    """{ключ: ConnectionTest} — для отрисовки кнопок списком, одним запросом."""
    sweep_stale()
    found = ConnectionTest.objects.filter(target_kind=kind, target_key__in=list(keys))
    return {t.target_key: t for t in found}
