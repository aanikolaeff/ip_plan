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
import re
from urllib.parse import urlsplit

from django.db import models
from django.db.models.functions import Now


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
    # CharField с username, не FK на User — тем же принципом, что
    # LocalAccount.created_by/SettingsAudit.username: письмо не должно
    # стать нередактируемым (или каскадно не удаляться) только из-за того,
    # что учётку автора потом удалили/переименовали, а нам нужен просто
    # факт "кто ввёл письмо в реестр", не связь с текущим состоянием auth_user.
    created_by = models.CharField("Кем добавлено", max_length=150, blank=True)

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
        # "IOC" здесь — не сетевой индикатор (IP/домен/URL это тоже IOC в
        # широком смысле), а конкретно файловый: MD5/SHA1/SHA256-хэш
        # вредоносного файла из бюллетеней регулятора (см.
        # services/ioc_parser.py — HASH_RE ловит все три длины). Своё
        # значение kind не "sha256"/"md5"/"sha1" — все три алгоритма лежат
        # в одном "не сетевом" бакете, различие между ними важно только
        # внутри парсера (для дедупликации), не в реестре: карточка/фильтры
        # везде говорят просто "IOC".
        ("hash", "IOC"),
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


class FirewallObjectName(models.Model):
    """"От какого имени вести отсчёт" для генератора CLI-команд firewall
    (см. views._regulator_firewall_context, вкладка «Команды для xFW») —
    на xFW 5000 нет CLI-команды редактирования уже созданного ip-object'а
    (только firewall ip-object add для НОВОГО), поэтому реальный workflow —
    каждый раз создавать новый список с очередным порядковым номером в
    имени и применять его вместо старого вручную через веб-интерфейс
    ViPNet (см. design doc, фаза 2).

    current_name — то имя, что реально применено на firewall ПРЯМО СЕЙЧАС
    (в применении разбирается администратор, эта запись — просто память
    последнего known-applied имени). Генератор команд считает следующий
    номер от него, но НЕ продвигает current_name сам — только явное
    "Отметить применённым" (regulator_firewall_commands_apply), иначе
    повторное открытие вкладки без реального применения каждый раз
    предлагало бы новый номер, хотя на firewall ничего не изменилось."""

    KIND_CHOICES = [("ip", "IP"), ("fqdn", "FQDN")]

    kind = models.CharField("Тип", max_length=8, choices=KIND_CHOICES, unique=True)
    current_name = models.CharField("Текущее имя на firewall", max_length=100)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_kind_display()}: {self.current_name}"

    def next_name(self):
        """current_name с инкрементированным хвостовым числом
        (BLOCK_BY_IP_2 -> BLOCK_BY_IP_3). Если хвостового числа нет —
        дописывает "_2" (первый преемник безымянного/нестандартного имени)."""
        m = re.match(r"^(.*?)(\d+)$", self.current_name)
        if m:
            prefix, num = m.group(1), int(m.group(2))
            return f"{prefix}{num + 1}"
        return f"{self.current_name}_2"


class LocalAccount(models.Model):
    """Отметка "это локальная учётка, а не зеркало LDAP-пользователя".

    django-auth-ldap кладёт вошедших через FreeIPA в ту же таблицу
    auth_user, что и локальных, — отличить их по самой User нечем (у
    зеркала стоит unusable password, но это косвенный признак, который
    сломается, если кто-то руками задаст LDAP-пользователю пароль). Явная
    запись надёжнее и заодно хранит must_change_password.

    Локальный вход существует не ради удобства, а как аварийный: настройки
    LDAP будут править из UI (см. design doc, фаза 4), и неудачная правка
    не должна оставлять снаружи вообще всех.
    """

    user = models.OneToOneField(
        "auth.User", on_delete=models.CASCADE,
        related_name="local_account", verbose_name="Пользователь",
    )
    # Ставится при засеве admin/admin (ensure_local_admin) и при сбросе
    # пароля администратором. Пока флаг стоит, middleware никуда, кроме
    # страницы смены пароля, пользователя не пускает — иначе дефолтная
    # учётка живёт в проде годами.
    must_change_password = models.BooleanField("Требуется смена пароля", default=False)
    created_by = models.CharField("Кем создана", max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Локальная учётная запись"
        verbose_name_plural = "Локальные учётные записи"

    def __str__(self):
        return self.user.username


class SettingsAudit(models.Model):
    """Журнал изменений, сделанных из раздела "Настройки".

    Файлы netbox-sync хранят историю версий сами (conf/.history/, см.
    services/sync_config.py), но по файлу не видно, КТО его поменял — а
    речь про SSH-пароли боевого firewall и коммутаторов. Здесь именно эта
    часть: кто, когда, что именно и с каким результатом.

    Значения секретов сюда не попадают никогда — в details пишется факт
    "пароль изменён", а не сам пароль.
    """

    ACTION_CHOICES = [
        ("create", "Создание"),
        ("update", "Изменение"),
        ("delete", "Удаление"),
    ]

    username = models.CharField("Пользователь", max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    scope = models.CharField("Раздел", max_length=50)
    target = models.CharField("Объект", max_length=200, blank=True)
    action = models.CharField("Действие", max_length=10, choices=ACTION_CHOICES)
    details = models.TextField("Что изменилось", blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Изменение настроек"
        verbose_name_plural = "Журнал изменений настроек"

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.username} {self.scope}/{self.target}"


class LdapConfig(models.Model):
    """Настройки подключения к FreeIPA — синглтон (всегда строка с pk=1).

    Раньше жили в ip_plan/ldap_settings.json, который читается один раз при
    импорте settings.py и лежит ВНУТРИ образа. Значит править его из UI было
    нельзя дважды: запись терялась бы при пересборке контейнера и всё равно
    не применилась бы без рестарта. Здесь — БД (и SQLite в томе sqlite_data,
    и PostgreSQL в томе pg_data переживают `up -d --build`), а применение
    без рестарта обеспечивает
    ip_plan/auth_backends.py.

    Файл ldap_settings.json остаётся как сид: миграция 0007 переносит из
    него значения при первом применении, дальше он не читается.

    Чего здесь намеренно НЕТ: пути к CA-сертификату LDAPS. Опции
    OPT_X_TLS_* в python-ldap глобальны для процесса и должны быть
    выставлены до первого соединения, поэтому сертификат меняется файлом и
    требует перезапуска контейнера (см. ip_plan/ldap_config.py).
    """

    enabled = models.BooleanField("Вход через LDAP включён", default=True)
    servers = models.TextField("Серверы", help_text="По одному URI в строке, например ldaps://ipa.example.local:636")
    bind_dn = models.CharField("Bind DN", max_length=400, blank=True)
    # Зашифровано (services/secretbox.py) — в открытом виде не хранится и в
    # интерфейс не отдаётся.
    bind_password_encrypted = models.TextField("Пароль bind (шифр.)", blank=True)
    user_search_base = models.CharField("База поиска пользователей", max_length=400)
    user_search_filter = models.CharField("Фильтр поиска пользователя", max_length=400,
                                          default="(uid=%(user)s)")
    group_search_base = models.CharField("База поиска групп", max_length=400, blank=True)
    require_group = models.CharField("Группа, дающая вход", max_length=400, blank=True)
    staff_group = models.CharField("Группа администраторов (is_staff)", max_length=400, blank=True)
    superuser_group = models.CharField("Группа суперпользователей (is_superuser)", max_length=400, blank=True)
    cache_timeout = models.PositiveIntegerField("Кеш групп, секунд", default=3600)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField("Кем изменено", max_length=150, blank=True)

    class Meta:
        verbose_name = "Настройки LDAP"
        verbose_name_plural = "Настройки LDAP"

    def __str__(self):
        return f"LDAP: {self.server_list()[:1]}"

    @classmethod
    def load(cls):
        """Единственная строка. Создаётся миграцией, но get_or_create здесь
        на случай пустой БД (например, свежий том без миграции-сида)."""
        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            "enabled": False, "servers": "", "user_search_base": "",
        })
        return obj

    def server_list(self):
        return [line.strip() for line in (self.servers or "").splitlines() if line.strip()]

    @property
    def bind_password(self):
        from .services import secretbox
        return secretbox.decrypt(self.bind_password_encrypted)

    def set_bind_password(self, value):
        from .services import secretbox
        self.bind_password_encrypted = secretbox.encrypt(value)

    def flags_by_group(self):
        flags = {}
        if self.staff_group:
            flags["is_staff"] = self.staff_group
        if self.superuser_group:
            flags["is_superuser"] = self.superuser_group
        return flags


class ConnectionTest(models.Model):
    """Результат проверки доступности устройства или гипервизора.

    Состояние живёт в базе, а не в памяти процесса: gunicorn запускает
    несколько воркеров, и следующий запрос браузера легко попадёт в другой —
    тот, который проверку не запускал.

    Запись на объект одна: она же текущий прогон, она же последний результат.
    Так кнопка всегда знает, что показывать, а история проверок не копится —
    она никому не нужна, важен только последний итог.
    """

    KIND_CHOICES = [
        ("device", "Сетевое устройство"),
        ("redvirt", "RedVirt"),
        ("vcenter", "vCenter"),
    ]
    STATUS_RUNNING = "running"
    STATUS_OK = "ok"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Выполняется"),
        (STATUS_OK, "Успешно"),
        (STATUS_FAILED, "Ошибка"),
    ]

    target_kind = models.CharField("Тип объекта", max_length=20, choices=KIND_CHOICES)
    target_key = models.CharField("Объект", max_length=300)
    status = models.CharField("Состояние", max_length=10, choices=STATUS_CHOICES)
    started_at = models.DateTimeField("Начата", auto_now_add=True)
    finished_at = models.DateTimeField("Завершена", null=True, blank=True)
    started_by = models.CharField("Кем запущена", max_length=150, blank=True)
    # [{"name": ..., "status": "ok|failed|skipped", "detail": ..., "elapsed": 0.42}]
    # Секретов здесь быть не должно: шаги пишет connection_test.py, и он
    # кладёт в detail только адреса, коды ответов и текст ошибок.
    steps = models.JSONField("Шаги", default=list, blank=True)
    summary = models.CharField("Итог", max_length=300, blank=True)

    class Meta:
        verbose_name = "Проверка подключения"
        verbose_name_plural = "Проверки подключений"
        constraints = [
            models.UniqueConstraint(fields=["target_kind", "target_key"],
                                    name="uniq_connection_test_target"),
        ]

    def __str__(self):
        return f"{self.target_kind}:{self.target_key} — {self.status}"

    @property
    def is_running(self):
        return self.status == self.STATUS_RUNNING

    def duration(self):
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class MapLayout(models.Model):
    """Раскладка карты сети одного пользователя: свободно перетащенные
    позиции узлов на площадке и слое. У каждого своя — общая раскладка
    мешала бы двоим, разбирающим разные участки сети."""

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="map_layouts")
    site = models.CharField("Площадка", max_length=100)          # slug или "*" (все площадки)
    layer = models.CharField("Слой", max_length=20, default="l2")
    # {"<node_id>": [x, y], ...}; node_id — "d<device id>" или "g<группа>"
    positions = models.JSONField("Позиции", default=dict, blank=True)
    # группы, развёрнутые пользователем: ["g…", …]
    expanded = models.JSONField("Развёрнутые группы", default=list, blank=True)
    locked = models.BooleanField("Закреплено", default=False)
    updated_at = models.DateTimeField("Изменено", auto_now=True)

    class Meta:
        verbose_name = "Раскладка карты"
        verbose_name_plural = "Раскладки карты"
        constraints = [
            models.UniqueConstraint(fields=["user", "site", "layer"], name="uniq_map_layout"),
        ]

    def __str__(self):
        return f"{self.user} · {self.site} · {self.layer}"


# --- Расписания и задания сборщика netbox-sync -----------------------------
# U8 плана docs/plans/2026-10-04-0248-feat-observations-stage2-worker-plan.md
# (KTD3, KTD4, KTD14, KTD15). Таблицы — контракт между репозиториями: сборщик
# netbox-sync читает и пишет их SQL-запросами ролью sync_worker, без Django.
# Поэтому db_table заданы явно, а всё, что сборщик может не указать при
# INSERT, имеет умолчание в самой базе (db_default), а не только в Python.
# Имена таблиц и колонок закреплены тестом test_sync_jobs_models — менять
# только вместе со сборщиком.
#
# Чего здесь нет намеренно: группы блокировки («пишет в NetBox» / «только
# netobs») — свойство шага в белом списке сборщика (KTD14), путь к скрипту и
# --env — тоже из его конфигурации (KTD5). Строка задания называет шаги
# только по имени.


class SyncSchedule(models.Model):
    """Расписание: один шаг или цепочка шагов и правило запуска (KTD15).

    Запуск — по интервалу, по времени суток или по событию «ночной nmap
    завершился» с запасным временем, если он к нему не завершился.
    Пропущенные такты не догоняются: после простоя сборщик создаёт одно
    задание и сдвигает next_run (единственная колонка, которую ему можно
    менять). Время суток — в часовом поясе SYNC_LOG_TZ.
    """

    TRIGGER_INTERVAL = "interval"
    TRIGGER_DAILY = "daily"
    TRIGGER_AFTER_NMAP = "after_nmap"
    TRIGGER_CHOICES = [
        (TRIGGER_INTERVAL, "По интервалу"),
        (TRIGGER_DAILY, "По времени суток"),
        (TRIGGER_AFTER_NMAP, "После nmap"),
    ]

    # Вид: имя шага или цепочки (например, «night_after_nmap»). Им же
    # помечаются задания этого расписания — по виду работает «один
    # queued/running на вид».
    kind = models.CharField("Вид", max_length=100, unique=True)
    title = models.CharField("Название", max_length=200, blank=True, db_default="")
    # ["netbox_devices_sync", "netbox_config_backup", ...] — имена шагов
    # белого списка сборщика, по порядку выполнения.
    steps = models.JSONField("Шаги", default=list)
    trigger = models.CharField("Запуск", max_length=20, choices=TRIGGER_CHOICES)
    interval_minutes = models.PositiveIntegerField("Интервал, мин", null=True, blank=True)
    daily_at = models.TimeField("Время суток", null=True, blank=True)
    # Для «после nmap»: если nmap к этому времени не завершился, цепочка
    # стартует всё равно, а баннер пишет «ночной nmap не прошёл».
    fallback_at = models.TimeField("Запасное время", null=True, blank=True)
    enabled = models.BooleanField("Включено", default=True, db_default=True)
    next_run = models.DateTimeField("Следующий запуск", null=True, blank=True)
    updated_at = models.DateTimeField("Изменено", auto_now=True, db_default=Now())
    updated_by = models.CharField("Кем изменено", max_length=150, blank=True, db_default="")

    class Meta:
        db_table = "sync_schedule"
        ordering = ["kind"]
        verbose_name = "Расписание синка"
        verbose_name_plural = "Расписания синка"
        constraints = [
            # Сборщик разбирает строку сам: строка без поля своего вида
            # остановила бы его цикл, поэтому несогласованность отсекает база.
            # isnull=False явно: NULL > 0 даёт NULL, и CHECK его пропустил бы.
            models.CheckConstraint(
                condition=(
                    models.Q(trigger="interval", interval_minutes__isnull=False,
                             interval_minutes__gt=0)
                    | models.Q(trigger="daily", daily_at__isnull=False)
                    | models.Q(trigger="after_nmap", fallback_at__isnull=False)
                ),
                name="sync_schedule_trigger_fields",
            ),
        ]

    def __str__(self):
        return self.title or self.kind


# Состояния задания — на уровне модуля: на них же стоят ограничения таблицы,
# а Meta внутри класса атрибутов самого класса не видит.
_SYNC_JOB_STATUS_CHOICES = [
    ("queued", "В очереди"),
    ("running", "Выполняется"),
    ("done", "Выполнено"),
    ("done_with_errors", "Выполнено с ошибками"),
    ("failed", "Ошибка"),
    ("timeout", "Прервано по таймауту"),
    ("interrupted", "Прервано"),
]
_SYNC_JOB_ACTIVE_STATUSES = ("queued", "running")


class SyncJob(models.Model):
    """Задание сборщика: снимок шагов на момент постановки, кто поставил,
    состояние и итог по шагам (KTD4, KTD5).

    Захват — короткая транзакция `UPDATE … WHERE id = (SELECT … FOR UPDATE
    SKIP LOCKED) RETURNING` с арендой lease_until, которую продлевает
    heartbeat; задание с истёкшей арендой любой живой сборщик переводит в
    interrupted — «выполняется» после падения не остаётся (R12).
    """

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_DONE_WITH_ERRORS = "done_with_errors"
    STATUS_FAILED = "failed"
    STATUS_TIMEOUT = "timeout"
    STATUS_INTERRUPTED = "interrupted"
    STATUS_CHOICES = _SYNC_JOB_STATUS_CHOICES
    ACTIVE_STATUSES = _SYNC_JOB_ACTIVE_STATUSES

    SOURCE_SCHEDULE = "schedule"
    SOURCE_USER = "user"
    SOURCE_CHOICES = [
        (SOURCE_SCHEDULE, "Расписание"),
        (SOURCE_USER, "Пользователь"),
    ]

    # Вид: kind расписания или имя одного шага («запустить сейчас»).
    kind = models.CharField("Вид", max_length=100)
    # Снимок шагов на момент постановки: правка расписания не меняет уже
    # поставленное задание.
    steps = models.JSONField("Шаги", default=list)
    # SET_NULL: история заданий переживает удаление расписания.
    schedule = models.ForeignKey(SyncSchedule, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="jobs")
    # Такт расписания, ради которого создано задание; у ручного — пусто.
    scheduled_for = models.DateTimeField("Такт расписания", null=True, blank=True)
    source = models.CharField("Источник", max_length=20, choices=SOURCE_CHOICES,
                              default=SOURCE_USER, db_default=SOURCE_USER)
    requested_by = models.CharField("Кем запущено", max_length=150, blank=True, db_default="")
    status = models.CharField("Состояние", max_length=20, choices=STATUS_CHOICES,
                              default=STATUS_QUEUED, db_default=STATUS_QUEUED)
    # Чего ждёт задание в running, пока шаг не начался, — например, «ждёт
    # nmap» при занятой общей блокировке KTD13. Пусто — не ждёт.
    wait_reason = models.CharField("Ожидание", max_length=200, blank=True, db_default="")
    worker_id = models.CharField("Сборщик", max_length=100, blank=True, db_default="")
    lease_until = models.DateTimeField("Аренда до", null=True, blank=True)
    created_at = models.DateTimeField("Поставлено", auto_now_add=True, db_default=Now())
    started_at = models.DateTimeField("Начато", null=True, blank=True)
    finished_at = models.DateTimeField("Завершено", null=True, blank=True)
    # [{"step": "...", "status": "done|done_with_errors|failed|timeout|interrupted|skipped",
    #   "started_at": "...", "duration": 12.3, "exit_code": 0}]
    step_results = models.JSONField("Итог по шагам", default=list, db_default=[])
    # Голова и хвост вывода (вместе до ~256 КБ); полный вывод — в файле
    # sync_logs/worker/<id>.log, путь — log_path.
    output_head = models.TextField("Начало вывода", blank=True, db_default="")
    output_tail = models.TextField("Конец вывода", blank=True, db_default="")
    log_path = models.CharField("Полный лог", max_length=500, blank=True, db_default="")
    # Хеш netbox-sync/conf на старте задания (KTD18) и VERSION кода синка.
    conf_hash = models.CharField("Хеш conf", max_length=64, blank=True, db_default="")
    code_version = models.CharField("Версия кода", max_length=100, blank=True, db_default="")
    # Короткая причина итога: «шаг вне белого списка», «аренда истекла» и т.п.
    summary = models.CharField("Итог", max_length=500, blank=True, db_default="")

    class Meta:
        db_table = "sync_job"
        ordering = ["-created_at", "-id"]
        verbose_name = "Задание синка"
        verbose_name_plural = "Задания синка"
        constraints = [
            # Один queued/running на вид: повторный «запустить сейчас»
            # возвращает уже стоящее задание (KTD14).
            models.UniqueConstraint(
                fields=["kind"], condition=models.Q(status__in=list(_SYNC_JOB_ACTIVE_STATUSES)),
                name="sync_job_one_active_per_kind",
            ),
            # Один такт расписания — одно задание, даже при двух сборщиках.
            models.UniqueConstraint(fields=["schedule", "scheduled_for"],
                                    name="sync_job_schedule_tick"),
            models.CheckConstraint(
                condition=models.Q(status__in=[k for k, _ in _SYNC_JOB_STATUS_CHOICES]),
                name="sync_job_status_valid",
            ),
        ]
        indexes = [
            # Захват из очереди и поиск истёкших аренд.
            models.Index(fields=["status", "created_at"], name="sync_job_status_created"),
        ]

    def __str__(self):
        return f"{self.kind} #{self.pk} ({self.status})"
