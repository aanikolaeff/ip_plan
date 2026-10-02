"""Шифрование секретов, которые приложение хранит у себя в БД.

Сейчас это ровно один секрет — bind-пароль LDAP (модель LdapConfig). Всё
остальное либо хешируется Django (пароли локальных учёток), либо живёт в
файлах netbox-sync и туда же и пишется.

Ключ выводится из SECRET_KEY, а не хранится отдельно: отдельный ключ надо
было бы куда-то положить и как-то доставлять на прод, то есть та же задача,
только с ещё одним секретом. Это защита от случайного просмотра базы
(db.sqlite3 или PostgreSQL — смотря по IP_PLAN_DB_ENGINE, в том числе в
бэкапе тома или дампе), а НЕ от того, кто уже получил доступ к хосту:
у него есть и SECRET_KEY.

Следствие, о котором нужно помнить при эксплуатации: смена SECRET_KEY
делает сохранённый bind-пароль нечитаемым. Это не поломка — decrypt()
вернёт пустую строку, вход через LDAP перестанет работать, и пароль нужно
будет ввести в интерфейсе заново (локальный вход для этого и существует).
"""
import logging
from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)


def _fernet():
    # Fernet требует ровно 32 байта в urlsafe-base64; SECRET_KEY произвольной
    # длины, поэтому прогоняем через sha256.
    key = urlsafe_b64encode(sha256(settings.SECRET_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Обычно означает смену SECRET_KEY. Пустая строка вместо исключения:
        # приложение должно остаться работоспособным (локальный вход!), а
        # человек — увидеть в интерфейсе, что пароль надо задать заново.
        logger.warning(
            "Не удалось расшифровать сохранённый секрет — вероятно, сменился SECRET_KEY. "
            "Введите пароль заново в разделе «Настройки»."
        )
        return ""
