# CLAUDE.md

Инструкции для Claude Code по работе с `ip_plan_docker_react` — отдельный
git-репозиторий (не часть монорепо `NETBOX/`). Общая карта репозитория и
правила, применимые ко всем трём подпроектам — в `../CLAUDE.md`.

> [!warning] До 2026-09-10 существовала более старая версия этого приложения
> без React — `ip_plan_docker` (удалена). Если где-то в документации/коде
> встречается путь `ip_plan_docker` или порт `8050` — это про неё, не про
> текущий репозиторий (порт здесь `8090`).

## Запуск локально

```bash
cd ip_plan_docker_react
cp .env.example .env          # заполнить SECRET_KEY, NETBOX_URL, NETBOX_TOKEN, NETBOX_SYNC_DIR
docker compose up -d --build
docker compose logs -f web
```

Приложение доступно на `http://localhost:8090` (проброс `8090:8000`).

При каждом старте контейнера `entrypoint.sh` выполняет по порядку:
`manage.py migrate` → `manage.py ensure_local_admin` (см. ниже) →
`manage.py collectstatic`, затем запускает `gunicorn`.

## Архитектура

Приложение — **не только** тонкий HTMX-фронтенд поверх NetBox REST API, как
было в старой (удалённой) версии. Основная таблица IP-адресов по-прежнему
устроена так — своих моделей для неё нет, всё читается/пишется напрямую через
NetBox API (`ip_plan/services/netbox_client.py`). Но за последнее время
появилось несколько подсистем с **настоящими Django-моделями и БД**
(`ip_plan/models.py`, `ip_plan/migrations/`, 9 миграций на момент написания):

- **Реестр блокировок регулятора** (`RegulatorDocument`, `RegulatorEntry`,
  `FirewallObjectName`) — письма регулятора и их IP/FQDN/URL/IOC-записи
  накапливаются во времени, не проксируются через NetBox (см.
  `docs/plans/2026-09-04-regulator-blocklist-design.md`). Таблица записей на
  странице письма и вкладка «Записи» общего списка — React/TanStack-island
  (`ip_plan/static_src/regulator_table`), тот же принцип, что у основной
  IP-таблицы (`ip_plan/static_src/ip_table`): один fetch целиком, дальше вся
  фильтрация/сортировка/пагинация на клиенте.
- **Раздел «Настройки»** (`ip_plan/views_settings.py`, `ip_plan/templates/ip_plan/settings.html`
  и `_settings_*.html`) — редактирование конфигов netbox-sync через UI вместо
  ручной правки файлов на проде. 7 вкладок: Оборудование/Модели/Гипервизоры/
  Скан и DNS (требуют `is_staff`, правят `conf/devices.json`,
  `conf/sources_*.json`, `conf/config.json`), Вход и LDAP/Пользователи
  (требуют `is_superuser`), Журнал (история изменений, `is_staff`). Пишет
  файлы через `ip_plan/services/sync_config.py` — единственное место в
  приложении, которое имеет право писать в примонтированную `netbox-sync/conf`
  (атомарная запись tmp+rename, история версий в `conf/.history/` — до 20 на
  файл, хеш файла проверяется при сохранении, чтобы два администратора не
  затёрли правки друг друга молча; секреты в форму никогда не уходят в
  открытом виде — только маска `••••••••`, а сохранение маски как есть = «не
  менять»).
- **Аутентификация** — двухбэкендовая: `ModelBackend` (локальные учётки) +
  `ip_plan.auth_backends.DbLDAPBackend` (свой LDAP-бэкенд поверх
  `django_auth_ldap.LDAPBackend`, переопределяющий `settings`-property так,
  чтобы читать параметры FreeIPA из модели `LdapConfig`, а не из
  `django.conf.settings` — иначе правка LDAP из UI требовала бы перезапуска
  контейнера). Единственное, что по-прежнему в `.env`/`settings.py`, а не в
  БД — путь до CA-сертификата LDAPS (`LDAP_TLS_CA_CERT`): опции TLS в
  python-ldap глобальны для процесса, меняются только файлом и перезапуском.
  `ip_plan/ldap_settings.json` теперь используется только как одноразовый
  сид для миграции `0008_seed_ldap_config` (переносит значения в БД при
  первом `migrate`, дальше не читается).
- **Локальный аварийный администратор** — `manage.py ensure_local_admin`
  (вызывается `entrypoint.sh` на каждом старте, но создаёт учётку **только
  если** локальных ещё нет; повторные старты пароль не сбрасывают). Нужен на
  случай неверной правки LDAP-настроек из UI: не должно быть сценария, где
  ошибка в интерфейсе оставляет снаружи вообще всех. `ip_plan.middleware.ForcePasswordChangeMiddleware`
  держит такую учётку на странице смены пароля, пока `LocalAccount.must_change_password`
  не снят — до этого приложение никуда больше не пускает.

SQLite (`data/db.sqlite3`, Docker volume `sqlite_data`) хранит Django-сессии,
LDAP-зеркалированные объекты `User`, и теперь ещё все модели выше —
`RegulatorDocument`/`RegulatorEntry`/`FirewallObjectName`/`LocalAccount`/
`SettingsAudit`/`LdapConfig`.

Прочее по-прежнему как было:
- `ip_plan/services/netbox_client.py` — класс `NetBoxClient`: единая
  HTTP-сессия, пагинация (`limit=1000`, `offset`), типизированная ошибка
  (`NetBoxAPIError` с `status_code`)
- `ip_plan/views.py` — все view возвращают HTML-партиалы (HTMX OOB swaps) или
  JSON. Никаких перезагрузок страницы при CRUD-операциях
- `ip_plan/services/network_tools.py` — тонкие обёртки над `fping` и `nmap`
  (оба должны быть в контейнере)
- `ip_plan/services/os_catalog.py` — каталог ОС хранится в
  `admin/os_catalog.json` (bind-mount, редактируется на хосте)
- `column_info.json` — описания колонок таблицы IP, читается один раз при
  старте процесса; обновление требует пересборки контейнера

### Фронтенд-сборка

Два React/TanStack-island'а (`ip_table`, `regulator_table`) на React 18 +
`@tanstack/react-table` v8, собираются Vite 6 в отдельной стадии
многостадийного `Dockerfile` (`node:20-alpine` → собранный `dist/` копируется
в финальный `python:3.12-slim`-образ). Правка `ip_plan/static_src/**`
требует пересборки образа (`docker compose up -d --build`), как и
`column_info.json` выше — dev-сервер Vite отдельно не поднят.

## Ключевые настройки (`.env`)

| Переменная | Заметки |
|----------|-------|
| `SECRET_KEY` | Для прода генерировать заново — им же шифруется `LdapConfig.bind_password_encrypted` (см. `ip_plan/services/secretbox.py`); смена ключа на проде делает сохранённый bind-пароль нечитаемым, придётся вводить заново через UI |
| `NETBOX_URL` | `http://host.docker.internal:8000`, если NetBox на том же хосте, но вне Docker-сети |
| `NETBOX_VERIFY_SSL` | `false` для самоподписанных сертификатов |
| `CSRF_TRUSTED_ORIGINS` | Обязательно для HTTPS-деплоя (иначе все POST/PATCH/DELETE вернут 403) |
| `URL_PREFIX` | Указать, если приложение работает под суб-путём (например `/ipplan`) |
| `SESSION_COOKIE_NAME` / `CSRF_COOKIE_NAME` | Свои имена — на одном hostname с NetBox/другим стендом на другом порту дефолтные имена кук развидят один логин при входе в другой (браузер не различает куки по порту) |
| `NETBOX_SYNC_DIR` | Хостовый (не внутриконтейнерный) путь до корня `netbox-sync` — только для `docker-compose.yml volumes`, Django не читает напрямую. Пусто = сиблинг-директория `../netbox-sync`. На проде — обязательно (раннер чекаутит проекты не рядом), см. `NETBOX_SYNC_DIR` в `netbox-sync/CLAUDE.md` про `DEPLOY_PATH=/opt/scan` |
| `ACL_CONFIG_BACKUPS_DIR` / `SYNC_LOGS_DIR` / `FIREWALL_BLOCKLISTS_DIR` | Пути ВНУТРИ контейнера до примонтированных read-only данных netbox-sync (страница ACL, сводка в «Настройках», блокировки регулятора) — пусто = дефолт, совпадающий с точкой монтирования в `docker-compose.yml` |
| `LDAP_BIND_PASSWORD` | Нужна только ОДИН раз — при первом применении миграции `0008_seed_ldap_config`. После — не читается, пароль правится в «Настройки → LDAP» |
| `LDAP_TLS_CA_CERT` | Единственный LDAP-параметр, который нельзя поменять из UI (см. выше) |
| `LDAP_LOG_LEVEL` | Уровень логов `django_auth_ldap`, для диагностики bind/фильтра/групп |
| `LOCAL_ADMIN_USERNAME` / `LOCAL_ADMIN_PASSWORD` | Пусто = `admin`/`admin`. Только для первого засева — повторные старты контейнера пароль не сбрасывают |
| `SEED_LOCAL_ADMIN` | `false` — не заводить локального администратора вообще (если уже есть `createsuperuser`-учётка) |

## Inline-редактируемые поля

Только `PATCHABLE_FIELDS` в `views.py` принимаются эндпоинтом `ip_patch` (PATCH):
- Верхний уровень: `dns_name`, `status`, `description`, `comments`
- Кастомные поля: `os_installed`, `purpose`

CPE, Open Ports, OS Guess — read-only в UI (пишутся только скриптами синка).

## Поддерживаемая ширина экрана

Только десктоп. Брейкпоинты в CSS есть лишь у постоянной шапки (`≤860px`,
`nav.css`) и у рельса раздела «Настройки» (`≤880px`) — сами плотные таблицы
(IP План, записи регулятора) под маленький экран не адаптированы и не
предполагались к этому: приложение — рабочий инструмент сетевых/security-
инженеров за десктопом, не публичный сайт.

> [!warning] TODO: это наблюдение по факту кода (см. "IP Plan Design
> Audit", 13.09.2026), не подтверждённое явно продуктовое решение — если
> мобильный доступ когда-то понадобится, отталкиваться придётся от этого
> текста, а не от того, что "раньше просто не было брейкпоинтов".

## Источники, использованные для этого файла

`docker-compose.yml`, `.env.example`, `config/settings.py`, `ip_plan/models.py`,
`ip_plan/migrations/`, `ip_plan/views_settings.py`, `ip_plan/middleware.py`,
`ip_plan/auth_backends.py`, `ip_plan/services/sync_config.py`, `package.json`,
`Dockerfile`, `DEPLOY_CHECKLIST.md` (частично устарел — см. ниже).

> [!warning] TODO: `DEPLOY_CHECKLIST.md` в этой директории местами не
> актуализирован под текущую версию (всё ещё называет директорию
> `ip_plan_docker/` и утверждает «база данных/миграции не нужны» — оба
> утверждения неверны для этой, React-версии). Даты/детали живого прод-стенда
> (реальный `NETBOX_URL`, текущее состояние LDAP на проде) в коде не видны —
> проверять вручную на самом проде, не по репозиторию.
