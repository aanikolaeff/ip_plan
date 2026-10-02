"""Доступ к хостам по SSH: служебная учётная запись и её закрытый ключ.

Раздел «Настройки» → «Доступ к хостам». Один ключ на всё: им входят

- проверка из этого же раздела (`probe` — вход и чтение сведений об ОС);
- шлюз удалённого доступа, пункт «SSH по ключу» (services/remote_gateway.py);
- синк, когда будет опрашивать ОС на самих машинах: файл лежит в
  `netbox-sync/conf/ssh_access.json`, рядом с остальными его настройками.

Чем этот файл отличается от соседних конфигов (sync_config.FILES):

- **в общий реестр не входит** — иначе он попал бы в экспорт настроек и в
  `conf/.history/`, а копиям закрытого ключа на диске и в архивах делать
  нечего. Отсюда своя запись: атомарная, с проверкой хеша, но без истории;
- **права 0600**;
- **ключ в браузер не уходит никогда**, даже маской в поле: форма показывает
  только тип и отпечаток, пустое поле при сохранении значит «не менять».

Формат файла: {"username", "port", "private_key", "passphrase"}.
"""
import hashlib
import io
import json
import logging
import os
import re
import socket

from . import sync_config as sc

logger = logging.getLogger(__name__)

FILENAME = "ssh_access.json"
DEFAULT_PORT = 22
CONNECT_TIMEOUT = 8
COMMAND_TIMEOUT = 10
OUTPUT_LIMIT = 16 * 1024

# Что спрашиваем у машины. Без Python и без sudo; на CentOS 6 и старше
# os-release нет — запасные файлы. `uname` последним: он есть везде.
PROBE_COMMAND = ("cat /etc/os-release 2>/dev/null || cat /etc/redhat-release 2>/dev/null "
                 "|| cat /etc/issue 2>/dev/null; echo ---kernel; uname -sr; echo ---host; hostname")

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@\\-]{0,63}$")


class SshAccessError(sc.SyncConfigError):
    pass


# ── Файл ───────────────────────────────────────────────────────────────────

def path():
    return sc.conf_dir() / FILENAME


def load():
    """(настройки, хеш файла). Файла нет — ({}, ""): ключ просто не задан."""
    try:
        raw = path().read_bytes()
    except FileNotFoundError:
        return {}, ""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SshAccessError(f"{FILENAME}: не разбирается как JSON ({e})")
    if not isinstance(data, dict):
        raise SshAccessError(f"{FILENAME}: ожидался объект")
    return data, hashlib.sha256(raw).hexdigest()


def save(data, expected_hash):
    """Атомарная запись с проверкой на параллельную правку. Без истории
    версий — см. докстринг модуля."""
    if expected_hash is not None and load()[1] != (expected_hash or ""):
        raise sc.ConfigConflict(
            f"{FILENAME} изменился, пока была открыта форма. "
            f"Откройте страницу заново, чтобы не затереть чужие изменения."
        )
    target = path()
    tmp = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    logger.info("Записан %s", target)


def delete():
    try:
        path().unlink()
    except FileNotFoundError:
        pass


def configured(cfg=None):
    if cfg is None:
        try:
            cfg = load()[0]
        except sc.SyncConfigError:
            return False
    return bool(cfg.get("username") and cfg.get("private_key"))


# ── Ключ ───────────────────────────────────────────────────────────────────

def normalize_key(text):
    """Ключ из textarea: переводы строк Windows, пробелы по краям, обязательный
    перевод строки в конце (без него часть библиотек ключ не читает)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text + "\n" if text else ""


def parse_key(text, passphrase=""):
    """Закрытый ключ → объект paramiko. Понятная причина, если не вышло."""
    import paramiko

    text = normalize_key(text)
    if "PRIVATE KEY" not in text:
        if text.startswith(("ssh-", "ecdsa-")):
            raise SshAccessError("Это открытый ключ (.pub). Нужен закрытый — файл без расширения, "
                                 "он начинается со строки «-----BEGIN … PRIVATE KEY-----»")
        if "PuTTY-User-Key-File" in text:
            raise SshAccessError("Это ключ в формате PuTTY (.ppk). Преобразуйте его в формат OpenSSH: "
                                 "PuTTYgen → Conversions → Export OpenSSH key")
        raise SshAccessError("Не похоже на закрытый ключ: нет строки «-----BEGIN … PRIVATE KEY-----»")
    classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]

    def attempt(password):
        """(ключ | None, зашифрован ли). Тип ключа paramiko сам не выбирает —
        перебираем классы."""
        encrypted = False
        for cls in classes:
            try:
                return cls.from_private_key(io.StringIO(text), password=password), encrypted
            except paramiko.PasswordRequiredException:
                encrypted = True
            except (paramiko.SSHException, ValueError, IndexError, TypeError, UnicodeError):
                pass
        return None, encrypted

    # Сначала без фразы: только так paramiko однозначно говорит, что ключ
    # зашифрован. Неверную фразу он от «не тот тип ключа» не отличает.
    pkey, encrypted = attempt(None)
    if pkey is not None:
        return pkey
    if encrypted:
        if not passphrase:
            raise SshAccessError("Ключ защищён парольной фразой: укажите её в поле ниже")
        pkey, _ = attempt(passphrase)
        if pkey is None:
            raise SshAccessError("Ключ защищён парольной фразой: фраза не подошла")
        return pkey
    raise SshAccessError("Ключ не разобран. Поддерживаются Ed25519, ECDSA и RSA в форматах OpenSSH и PEM")


def is_encrypted(text):
    """Защищён ли ключ парольной фразой. Нечитаемый ключ — False: причину
    назовёт parse_key."""
    try:
        parse_key(text)
    except SshAccessError as e:
        return "парольной фразой" in str(e)
    return False


def describe_key(pkey):
    """Что показываем вместо ключа: тип, длина, отпечаток, открытая часть."""
    name = pkey.get_name()
    kind = {"ssh-ed25519": "Ed25519", "ssh-rsa": "RSA"}.get(name, "ECDSA" if name.startswith("ecdsa") else name)
    digest = hashlib.sha256(pkey.asbytes()).digest()
    import base64
    return {
        "kind": kind,
        "bits": pkey.get_bits(),
        "fingerprint": "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("="),
        "public": f"{name} {pkey.get_base64()}",
    }


def summary(cfg):
    """Сведения о сохранённом ключе для страницы; ключ не раскрывается."""
    out = {"username": cfg.get("username", ""), "port": cfg.get("port") or DEFAULT_PORT,
           "has_key": bool(cfg.get("private_key")), "passphrase_mask": sc.masked(cfg.get("passphrase")),
           "key": None, "key_error": ""}
    if out["has_key"]:
        try:
            out["key"] = describe_key(parse_key(cfg["private_key"], cfg.get("passphrase") or ""))
        except SshAccessError as e:
            out["key_error"] = str(e)
    return out


def validate_username(value):
    value = (value or "").strip()
    if not _USERNAME_RE.match(value):
        raise SshAccessError("Пользователь: латинские буквы, цифры, «.», «_», «-», до 64 символов")
    return value


# ── Вход на хост ───────────────────────────────────────────────────────────

def parse_os_release(text):
    """Вывод PROBE_COMMAND → {os, kernel, hostname}. os — PRETTY_NAME из
    os-release, а где его нет — первая непустая строка (redhat-release, issue)."""
    head, _, rest = text.partition("---kernel")
    kernel, _, host = rest.partition("---host")
    fields = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?\s*$', head, flags=re.M))
    pretty = fields.get("PRETTY_NAME") or " ".join(
        x for x in (fields.get("NAME"), fields.get("VERSION")) if x)
    if not pretty:
        pretty = next((line.strip() for line in head.splitlines()
                       if line.strip() and "\\" not in line), "")
    return {"os": pretty, "id": fields.get("ID", ""), "version": fields.get("VERSION_ID", ""),
            "kernel": kernel.strip(), "hostname": host.strip()}


def probe(host, cfg=None, port=None):
    """Войти на хост сохранённым ключом и прочитать сведения об ОС.
    Возвращает (успех, строки отчёта, сведения об ОС либо None). На машине
    выполняется одна команда чтения, ничего не меняется."""
    import paramiko

    if cfg is None:
        cfg = load()[0]
    if not configured(cfg):
        return False, ["Ключ не задан — сохраните пользователя и ключ выше."], None
    port = int(port or cfg.get("port") or DEFAULT_PORT)
    user = cfg["username"]
    try:
        pkey = parse_key(cfg["private_key"], cfg.get("passphrase") or "")
    except SshAccessError as e:
        return False, [str(e)], None
    lines = []

    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            pass
        lines.append(f"TCP {host}:{port} — открыт")
    except OSError as e:
        return False, [f"TCP {host}:{port} — нет соединения ({e.strerror or type(e).__name__}). "
                       f"Порт закрыт либо с сервера ip_plan до хоста нет доступа."], None

    def connect(**extra):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, port=port, username=user, pkey=pkey,
                       look_for_keys=False, allow_agent=False, timeout=CONNECT_TIMEOUT,
                       banner_timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT, **extra)
        return client

    client = None
    try:
        try:
            client = connect()
        except paramiko.AuthenticationException:
            # Старые серверы (OpenSSH до 7.2) не умеют подпись rsa-sha2 и не
            # сообщают об этом; с RSA-ключом повторяем со старой подписью.
            if pkey.get_name() != "ssh-rsa":
                raise
            client = connect(disabled_algorithms={"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]})
        banner = client.get_transport().remote_version if client.get_transport() else ""
        lines.append(f"SSH — вход под «{user}» принят" + (f" ({banner})" if banner else ""))
        _, stdout, _ = client.exec_command(PROBE_COMMAND, timeout=COMMAND_TIMEOUT)
        output = stdout.read(OUTPUT_LIMIT).decode("utf-8", "replace")
    except paramiko.AuthenticationException:
        return False, lines + [f"SSH — ключ не принят для пользователя «{user}». Открытая часть ключа "
                               f"должна лежать в ~{user}/.ssh/authorized_keys на этом хосте."], None
    except (paramiko.SSHException, socket.timeout, EOFError) as e:
        return False, lines + [f"SSH — ошибка протокола: {e or type(e).__name__}"], None
    except OSError as e:
        return False, lines + [f"SSH — соединение оборвано: {e.strerror or type(e).__name__}"], None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    info = parse_os_release(output)
    if not info["os"] and not info["kernel"]:
        return False, lines + ["Вход выполнен, но машина не ответила на команду чтения сведений об ОС "
                               "(не Linux либо ограниченная оболочка)."], None
    lines.append(f"ОС — {info['os'] or 'не определена'}")
    if info["kernel"]:
        lines.append(f"Ядро — {info['kernel']}")
    if info["hostname"]:
        lines.append(f"Имя — {info['hostname']}")
    return True, lines, info
