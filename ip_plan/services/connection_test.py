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
    cfg = dict((data.get("device_types") or {}).get(raw.get("model"), {}))
    cfg.update(raw)

    host = cfg.get("host")
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
