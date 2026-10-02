# Снимки политики доступа со всех устройств — дизайн

Статус: **в работе**, 2026-10-01. Затрагивает оба репозитория: сбор — в
`netbox-sync`, разбор и показ — в `ip_plan_docker_react`.

## Зачем

До этого списки доступа контролировались только на двух Cisco ASR: бэкап
`show running-config` → `config_backups/<dev>/latest.cfg` → страница «ACL»
с парсером Cisco. Межсетевые экраны ViPNet (xFW 5000, HW5000) читались лишь
ради двух объектов блокировок регулятора, правила `firewall rules show` не
разбирались вовсе; коммутаторы Eltex, Maipu, Huawei не бэкапились.

Исследование 2026-09-30 показало:

- xFW отдаёт всю политику двумя командами за 16 с: `firewall rules show`
  (Forward 262, Local 15, Service Local 4, Service VPN 21, NAT 54 правила)
  и `firewall object show` (321 IP-объект, 90 сервисов, 7 интерфейсных,
  2 расписания). В каждой секции печатается `Current Rules Set Id` —
  счётчик версии политики, растёт при любом изменении.
- На коммутаторах списков почти нет (Huawei — один basic ACL 2001 для SNMP,
  Eltex и Maipu пусто), но появление первого должно быть видно.
- `show ip access-lists` / `display acl all` содержат счётчики совпадений,
  которые меняются каждый день, — для снимков берём конфигурацию, где
  списки записаны без счётчиков.
- Страница ACL показывала SNMP community ASR открытым текстом в колонке
  «Применён» — закрыто в этой же работе.

## Сбор (netbox-sync, `netbox_config_backup.py`)

Один шаг «Config backup» снимает с устройства **несколько именованных
выводов** за одну SSH-сессию:

- `config_command` модели → `latest.cfg` + `<dev>_<ts>.cfg` (как раньше);
- `policy_commands` модели — словарь `{имя: команда}` → `latest.<имя>.txt`
  + `<dev>_<ts>.<имя>.txt`. У ViPNet:
  `firewall_rules: firewall rules show`, `firewall_objects: firewall object show`,
  `iplir_config: iplir show config`.

`backup_config: true` теперь требует хотя бы одного из двух ключей.
Дедупликация побайтовая для каждого файла отдельно, история копится только
из изменений, `--keep-days` чистит все `<dev>_*.{cfg,txt}`.

Конец вывода режется по маркеру парсера модели: Cisco — `end`, Huawei —
`return`, остальные — по строке промпта. Маркер применяется только к
`config_command`; у прочих команд убирается лишь эхо и промпт. ANSI-мусор
ViPNet (`ESC[?1h`, `ESC=`) вычищается.

Рядом пишется **`meta.json`**: устройство, модель, парсер интерфейсов
(`interfaces_parser` — по нему ip_plan выбирает парсер списков), площадка,
время снятия и по каждому файлу — команда, время последнего изменения,
для ViPNet — `Current Rules Set Id`. Так ip_plan не читает `devices.json`
и не гадает по имени каталога.

`netbox_firewall_blocklist_sync.py` перед SSH смотрит, нет ли свежего
(моложе 6 ч) `latest.firewall_objects.txt` из этого же прогона, и разбирает
его — одна SSH-сессия к xFW вместо двух. Формат `latest.json` для страницы
регулятора не меняется.

## Разбор (ip_plan, `ip_plan/services/`)

- `acl_model.py` — общая модель: `Endpoint`, `AclRule`, `Acl`,
  `Application`, `ParsedDevice`. Выросла из датаклассов `cisco_acl.py`,
  добавлены `label`, `action_raw`, `inactive`, `schedule`, `extra` у правила,
  `object_group_exclusions`, `service_objects`, `warnings` у устройства.
  Действие нормализовано в permit/deny/remark, вендорское слово в
  `action_raw`.
- Парсеры с единым входом `parse_snapshot(files, device_name)`, где
  `files` — `{имя снимка: текст}` (`config`, `firewall_rules`,
  `firewall_objects`):
  - `cisco_acl` — как был;
  - `vipnet_firewall` — категории Local, Forward, Tunnel, Vpn и NAT →
    `Acl` на непустую (`acl_type` = local | forward | tunnel | vpn | nat),
    показываются вкладками на карточке устройства; служебные секции
    Service Local / Service Vpn (Generated) в списки не идут, но остаются в
    файле снимка и в diff. Парсер понимает оба формата: xFW (Rule ID,
    стрелка `->`, строка DPI) и координатор HW (только имя, `(inactive)`
    перед именем, стрелка `>`, Tunnel Rules, VPN-объекты из имён узлов без
    адресов, имена объектов в колонках напечатаны байтами UTF-8 поверх
    KOI8-R — чинится `_fix_mojibake`). Фильтр
    = правило: Rule ID → `rule_id`, имя → `name`, порядок в секции →
    `sequence`; `@объект` → kind
    `object-group` (резолвится через IP Objects, исключения отдельно),
    `@local` → kind `local` без резолва, диапазоны и `ip/mask` → сети,
    `(inactive)` → флаг, DPI/DomainUser → `extra`; каждая непустая секция
    считается применённой (`Application(kind="section")`);
  - `huawei_acl` — `acl number N` / `acl name X` + `rule N permit|deny …`,
    применение: `snmp-agent community … acl`, `traffic-filter`,
    `user-interface … acl`;
  - `eltex_acl`, `maipu_acl` — Cisco-подобный синтаксис своих прошивок.
- `acl_parsers.py` — реестр `interfaces_parser → модуль`.
- `acl_store.py` читает `meta.json`, собирает нужные `latest.*` файлы,
  кэширует по кортежу mtime. Без `meta.json` (старый снимок) — как раньше,
  Cisco по `latest.cfg`.

Поиск по IP: попадание в исключение объекта снимает совпадение; правила
`inactive` показываются, но помечены и сортируются ниже.

## Показ

Страница «ACL» становится общей: заголовок и подсказки без слова Cisco,
в обзоре — имя правила, секция, бейдж «выключено», сведения о снимке
(когда снят, версия политики). Новая страница истории
`acl/<device>/history/`: список снимков файла и diff между соседними, в
diff маскируются строки с секретами (`community`, `password`, `secret`,
`key`).

## Что не делается сейчас

- Сверка реестра регулятора с объектами xFW и списками 20/30 ASR —
  следующий этап, на этой модели.
- Правила Континента: SNMP их не отдаёт, CLI не изучен.
- IPv6, первое-совпадение по портам.

## Проверка

- `netbox-sync`: `tests/test_config_backup.py` — разрез вывода на команды,
  маркеры конца, meta.json, Rules Set Id; прогон `--only xfw5000-mdc`.
- `ip_plan`: `ip_plan/tests/` с фикстурами, повторяющими формат живых
  выводов (адреса вымышленные): `test_vipnet_firewall.py`,
  `test_huawei_acl.py`, `test_eltex_acl.py`, `test_maipu_acl.py`,
  `test_acl_store.py`. Запуск: `python manage.py test ip_plan`.
