from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())
# Нужен, если приложение открывается по HTTPS (напр. через nginx) — Django
# проверяет заголовок Origin при POST/PATCH/DELETE и без этого списка
# отвергает их с 403, даже если ALLOWED_HOSTS настроен верно.
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# Браузер различает куки только по (домену, пути), но не по порту — если
# открывать это приложение и NetBox на одном hostname (например localhost,
# 8050 и 8000), оба Django-проекта по умолчанию используют одно и то же имя
# куки сессии ("sessionid"/"csrftoken") и перетирают друг друга при логине.
# Свои имена решают это полностью, без привязки к hostname/порту.
SESSION_COOKIE_NAME = config("SESSION_COOKIE_NAME", default="ipplan_sessionid")
CSRF_COOKIE_NAME = config("CSRF_COOKIE_NAME", default="ipplan_csrftoken")

# Включать только если nginx/другой reverse proxy перед приложением сам
# выставляет X-Forwarded-Proto и не пропускает этот заголовок от клиента —
# иначе это даёт возможность подделать "https" заголовком и обойти
# secure-куки/редиректы.
if config("USE_X_FORWARDED_PROTO", default=False, cast=bool):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Если приложение открывается не в корне домена, а под префиксом пути
# (например https://netbox.sicmt.local/ipplan) — задать здесь, и весь код
# (reverse(), {% url %}, редиректы логина) сам подставит префикс. Пустое
# значение (по умолчанию, как при локальной разработке) — поведение не
# меняется вообще.
URL_PREFIX = config("URL_PREFIX", default="").rstrip("/")
if URL_PREFIX:
    FORCE_SCRIPT_NAME = URL_PREFIX

INSTALLED_APPS = [
    # auth/contenttypes/sessions — раньше не были нужны (всё хранилось в
    # NetBox через REST API, своих моделей/пользователей не было). С LDAP-
    # логином (см. ip_plan/ldap_config.py) появляется реальный Django User
    # (зеркало LDAP-идентичности) и сессии — это их зависимости.
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "ip_plan",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Приложение всегда работает через gunicorn (даже в dev) — у него, в
    # отличие от runserver, нет встроенной раздачи статики. whitenoise
    # отдаёт собранные collectstatic-ом файлы прямо из WSGI-приложения,
    # без необходимости отдельно настраивать это в nginx.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Защищает ВСЕ view по умолчанию (редирект на LOGIN_URL для анонимов) —
    # единственное исключение явно помечено @login_not_required в
    # ip_plan/urls.py (страница логина). Надёжнее, чем вручную обвешивать
    # @login_required каждую view-функцию — новую view, если забыть
    # декоратор, тут защитит сама middleware. Доступно с Django 5.1.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    # Строго после LoginRequiredMiddleware: анонимов та уже развернула на
    # страницу входа, здесь остаются только вошедшие. Держит локального
    # пользователя с флагом must_change_password на странице смены пароля,
    # пока он его не сменит (см. ip_plan/middleware.py и команду
    # ensure_local_admin — учётка admin/admin засевается именно с этим
    # флагом).
    "ip_plan.middleware.ForcePasswordChangeMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                # {{ user }} в шаблонах (кнопка выхода в base.html) —
                # без этого там был бы только request.user "напрямую".
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# IP-данные всё ещё читаются и пишутся напрямую через NetBox REST API, без
# собственных моделей — но с LDAP-логином (ip_plan/ldap_config.py) Django
# сам хранит здесь сессии и зеркало User из LDAP-идентичности. Путь — внутри
# data/, а не прямо в BASE_DIR: каталог смонтирован volume'ом в
# docker-compose.yml, чтобы сессии/пользователи не терялись при пересборке
# контейнера (а пересобираем мы его в этом проекте часто).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "data" / "db.sqlite3",
    },
}

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Абсолютный (не относительный!) путь, включающий URL_PREFIX заранее —
# относительный STATIC_URL резолвился бы браузером относительно текущей
# страницы и ломался в зависимости от слэша на конце адреса/глубины пути.
# whitenoise (см. WhiteNoiseMiddleware) сам срезает URL_PREFIX из этого
# значения при сравнении с PATH_INFO, который nginx отдаёт уже без префикса.
STATIC_URL = f"{URL_PREFIX}/static/"
# Сюда collectstatic складывает файлы (см. Dockerfile) — whitenoise отдаёт
# их оттуда же.
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# --- NetBox API ---
NETBOX_URL = config("NETBOX_URL")
NETBOX_TOKEN = config("NETBOX_TOKEN")
NETBOX_VERIFY_SSL = config("NETBOX_VERIFY_SSL", default=True, cast=bool)
NETBOX_TIMEOUT = config("NETBOX_TIMEOUT", default=15, cast=int)
NETBOX_SITE_FILTER = config("NETBOX_SITE_FILTER", default="")
NETBOX_TAG_FILTER = config("NETBOX_TAG_FILTER", default="")

# --- Сетевые проверки (пункты меню "Проверить доступность"/"Сканировать порты"/"Резолв DNS") ---
NETBOX_DNS_SERVERS = config("NETBOX_DNS_SERVERS", default="", cast=Csv())
NETBOX_DNS_SKIP_PATTERN = config("NETBOX_DNS_SKIP_PATTERN", default=r"\.vipnet$")

# --- ACL Lookup (страница "ACL") ---
# Каталог, куда netbox-sync/netbox_config_backup.py сохраняет running-config
# устройств (config_backups/<device>/latest.cfg) — см. ip_plan/services/
# acl_store.py и cisco_acl.py. Не через NetBox API: у netbox-acls плагина
# source/destination правила ссылаются на реальные объекты IPAM, а часть
# ACL здесь (deny-блэклист, object-group RusNets) — десятки тысяч записей
# внешних адресов, не имеющих отношения к вашей адресации — заводить их как
# Prefix/IPAddress в общем IPAM было бы неуместно. Поэтому эта страница
# читает те же .cfg-файлы напрямую и ничего не пишет в NetBox.
# По умолчанию — путь внутри контейнера, куда docker-compose.yml монтирует
# ${NETBOX_SYNC_DIR:-../netbox-sync}/config_backups (read-only).
# NETBOX_SYNC_DIR — хостовый путь до корня netbox-sync, задаётся через .env
# (см. .env.example): локально не нужен (дефолт — соседняя директория
# ../netbox-sync на диске разработчика), на проде обязателен и указывает на
# /opt/scan (DEPLOY_PATH из netbox-sync, см. CLAUDE.md) — там netbox-sync и
# ip_plan_react разложены в независимые build-директории GitLab-раннера,
# никакой относительный путь между ними не работает (баг, найденный и
# починенный при добавлении SYNC_LOGS_DIR ниже — до этого ACL_CONFIG_BACKUPS_DIR
# на проде молча резолвился в пустую папку через тот же самый относительный
# путь). Если раскладка когда-нибудь изменится ещё раз — свой путь/схему
# доставки через ACL_CONFIG_BACKUPS_DIR напрямую.
ACL_CONFIG_BACKUPS_DIR = config("ACL_CONFIG_BACKUPS_DIR", default=str(BASE_DIR / "config_backups"))

# --- Редактируемые конфиги netbox-sync (раздел "Настройки") ---
# Директория conf/ репозитория netbox-sync, смонтированная НА ЗАПИСЬ (см.
# docker-compose.yml). Здесь лежат devices.json, config.json и оба
# sources_*.json — те самые файлы, которые netbox-sync вынес из git именно
# для того, чтобы их можно было править отсюда (см. conf_loader.py в
# netbox-sync и docs/plans/2026-09-07-netbox-sync-settings-editor-design.md).
NETBOX_SYNC_CONF_DIR = config("NETBOX_SYNC_CONF_DIR", default=str(BASE_DIR / "netbox_sync_conf"))

# --- Раздел "Настройки" (шапка) — сводка последнего прогона netbox-sync ---
# Тот же принцип и тот же NETBOX_SYNC_DIR, что и у ACL_CONFIG_BACKUPS_DIR
# выше — только подпапка sync_logs вместо config_backups. См.
# ip_plan/services/sync_log.py.
SYNC_LOGS_DIR = config("SYNC_LOGS_DIR", default=str(BASE_DIR / "sync_logs"))

# Лок-файл full_sync.sh. Лежит в sync_logs/ — единственном каталоге синка,
# кроме conf/, который сюда проброшен (корень netbox-sync не монтируется
# сознательно: там код синка, который туда же деплоит CI). Раньше путь
# считался как «на уровень выше conf/», но conf/ монтируется как
# /app/netbox_sync_conf, и уровень выше — это корень самого ip_plan: файла
# там нет и быть не может, так что признак «синк идёт» всегда был ложным, а
# импорт настроек не блокировался вопреки задумке.
#
# Нужен только для предупреждения в UI, поэтому отсутствие каталога ошибкой
# не считается.
NETBOX_SYNC_LOCK_FILE = config(
    "NETBOX_SYNC_LOCK_FILE", default=str(Path(SYNC_LOGS_DIR) / ".full_sync.lock")
)

# --- Блокировки регулятора — снимок объектов блокировки firewall ---
# Тот же принцип и тот же NETBOX_SYNC_DIR, что и у ACL_CONFIG_BACKUPS_DIR/
# SYNC_LOGS_DIR выше — подпапка firewall_blocklists (пишет
# netbox-sync/netbox_firewall_blocklist_sync.py). См.
# ip_plan/services/firewall_blocklist.py.
FIREWALL_BLOCKLISTS_DIR = config("FIREWALL_BLOCKLISTS_DIR", default=str(BASE_DIR / "firewall_blocklists"))

# Превью разбора документов (ip_plan/services/ioc_parser.py,
# regulator_document_ioc_save) шлёт по 2 POST-поля (ioc_kind+ioc_value) на
# каждую найденную запись — у реального бюллетеня регулятора это легко
# несколько сотен IOC за один разбор (см. чек-лист в design doc: сотня+
# SHA256-хэшей в одном файле — не редкость), Django-дефолт в 1000 полей
# превышается уже на паре сотен строк (TooManyFieldsSent -> 400 Bad
# Request на кнопке "Сохранить в письмо"). Официально рекомендованный путь
# Django для легитимно больших форм — поднять лимит, не городить свой
# транспорт для этой одной формы.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 20000

# --- Аутентификация через FreeIPA (LDAPS) ---
# Сами настройки AUTH_LDAP_* — в ip_plan/ldap_config.py (он же читает
# ip_plan/ldap_settings.json). Здесь — только то, что относится к Django
# auth в целом, а не конкретно к LDAP-бэкенду.
from ip_plan.ldap_config import *  # noqa: F401,F403  — см. комментарий выше

# ModelBackend ПЕРВЫМ, LDAP вторым — порядок не косметический. В
# AUTH_LDAP_GLOBAL_OPTIONS стоят таймауты по 15 секунд на сервер, серверов
# два: при недоступном или неверно настроенном LDAP обратный порядок
# заставлял бы локального администратора ждать эти таймауты на каждом входе
# — ровно в тот момент, когда локальный вход и нужен. Зеркала
# LDAP-пользователей ModelBackend отбраковывает мгновенно (у них unusable
# password), так что для обычного входа через FreeIPA порядок ничего не
# меняет.
#
# Побочный эффект: локальная учётка с именем существующего
# LDAP-пользователя перекрыла бы ему вход. Поэтому создание локальных
# учёток проверяет, что такого uid нет в LDAP (см. ensure_local_admin и
# вкладку "Пользователи").
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    # Не штатный LDAPBackend: настройки FreeIPA правятся из раздела
    # "Настройки" и лежат в БД, а не здесь (см. ip_plan/auth_backends.py).
    "ip_plan.auth_backends.DbLDAPBackend",
]

# Django по умолчанию НЕ включает ни одного валидатора (пустой список —
# это дефолт самого фреймворка, набор из четырёх появляется только в
# шаблоне startproject, которого тут не было). Пока вход был исключительно
# через FreeIPA, это ничего не значило: пароли живут в каталоге и правила
# задаёт он. С появлением локальных учёток проверять стало что — и учётка
# это не рядовая, у неё is_superuser и доступ к паролям всего сетевого
# периметра. MinimumLength поднят с дефолтных 8 до 10 по той же причине.
# CommonPasswordValidator заодно закрывает возврат к "admin": это слово
# есть в поставляемом с Django списке частых паролей.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "ip_plan:login"
LOGIN_REDIRECT_URL = "ip_plan:ip_list"
LOGOUT_REDIRECT_URL = "ip_plan:login"
# Куда PasswordChangeView уводит после успешной смены. Именно ip_list, а не
# отдельная страница "готово": для локального админа, которого сюда пригнал
# ForcePasswordChangeMiddleware, смена пароля — препятствие на пути в
# приложение, а не самостоятельная задача.
PASSWORD_CHANGE_REDIRECT_URL = "ip_plan:ip_list"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "ip_plan": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        # WARNING по умолчанию — на проде LDAP-биндинг логировать подробно
        # не нужно. Для диагностики при первой настройке (bind DN, фильтр
        # поиска пользователя, проверка членства в группе) задайте в .env
        # LDAP_LOG_LEVEL=DEBUG — без правки кода и без необходимости потом
        # не забыть вернуть обратно.
        "django_auth_ldap": {
            "handlers": ["console"],
            "level": config("LDAP_LOG_LEVEL", default="WARNING"),
            "propagate": False,
        },
    },
}
