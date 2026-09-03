"""Живые сетевые проверки IP-адресов: ping (fping), сканирование портов
(nmap), PTR-резолв DNS. В отличие от netbox_client.py, этот модуль не
обращается к NetBox — он работает напрямую с самим IP-адресом по сети.
"""
import logging
import re
import socket
import subprocess

import dns.exception
import dns.resolver
import dns.reversename
from django.conf import settings

logger = logging.getLogger(__name__)


class NetworkToolError(Exception):
    """Ошибка запуска/выполнения внешней сетевой утилиты (fping/nmap)."""


_FPING_LINE_RE = re.compile(r"^(\S+)\s+is\s+(alive|unreachable)(?:\s+\(([\d.]+)\s*ms\))?")


def ping_check(address, timeout_ms=1000, retries=1):
    """Проверка доступности через fping. Возвращает (alive: bool, latency_ms: float|None)."""
    cmd = ["fping", "-e", "-r", str(retries), "-t", str(timeout_ms), address]
    logger.info("ping_check: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000 + 1) * (retries + 1) + 3,
        )
    except subprocess.TimeoutExpired as exc:
        raise NetworkToolError("Превышено время ожидания fping.") from exc
    except FileNotFoundError as exc:
        raise NetworkToolError("Утилита fping не найдена в контейнере.") from exc

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in output.splitlines():
        match = _FPING_LINE_RE.match(line.strip())
        if match:
            _, state, ms = match.groups()
            return state == "alive", float(ms) if ms else None
    return False, None


def scan_ports(address, port_range="1-1000", host_timeout="25s"):
    """Сканирование портов через nmap. Возвращает отсортированный список
    открытых портов (int). -Pn — не пропускать скан, если хост не ответил
    на ICMP (порты могут быть доступны даже при заблокированном ping).

    -T3 (Normal, дефолтный профиль nmap) вместо -T4 (Aggressive) — после
    инцидента, когда скан внутреннего адреса на -T4 вызвал временную потерю
    сетевого соединения (похоже на реакцию IDS/IPS на агрессивный трафик).
    -T2 (Polite) был бы ещё безопаснее, но сканирует строго последовательно
    с задержкой ≥0.4с между портами — 1000 портов могли бы занять минуты,
    что не укладывается в синхронный HTTP-запрос."""
    cmd = [
        "nmap", "-p", port_range, "-T3", "-Pn",
        "--host-timeout", host_timeout,
        "--max-retries", "1",
        "-oG", "-",
        address,
    ]
    logger.info("scan_ports: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=50)
    except subprocess.TimeoutExpired as exc:
        raise NetworkToolError("Превышено время ожидания nmap.") from exc
    except FileNotFoundError as exc:
        raise NetworkToolError("Утилита nmap не найдена в контейнере.") from exc

    ports = []
    for line in result.stdout.splitlines():
        if not line.startswith("Host:") or "Ports:" not in line:
            continue
        ports_field = line.split("Ports:", 1)[1].split("\t", 1)[0]
        for entry in ports_field.split(","):
            parts = entry.strip().split("/")
            if len(parts) >= 2 and parts[1] == "open":
                try:
                    ports.append(int(parts[0]))
                except ValueError:
                    continue
    return sorted(set(ports))


def _ptr_via_dns_server(address, server):
    try:
        rev = dns.reversename.from_address(address)
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [server]
        resolver.lifetime = 3
        answers = resolver.resolve(rev, "PTR")
        name = str(answers[0]).rstrip(".")
        return name or None
    except (dns.exception.DNSException, OSError):
        return None


def _ptr_via_system(address, timeout=3):
    # У socket.gethostbyaddr() нет параметра timeout — без явного лимита
    # системный резолвер может зависнуть на неопределённое время (дольше,
    # чем --timeout у gunicorn), если запись не существует и upstream DNS
    # не отвечает вовсе. Ограничиваем глобальный socket-таймаут на время
    # этого вызова (синхронный воркер обрабатывает один запрос за раз).
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        name, _, _ = socket.gethostbyaddr(address)
        return name if name and name != address else None
    except OSError:
        return None
    finally:
        socket.setdefaulttimeout(previous)


def resolve_ptr(address):
    """PTR-резолв: сначала по серверам из NETBOX_DNS_SERVERS (по порядку,
    первый полезный ответ), затем системный DNS как fallback. Имена,
    подходящие под NETBOX_DNS_SKIP_PATTERN, считаются бесполезными."""
    skip_pattern = settings.NETBOX_DNS_SKIP_PATTERN
    skip_re = re.compile(skip_pattern, re.IGNORECASE) if skip_pattern else None

    def is_useful(name):
        if not name:
            return False
        if skip_re and skip_re.search(name):
            return False
        return True

    for server in settings.NETBOX_DNS_SERVERS:
        name = _ptr_via_dns_server(address, server)
        if is_useful(name):
            return name

    name = _ptr_via_system(address)
    return name if is_useful(name) else None
