# Модернизация таблицы IP-плана: React + TanStack Table

Дата: 2026-09-03
Статус: дизайн согласован, реализация начата

## Проблема

Таблица IP-адресов (`ip_plan_docker`, `ip_table.html`) построена на jQuery
DataTables 2.0.8 + плагин FixedColumns, поверх — самописный JS (~700 строк
в `app.js`), который вручную считает и проставляет пиксельные ширины
колонок (`FIXED_COLUMN_WIDTHS`/`STRETCHY_MIN_WIDTHS` — жёсткие константы) и
пересчитывает офсеты закреплённых колонок при каждом изменении. Ресайза
колонок пользователем нет вообще — то, что выглядит как "система ширин",
статично. Хочется современный движок с ресайзом (запоминается на клиенте)
и не потерять существующий функционал.

## Ключевое решение: полный переход на JSON + React + TanStack Table

Рассматривались три варианта: (А) точечный патч поверх DataTables, (Б)
Tabulator.js как новый движок с адаптером над текущей HTMX-моделью
(строки остаются на сервере), (В) полный переход на JSON-данные + React +
`@tanstack/react-table` (клиент владеет рендером таблицы). Выбран **вариант
В** — по явному запросу, несмотря на больший объём работ, ради
единообразной современной архитектуры без промежуточных адаптеров.

### Почему это не так рискованно, как кажется

Инспекция `app.js` показала: inline-редактирование (`saveInlineEdit`),
kebab-действия (check/scan-ports/resolve-dns — `runIpNetworkAction`) и
удаление (`deleteIpRow`, bulk) **уже** ходят через `fetch()` + JSON
(`patchJson`, `fetchWithCsrf`), а не через HTMX OOB-swap. HTMX реально
нужен только для загрузки модалок (форма добавления/редактирования IP,
справка, справочник ОС) — из отдельных Django-шаблонов, не связанных с
самой таблицей. Значит SPA-переход касается **только** отрисовки строк и
тулбара, а не всей бизнес-логики.

## Границы проекта

- **Отдельная директория `ip_plan_docker_react/`**, полная копия
  `ip_plan_docker/` (сделана через `git archive HEAD` — только
  версионированные файлы, без `.env`/БД/`data/`), свой git-репозиторий.
  `ip_plan_docker/` — **read-only**, не трогаем.
- Порт **8090** (в `ip_plan_docker` — 8050), свои имена cookie сессии/CSRF
  (`ipplanreact_*` вместо `ipplan_*` — оба стенда на одном hostname,
  разные порты, куки браузер по порту не различает).
- На React переезжают **только таблица + тулбар** (поиск/фильтры/колонки/
  bulk-delete/счётчики). Модалки (`ip_add_modal.html`, `ip_edit_modal.html`,
  `help_modal.html`, `os_catalog_manage.html`) остаются HTMX — не связаны с
  проблемой ресайза, переписывание добавило бы риск без выгоды.
- ACL-раздел (`acl_lookup`, `acl_detail`, `acl_object_group_detail`) — не
  трогаем, отдельная DataTables-таблица вне этой задачи.
- Cutover на прод — вне рамок этого дизайна: оба стенда работают
  параллельно на одном NetBox, решение о переключении — отдельно, когда
  React-версия будет проверена.

## Архитектура

React-остров монтируется в `<div id="ip-table-root">` внутри
`ip_table.html` (тот же паттерн, что сейчас `x-data` у Alpine для модалок).
Django рендерит страницу целиком (hero, тулбар-обёртку, пустой
root-`div`), но больше не рендерит `<tbody>` — вместо
`{% for ip in ip_addresses %}` React делает `fetch` к новому JSON-эндпоинту
при монтировании.

## Бэкенд: JSON-эндпоинт

Новая view `GET /ip/list.json`:

```python
@ensure_csrf_cookie
def ip_list_json(request):
    client = NetBoxClient()
    ip_addresses = client.list_ip_addresses()
    _attach_roles_bulk(client, ip_addresses)
    return JsonResponse({
        "results": [serialize_ip(ip) for ip in ip_addresses],
        "prefixes": [p.prefix for p in client.list_prefixes()],
    })
```

`serialize_ip()` переносит на бэкенд логику 5 фильтров из
`templatetags/ip_plan_extras.py` (`assignment_role`, `guest_os`,
`effective_description`, `description_hint`, `ip_only`) — сейчас они
считаются в Jinja при рендере `<td>`, а должны попасть в JSON готовыми
полями, чтобы React не дублировал бизнес-логику. Сами фильтры не
удаляются — используются на других страницах.

`ip_list` (HTML-view) упрощается: убирается сборка `ip_addresses` в
контекст, шаблон не рендерит `<tbody>`.

`PATCHABLE_FIELDS`, `/patch/`, `/delete/`, экшены-эндпоинты — **не
меняются**, уже готовый JSON API.

## Фронтенд: TanStack Table

`@tanstack/react-table`, `useReactTable()` с 16 `columnDefs` (1:1 с
текущими `<th data-col-key>`). Состояние колонок — обычный React state,
сериализуется в localStorage через `useEffect`:

- `columnSizing` — резайз (замена ручных `FIXED_COLUMN_WIDTHS`/
  `applyColumnWidths()`). Драг за `header.getResizeHandler()`, пишем в
  localStorage по `mouseup`.
- `columnVisibility` — замена `COLUMN_VISIBILITY_STORAGE_KEY` (версия
  ключа растёт при изменении набора колонок, как сейчас v6→v7).
- `columnPinning: { left: ['select', 'address'] }` — замена FixedColumns +
  весь блок `applyFixedColumnOffsets()` (TanStack сам считает нарастающую
  сумму ширин закреплённых колонок).
- `columnOrder` — **новое**, drag-переупорядочивание колонок.
- `sorting: SortingState[]` — **новое**, мульти-сортировка (shift+клик).

Резиновые колонки (сейчас `STRETCHY_COLUMNS`/`STRETCHY_MIN_WIDTHS`) —
`minSize` в columnDefs + CSS `flex: 1 1 auto` на одной эластичной
колонке; TanStack не даёт это из коробки, логика остаётся нашей, но без
хаков вокруг `table-layout:fixed`.

**Новое сверху** (выбрано пользователем, остальное — сознательно не
включено, YAGNI): мульти-сортировка, drag-переупорядочивание колонок,
автоподбор ширины по двойному клику на границе. Не включены: виртуализация
строк, сохранённые пресеты колонок, экспорт текущего вида, плотность
строк — можно добавить позже отдельной итерацией.

## Checklist паритета функционала

| Функция сейчас | Механизм сейчас | Механизм в React/TanStack |
|---|---|---|
| Гэп-строки свободных IP в диапазоне | `insertGapRow()` по разнице `ipv4ToInt` соседей | Та же функция, применяется к отфильтрованному массиву перед `getRowModel()` через `useMemo` |
| Поиск/статус/подсеть/роль-фильтры | `$.fn.dataTable.ext.search.push()` + built-in global search | `columnFilters`/`globalFilterFn`, тот же `ipInPrefix()` как `filterFn` |
| Выбор строк + bulk-delete | Set `selectedRowIds`, ручная синхронизация при пагинации | `rowSelection` — нативный TanStack state |
| Kebab-меню + позиционирование у края экрана | `positionKebabMenu()`, ручной DOM вне ячейки | React-портал (`createPortal`), та же геометрия позиционирования |
| OS-меню (поиск + подменю версий) | `startOsMenuEdit()`, ~150 строк ручного DOM | Компонент `<OsMenu>`, та же логика фильтрации `OS_CATALOG_OPTIONS` |
| Copy-кнопки, inline edit, `patchJson`/`fetchWithCsrf` | Функции в `app.js` | Переносятся почти без изменений в `api.js`/хуки — уже JSON |
| Размер страницы (sessionStorage) | `PAGE_LENGTH_STORAGE_KEY` | `pagination` state TanStack + тот же ключ |
| Счётчики (total/active/inactive, "Показано X из Y") | Пересчёт на каждый `draw()` | `useMemo` от отфильтрованного массива |
| Loading-overlay на первую отрисовку | Скрывается после `initIpTable()` | Скрывается после первого `fetch` + первого рендера |

## Сборка и деплой

Новая поддиректория `ip_plan/static_src/ip_table/` (React-исходники:
`main.jsx`, `TableRoot.jsx`, `columns.jsx`, `api.js`, компоненты).
`package.json` + `vite.config.js` в корне проекта. Зависимости: `react`,
`react-dom`, `@tanstack/react-table`.

**Дев-режим:** `npm run dev` — Vite dev-server с HMR на отдельном порту
(5173), Django-шаблон в дебаге подключает `@vite/client`.

**Прод-сборка:** `npm run build` → `dist/` с хэшами → копируется в
`ip_plan/static/ip_plan/dist/` (Django collectstatic подхватывает как
обычные статические файлы, без `django-vite`). В `Dockerfile` — новый
build-stage `FROM node:20-alpine AS frontend-build` перед основным
Python-stage; в финальный образ попадает только `dist/`, не
`node_modules`.

Это первый Node.js в проекте — при появлении отдельного CI/CD-пайплайна
для `ip_plan_docker_react` (по аналогии с `netbox-sync`, где уже есть
GitLab CI c раннером на прод-хосте) нужно будет завести сборку фронтенда
тем же образом, что и Python-часть.

## Миграция и тестирование

Оба стенда (8050 — оригинал, 8090 — React) смотрят в один и тот же
NetBox — можно открыть рядом и сверить построчно: резайз/pin/sort/
фильтры/inline-edit/kebab-действия/bulk-delete на одних и тех же реальных
данных. Отдельно стоит проверить численно, что derived-поля
(`effective_description`, `guest_os`) совпадают между старым Django-
фильтром и новым Python-сериализатором на всём датасете, а не только "на
глаз" в браузере.

## Известные побочные находки (не в скоупе этой задачи)

- `tokens.txt` в корне репозитория содержит закоммиченный живой Bearer-
  токен NetBox — унаследовано из `ip_plan_docker` через `git archive`.
  Существовало и в оригинале, чинить нужно отдельно (ревокация +
  `.gitignore` + переписывание истории), не как часть переписывания
  таблицы.
