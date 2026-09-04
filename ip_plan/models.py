"""Реестр блокировок регулятора (IP/FQDN/URL из писем) — первые "настоящие"
Django-модели в этом приложении (см. docs/plans/2026-09-04-regulator-blocklist-design.md).
Раньше своих моделей для сетевых данных не было принципиально (см. CLAUDE.md
корня репозитория, "нет своих моделей — источник истины NetBox"): ACL Lookup
и os_catalog обходятся файлами/памятью процесса, потому что там нечего
накапливать — состояние всегда равно текущему конфигу/справочнику на диске.
Здесь иначе: письма регулятора накапливаются во времени, старые записи не
исчезают при снятии блокировки (нужна история для аудита) — это ровно тот
случай, для которого нужна настоящая таблица с first-class CRUD, а не
in-memory парсинг файла при каждом запросе.
"""
from urllib.parse import urlsplit

from django.db import models


class RegulatorDocument(models.Model):
    """Одно письмо/предписание регулятора. reference_no не unique намеренно
    — регулятор иногда переиспользует номера в разных годах/сериях, дата
    вместе с номером — более надёжный естественный ключ, но и его не
    форсируем через unique_together: возможны дубликаты по недосмотру,
    пусть это будет видно на UI, а не блокирует сохранение."""

    reference_no = models.CharField("Номер письма", max_length=100)
    document_date = models.DateField("Дата письма")
    note = models.TextField("Примечание", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-document_date", "-id"]

    def __str__(self):
        return f"№{self.reference_no} от {self.document_date:%d.%m.%Y}"


class RegulatorEntry(models.Model):
    """Одна запись блокировки (IP/FQDN/URL) внутри письма."""

    KIND_CHOICES = [
        ("ip", "IP"),
        ("fqdn", "FQDN"),
        ("url", "URL"),
    ]
    STATUS_CHOICES = [
        ("active", "Активна"),
        ("revoked", "Снята"),
    ]

    document = models.ForeignKey(
        RegulatorDocument, related_name="entries", on_delete=models.PROTECT,
        verbose_name="Письмо",
    )
    kind = models.CharField("Тип", max_length=8, choices=KIND_CHOICES)
    value = models.CharField("Значение", max_length=500)
    # Нормализованное поле для сверки (с firewall-объектами в будущем — см.
    # design doc, фаза 2 — и вообще для поиска без учёта регистра/схемы).
    # Для kind=url — только домен (firewall фильтрует по DNS/IP, не по
    # path — сверять path не с чем; сам URL при этом хранится в value
    # целиком, на будущее, если появится URL-фильтрация). Для ip/fqdn —
    # value как есть, lower/strip.
    match_value = models.CharField("Значение для сверки", max_length=255, editable=False)
    status = models.CharField("Статус", max_length=8, choices=STATUS_CHOICES, default="active")
    revoked_at = models.DateTimeField("Снята", null=True, blank=True)
    revoked_reason = models.CharField("Причина снятия", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Запись блокировки"
        verbose_name_plural = "Записи блокировок"
        indexes = [
            models.Index(fields=["match_value"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.value}"

    @staticmethod
    def compute_match_value(kind, value):
        value = (value or "").strip()
        if kind == "url":
            # netloc содержит IP/FQDN, возможно с портом ("host:port") —
            # порт для сверки с firewall-объектом не нужен. Если urlsplit
            # не находит схему (пользователь ввёл голый домен в поле URL по
            # ошибке) — netloc пуст, тогда сверяем как есть.
            netloc = urlsplit(value if "://" in value else f"//{value}").netloc
            host = netloc.split("@")[-1].split(":")[0]
            return (host or value).lower()
        return value.lower()

    def save(self, *args, **kwargs):
        self.match_value = self.compute_match_value(self.kind, self.value)
        super().save(*args, **kwargs)
