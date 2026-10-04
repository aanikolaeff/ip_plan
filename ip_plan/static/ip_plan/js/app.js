    // Скрывает оверлей-загрузчик таблицы (см. ip_table.html) — только при
    // первой инициализации, чтобы пользователь не видел промежуточные
    // состояния (сервер рисует все колонки видимыми и без сортировки/
    // ширин из localStorage — JS приводит это к финальному виду не сразу).
    // Защитный таймаут — на случай, если что-то пойдёт не так до того, как
    // initIpTable() успеет нормально завершиться, оверлей не должен висеть
    // вечно.
    let tableRevealedOnce = false;
    function revealTableAfterFirstLoad() {
      if (tableRevealedOnce) return;
      tableRevealedOnce = true;
      document.getElementById('table-loading-overlay')?.remove();
    }
    setTimeout(revealTableAfterFirstLoad, 5000);

    // Статусы для select-инпута inline-редактирования (общие для всех строк).
    window.IP_STATUS_OPTIONS = [
      { value: 'active', label: 'Active' },
      { value: 'reserved', label: 'Reserved' },
      { value: 'deprecated', label: 'Deprecated' },
      { value: 'dhcp', label: 'DHCP' },
      { value: 'slaac', label: 'SLAAC' },
    ];

    function getCookie(name) {
      const match = document.cookie.match(new RegExp('(^|; )' + name + '=([^;]*)'));
      return match ? decodeURIComponent(match[2]) : null;
    }

    // Задаётся в base.html из request.META.SCRIPT_NAME (см. FORCE_SCRIPT_NAME
    // в settings.py) — пусто, если приложение в корне домена.
    const URL_PREFIX = window.URL_PREFIX || '';

    // Задаётся в base.html из settings.CSRF_COOKIE_NAME — своё имя куки
    // (не дефолтное Django "csrftoken"), чтобы не конфликтовать с куки
    // NetBox при открытии обоих приложений на одном hostname.
    const CSRF_COOKIE_NAME = window.CSRF_COOKIE_NAME || 'csrftoken';

    // window.OS_CATALOG_OPTIONS задаётся в base.html из admin/os_catalog.json
    // (см. ip_plan/services/os_catalog.py) —
    // {категория: [{name, versions: [...]}, ...]}. Правится не через UI
    // таблицы, а через страницу /os-catalog/ или руками в файле.
    function osCatalogFlatNames() {
      const grouped = window.OS_CATALOG_OPTIONS || {};
      const names = [];
      Object.values(grouped).forEach((entries) => entries.forEach((e) => names.push(e.name)));
      return names;
    }

    function osCatalogFindEntry(name) {
      const grouped = window.OS_CATALOG_OPTIONS || {};
      for (const entries of Object.values(grouped)) {
        const found = entries.find((e) => e.name === name);
        if (found) return found;
      }
      return null;
    }

    // Подбирает (entry, version) под уже сохранённое значение ячейки —
    // либо точное совпадение с именем (без версии), либо "Имя Версия" из
    // одной из версий этого имени. Нужно, чтобы при повторном открытии
    // редактирования каскад сразу показывал то, что реально выбрано.
    function osCatalogMatchValue(value) {
      if (!value) return { entry: null, version: '' };
      const grouped = window.OS_CATALOG_OPTIONS || {};
      for (const entries of Object.values(grouped)) {
        for (const entry of entries) {
          if (value === entry.name) return { entry, version: '' };
          const version = (entry.versions || []).find((v) => value === `${entry.name} ${v}`);
          if (version) return { entry, version };
        }
      }
      return { entry: null, version: '' };
    }

    function populateOsDatalist() {
      const datalist = document.getElementById('os-catalog-datalist');
      if (!datalist) return;
      datalist.innerHTML = '';
      osCatalogFlatNames().forEach((value) => {
        const opt = document.createElement('option');
        opt.value = value;
        datalist.appendChild(opt);
      });
    }
    document.addEventListener('DOMContentLoaded', populateOsDatalist);

    function csrfTokenLooksValid(token) {
      return !!token && (token.length === 32 || token.length === 64);
    }

    // Лёгкий пинг (без обращения к NetBox) — продлевает CSRF-cookie, пока
    // страница открыта. Дёргается по таймеру и при возврате на вкладку, чтобы
    // токен не "протухал" без перезагрузки страницы.
    async function refreshCsrfToken() {
      try {
        await fetch(URL_PREFIX + '/csrf/', { credentials: 'same-origin', cache: 'no-store' });
      } catch (e) { /* сети нет — попробуем по следующему тику/событию */ }
      return getCookie(CSRF_COOKIE_NAME);
    }

    const CSRF_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
    setInterval(refreshCsrfToken, CSRF_REFRESH_INTERVAL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') refreshCsrfToken();
    });

    // Запрос с автоматическим обновлением CSRF-токена: если cookie выглядит
    // протухшей ИЛИ сервер ответил 403 — обновляем токен и пробуем ещё раз,
    // прозрачно для пользователя, без перезагрузки страницы.
    async function fetchWithCsrf(url, method, body) {
      const buildOptions = (csrfToken) => {
        const options = { method, headers: { 'X-CSRFToken': csrfToken } };
        if (body !== undefined) {
          options.headers['Content-Type'] = 'application/json';
          options.body = JSON.stringify(body);
        }
        return options;
      };

      let csrfToken = getCookie(CSRF_COOKIE_NAME);
      if (!csrfTokenLooksValid(csrfToken)) {
        csrfToken = await refreshCsrfToken();
      }
      let response = await fetch(url, buildOptions(csrfToken));
      if (response.status === 403) {
        csrfToken = await refreshCsrfToken();
        response = await fetch(url, buildOptions(csrfToken));
      }
      return response;
    }

    function patchJson(url, body) {
      return fetchWithCsrf(url, 'PATCH', body);
    }

    // По умолчанию htmx 2.x НЕ подставляет содержимое ответа при статусе
    // 4xx/5xx (shouldSwap:false) — формы add/save при ошибке валидации
    // (HTTP 400, форма с {{ form.errors }} в теле) рисовались бы "в никуда":
    // сервер всё корректно возвращает, но пользователь этого не видит.
    // Разрешаем подстановку явно для 400 — именно так view возвращает
    // переотрисованную форму с ошибками.
    // 409 — правки конфигурации синка заблокированы, пока идёт задание или
    // nmap (KTD18): ответ — партиал ошибки с причиной, его тоже показываем.
    // Форма, которая сама стоит в модалке, при 409 не заменяется ответом:
    // она остаётся заполненной, причина — тостом (иначе ответ стёр бы всё
    // введённое). Формы на странице получают ответ в модалку, как раньше.
    document.body.addEventListener('htmx:beforeSwap', function (evt) {
      const status = evt.detail.xhr && evt.detail.xhr.status;
      if (status === 409) {
        const elt = evt.detail.requestConfig && evt.detail.requestConfig.elt;
        const modal = document.getElementById('modal-content');
        if (elt && modal && modal.contains(elt)) {
          evt.detail.shouldSwap = false;
          evt.detail.isError = false;
          const doc = new DOMParser().parseFromString(evt.detail.serverResponse || '', 'text/html');
          const text = (doc.querySelector('.alert-modal-error') || doc.body).textContent.trim();
          window.dispatchEvent(new CustomEvent('show-toast', {
            detail: { message: text || 'Сохранение отклонено (409)', kind: 'error' },
          }));
          return;
        }
      }
      if (status === 400 || status === 409) {
        evt.detail.shouldSwap = true;
        evt.detail.isError = false;
      }
    });

    // Блокировка правок conf (KTD18): на страницах «Настроек» с плашкой
    // ([data-conf-locked] в settings_base.html) кнопки сохранения и удаления
    // форм, пишущих в conf, отключаются — и на странице, и в модалке,
    // которую htmx подгружает позже. Проверка на сервере (409) остаётся
    // главной; это — чтобы человек не заполнял форму впустую.
    const CONF_WRITE_URL = /\/settings\/.*(\/save\/|\/delete\/|\/import\/(preview|apply)\/)$/;
    function lockConfForms(root) {
      if (!document.querySelector('[data-conf-locked]')) return;
      (root || document).querySelectorAll('form').forEach(function (form) {
        const url = form.getAttribute('hx-post') || form.getAttribute('action') || '';
        if (!CONF_WRITE_URL.test(url) || /\/settings\/(ldap|users)\//.test(url)) return;
        form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (btn) {
          btn.disabled = true;
          btn.title = 'Правки конфигурации синка заблокированы: идёт задание или nmap';
        });
        document.querySelectorAll('button[form="' + form.id + '"]').forEach(function (btn) {
          if (form.id) { btn.disabled = true; }
        });
      });
    }
    document.addEventListener('DOMContentLoaded', function () { lockConfForms(document); });
    document.body.addEventListener('htmx:afterSettle', function (evt) { lockConfForms(evt.target); });

    // Опрос страницы задания (job_detail): тело заменяется целиком, а
    // прокрутка вывода ([data-keep-scroll]) сохраняется — у низа остаётся у
    // низа (следим за хвостом), иначе остаётся на месте.
    const keptScroll = {};
    document.body.addEventListener('htmx:beforeSwap', function () {
      document.querySelectorAll('[data-keep-scroll]').forEach(function (el) {
        keptScroll[el.dataset.keepScroll] = {
          top: el.scrollTop,
          atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 4,
        };
      });
    });
    document.addEventListener('DOMContentLoaded', function () {
      document.querySelectorAll('[data-keep-scroll][data-start-bottom]').forEach(function (el) {
        el.scrollTop = el.scrollHeight;
      });
    });
    document.body.addEventListener('htmx:afterSwap', function () {
      document.querySelectorAll('[data-keep-scroll]').forEach(function (el) {
        const saved = keptScroll[el.dataset.keepScroll];
        if (!saved) return;
        el.scrollTop = saved.atBottom ? el.scrollHeight : saved.top;
      });
    });

    // "2026-09-10 03:00:01" (см. sync_log.py: header.started, формат
    // full_sync.sh — date '+%Y-%m-%d %H:%M:%S') -> "10.09 03:00" — для
    // постоянно видимого индикатора в шапке секунды не нужны, полная
    // строка по-прежнему видна в развёрнутом дропдауне/логе.
    function formatSyncStarted(raw) {
      const m = raw && raw.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
      if (!m) return raw || '—';
      const [, , month, day, hour, minute] = m;
      return `${day}.${month} ${hour}:${minute}`;
    }

    document.addEventListener('alpine:init', () => {
      // Глобальное состояние модалки: открыта/закрыта, есть ли несохранённые изменения.
      Alpine.store('modal', {
        open: false,
        dirty: false,
        requestClose() {
          if (this.dirty && !window.confirm('Закрыть без сохранения изменений?')) return;
          this.open = false;
          this.dirty = false;
        },
      });

      // Правая панель (drawer) — карточка адреса; то же, что $store.modal,
      // но для бокового окна. Сервер закрывает её HX-Trigger'ом close-drawer.
      Alpine.store('drawer', {
        open: false,
        dirty: false,
        requestClose() {
          if (this.dirty && !window.confirm('Закрыть без сохранения изменений?')) return;
          this.open = false;
          this.dirty = false;
          const box = document.getElementById('drawer-content');
          if (box) box.innerHTML = '';
        },
      });
      window.addEventListener('close-drawer', () => {
        const box = document.getElementById('drawer-content');
        if (box) box.innerHTML = '';
      });

      // ⌘↵ / Ctrl+Enter — отправить форму открытой панели или модалки.
      window.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return;
        const host = Alpine.store('drawer').open ? '#drawer-content' : (Alpine.store('modal').open ? '#modal-content' : null);
        if (!host) return;
        const form = document.querySelector(host + ' form');
        if (form) { e.preventDefault(); form.requestSubmit(); }
      });

      // Модалка «Новый IP-адрес» (ip_add_modal.html): подсказка «адрес
      // свободен/занят» по списку свободных адресов, переданному таблицей,
      // чипы свободных, комментарий по кнопке.
      // initialAddress — из сервера, вторым аргументом: x-model перезаписал
      // бы value инпута пустой строкой модели раньше, чем init() успел бы
      // её прочитать.
      Alpine.data('ipAddForm', (free, initialAddress) => ({
        free: Array.isArray(free) ? free : [],
        address: initialAddress || '',
        comment: false,
        init() {
          const input = this.$el.querySelector('input[name="address"]');
          if (input && !this.address) input.focus();
          const c = this.$el.querySelector('textarea[name="comments"]');
          if (c && c.value) this.comment = true;
        },
        pick(a) {
          const mask = (this.address.split('/')[1] || '').trim();
          this.address = a + (mask ? '/' + mask : '');
          const input = this.$el.querySelector('input[name="address"]');
          if (input) { input.value = this.address; input.focus(); }
        },
        freeState() {
          if (!this.address) return 'is-empty';
          if (!this.free.length) return 'is-unknown';
          return this.free.includes(this.address.split('/')[0]) ? 'is-free' : 'is-unknown';
        },
        freeLabel() {
          const s = this.freeState();
          return s === 'is-free' ? '✓ Адрес свободен' : (s === 'is-unknown' ? 'Проверится при сохранении' : '');
        },
      }));

      // Панель карточки адреса (ip_edit_panel.html): действия по адресу
      // (проверка доступности, порты, DNS — те же эндпоинты, что у меню
      // строки), удаление, подпись статуса рядом с адресом.
      Alpine.data('ipEditForm', () => ({
        dirty: false,
        result: '',
        resultKind: '',
        status: (window.__ipEdit || {}).status || 'active',
        statusLabel() { return ((window.__ipEdit || {}).labels || {})[this.status] || this.status; },
        copy(text) { navigator.clipboard.writeText(text); window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'Адрес скопирован', kind: 'success' } })); },
        async run(kind, started) {
          const id = (window.__ipEdit || {}).id;
          if (!id) return;
          this.result = started; this.resultKind = '';
          try {
            const resp = await fetch((window.URL_PREFIX || '') + '/ip/' + id + '/' + kind + '/', {
              method: 'POST', credentials: 'same-origin',
              headers: { 'X-CSRFToken': getCookie(CSRF_COOKIE_NAME) },
            });
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || ('HTTP ' + resp.status));
            if (kind === 'check') {
              this.result = data.alive ? ('Доступен, ' + (data.latency_ms != null ? data.latency_ms + ' мс' : 'ответил') ) : 'Не отвечает';
              this.resultKind = data.alive ? 'is-ok' : 'is-bad';
            } else if (kind === 'scan-ports') {
              this.result = data.ports ? ('Открытые порты: ' + data.ports) : 'Открытых портов не найдено';
              this.resultKind = 'is-ok';
            } else {
              this.result = data.resolved ? ('DNS: ' + data.resolved + (data.action === 'saved' ? ' (сохранено)' : '')) : 'Имя не резолвится';
              this.resultKind = data.resolved ? 'is-ok' : 'is-bad';
            }
            window.dispatchEvent(new CustomEvent('ip-addresses-changed'));
          } catch (err) {
            this.result = 'Ошибка: ' + err.message; this.resultKind = 'is-bad';
          }
        },
        async remove() {
          const id = (window.__ipEdit || {}).id;
          if (!id || !window.confirm('Удалить этот IP-адрес из NetBox?')) return;
          const resp = await fetch((window.URL_PREFIX || '') + '/ip/' + id + '/delete/', {
            method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRFToken': getCookie(CSRF_COOKIE_NAME) },
          });
          if (resp.ok) {
            Alpine.store('drawer').dirty = false;
            window.dispatchEvent(new CustomEvent('close-drawer'));
            window.dispatchEvent(new CustomEvent('ip-addresses-changed'));
            window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'IP-адрес удалён', kind: 'success' } }));
          } else {
            window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'Не удалось удалить адрес', kind: 'error' } }));
          }
        },
      }));

      // Сводка последнего прогона netbox-sync (ip_plan/services/sync_log.py).
      // Глобальный $store, а не локальное состояние компонента — читается
      // сразу в ДВУХ разных местах шапки: постоянный индикатор рядом с
      // вкладками (.nav-sync-status, nav-left) и развёрнутый дропдаун
      // "Настройки" (тема + шаги, nav-right) — компонентное состояние
      // означало бы либо дублировать fetch на каждое место, либо тянуть
      // x-data одного в разметку другого через разные ветки шапки.
      Alpine.store('sync', {
        log: null,
        loading: false,
        error: null,
        async refresh() {
          this.loading = true;
          this.error = null;
          try {
            const resp = await fetch((window.URL_PREFIX || '') + '/sync-log/summary.json', { credentials: 'same-origin' });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            this.log = await resp.json();
          } catch (e) {
            this.error = 'Не удалось загрузить сводку netbox-sync';
          } finally {
            this.loading = false;
          }
        },
        // Текст времени в постоянном индикаторе — компактная дата без
        // секунд ("10.09 03:00"), полная строка с env/шагами по-прежнему
        // только в развёрнутом дропдауне/модалке лога.
        timeLabel() {
          if (this.loading && !this.log) return 'загрузка…';
          if (this.error) return 'ошибка';
          if (!this.log || !this.log.available) return 'нет прогонов';
          return formatSyncStarted(this.log.started_at);
        },
        // Статус рядом со временем — тот же overall (OK/FAILED/RUNNING/
        // UNKNOWN), что и .sync-log-badge внутри дропдауна; null, пока
        // время само по себе ещё не готово показывать статус отдельно
        // (загрузка/ошибка/нет прогонов — это уже видно по тексту времени).
        statusText() {
          return this.log && this.log.available ? this.log.overall : null;
        },
        statusClass() {
          if (this.error || !this.log || !this.log.available) return 'unknown';
          return this.log.overall.toLowerCase();
        },
      });
      Alpine.store('sync').refresh();

      // Состояние сборщика заданий netbox-sync (worker_health.banner через
      // /worker/health.json): баннер под шапкой и индикатор «выполняется …».
      // Сервер сам кэширует /health (15–30 с) и ходит к сборщику с таймаутом,
      // так что опрос раз в 30 с не нагружает ни web, ни сборщик. Страница
      // от этого запроса не ждёт ничего: баннер дорисовывается сам. Пока
      // вкладка скрыта, опроса нет; вернулись на неё — сразу свежий ответ.
      Alpine.store('worker', {
        level: '',
        text: '',
        link: '',
        linkText: '',
        running: [],
        async refresh() {
          try {
            const resp = await fetch((window.URL_PREFIX || '') + '/worker/health.json', { credentials: 'same-origin' });
            if (!resp.ok) return;
            const data = await resp.json();
            // Присваивать только изменившееся: каждое присваивание
            // перерисовывает баннер и индикатор, а ответ почти всегда прежний.
            const next = {
              level: data.level || '', text: data.text || '', link: data.link || '',
              linkText: data.link_text || '',
            };
            for (const key in next) {
              if (this[key] !== next[key]) this[key] = next[key];
            }
            const running = data.running || [];
            if (JSON.stringify(running) !== JSON.stringify(this.running)) this.running = running;
          } catch (e) {
            // Сеть до самого ip_plan пропала — баннер сборщика тут ни при чём.
          }
        },
      });
      if (window.IP_PLAN_WORKER) {
        let workerTimer = null;
        const startWorkerPoll = () => {
          if (workerTimer) return;
          Alpine.store('worker').refresh();
          workerTimer = setInterval(() => Alpine.store('worker').refresh(), 30000);
        };
        const stopWorkerPoll = () => {
          clearInterval(workerTimer);
          workerTimer = null;
        };
        document.addEventListener('visibilitychange', () => {
          if (document.hidden) stopWorkerPoll();
          else startWorkerPoll();
        });
        if (!document.hidden) startWorkerPoll();
      }

      // Dropdown "Настройки" в шапке — тема + сводка netbox-sync (сводка —
      // через $store.sync выше, здесь только open/close и переключатель
      // темы). Именованный компонент, а не инлайн x-data прямо в base.html
      // (как раньше, пока там была только тема) — дальнейшие настройки
      // будут добавляться в ЭТОТ ЖЕ блок, не меняя место в шапке.
      // Оболочка (base.html, x-data="shell" на <body>): боковое меню
      // свёрнуто/развёрнуто (localStorage 'ip_plan_sidebar', до загрузки
      // Alpine то же состояние даёт <html data-sidebar>), меню площадок,
      // тема. Клавиша «[» сворачивает меню, как в макете.
      Alpine.data('shell', () => ({
        collapsed: localStorage.getItem('ip_plan_sidebar') === 'collapsed',
        siteMenu: false,
        siteQuery: '',
        theme: (localStorage.getItem('ip_plan_theme') === 'dark' ? 'dark' : 'light'),
        init() {
          const aside = this.$el.querySelector('.sb');
          if (aside) aside.classList.add('alpine-ready');
          window.addEventListener('keydown', (e) => {
            if (e.key !== '[' || e.ctrlKey || e.metaKey || e.altKey) return;
            if (/^(input|textarea|select)$/i.test(e.target.tagName) || e.target.isContentEditable) return;
            this.toggleSidebar();
          });
        },
        toggleSidebar() {
          this.collapsed = !this.collapsed;
          localStorage.setItem('ip_plan_sidebar', this.collapsed ? 'collapsed' : 'expanded');
          if (this.collapsed) document.documentElement.setAttribute('data-sidebar', 'collapsed');
          else document.documentElement.removeAttribute('data-sidebar');
        },
        toggleTheme() {
          this.theme = this.theme === 'dark' ? 'light' : 'dark';
          window.setIpPlanTheme(this.theme);
        },
      }));

      Alpine.data('navSettings', () => ({
        open: false,
        theme: (localStorage.getItem('ip_plan_theme') === 'dark' ? 'dark' : 'light'),
        toggle() {
          this.open = !this.open;
        },
      }));

      // Chip-инпут общего назначения — изначально только для "Тэгов",
      // теперь и для "Версий" в справочнике ОС (см. _os_catalog_entry_form.html).
      // fieldName — имя скрытого инпута, в который синхронизируются чипы.
      Alpine.data('chipInput', (initialCsv, fieldName) => ({
        draft: '',
        items: initialCsv ? initialCsv.split(',').map((t) => t.trim()).filter(Boolean) : [],
        fieldName: fieldName || 'tags',
        root: null,
        addItem() {
          const value = this.draft.trim();
          if (value && !this.items.includes(value)) {
            this.items.push(value);
          }
          this.draft = '';
          this.syncHidden();
        },
        removeItem(idx) {
          this.items.splice(idx, 1);
          this.syncHidden();
        },
        syncHidden() {
          // ВАЖНО: $el внутри addItem()/removeItem() — это элемент, на
          // котором СЕЙЧАС вычисляется директива (input при @keydown,
          // кнопка ✕ при @click), а не корень компонента! Поэтому
          // querySelector от $el тут ничего не находил, и чипы никогда не
          // попадали в реально отправляемое скрытое поле. root запоминаем
          // один раз в init(), где $el гарантированно равен корню.
          this.root.querySelector(`input[name="${this.fieldName}"]`).value = this.items.join(',');
        },
        init() {
          this.root = this.$el;
          this.syncHidden();
        },
      }));

    });

    // --- Inline-редактирование ячеек таблицы ---
    // ВАЖНО: это обычный JS с делегированием событий на #ip-table-body, а НЕ
    // Alpine-компоненты на каждой ячейке. DataTables при каждой перерисовке
    // (сортировка/поиск/страница) вынимает и переставляет DOM-узлы строк, а
    // <tbody> при этом не пересоздаётся — поэтому один обработчик на нём
    // переживает любые перерисовки, в отличие от привязок на самих строках.
    function flashCell(cell, cls) {
      cell.classList.add(cls);
      setTimeout(() => cell.classList.remove(cls), 600);
    }

    function escapeHtml(str) {
      const div = document.createElement('div');
      div.textContent = str == null ? '' : str;
      return div.innerHTML;
    }

    function renderCellDisplay(cell, field, value) {
      const display = cell.querySelector('.cell-display');
      if (field === 'status') {
        const opt = window.IP_STATUS_OPTIONS.find((o) => o.value === value);
        display.innerHTML = `<span class="badge-status" data-status="${escapeHtml(value)}">${escapeHtml(opt ? opt.label : value)}</span>`;
        return;
      }
      const maxLen = parseInt(cell.dataset.maxLen || '0', 10);
      const text = value || '—';
      const truncated = maxLen && text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
      const titleAttr = field === 'dns_name' ? '' : ` title="${escapeHtml(value)}"`;
      const valueSpan = `<span class="cell-value"${titleAttr}>${escapeHtml(truncated)}</span>`;
      if (field === 'dns_name' && value) {
        const copyIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
        display.innerHTML = `<span class="cell-with-copy">${valueSpan}<button type="button" class="copy-btn" data-copy-value="${escapeHtml(value)}" title="Копировать">${copyIcon}</button></span>`;
      } else if (field === 'os_installed' && value) {
        const clearSvg = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>';
        display.innerHTML = `<span class="cell-with-copy">${valueSpan}<button type="button" class="cell-clear-btn" aria-label="Очистить OS Installed" title="Очистить">${clearSvg}</button></span>`;
      } else {
        display.innerHTML = valueSpan;
      }
    }

    async function saveInlineEdit(cell, field, newValue) {
      try {
        const response = await patchJson(`${URL_PREFIX}/ip/${cell.dataset.id}/patch/`, { [field]: newValue });
        if (response.ok) {
          cell.dataset.value = newValue;
          renderCellDisplay(cell, field, newValue);
          flashCell(cell, 'flash-success');
          window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'Сохранено', kind: 'success' } }));
        } else {
          flashCell(cell, 'flash-error');
          let message = 'Не удалось сохранить изменение';
          if (response.status === 403) {
            message = 'Сессия истекла — обновите страницу (F5)';
          } else {
            try {
              message = (await response.json()).error || message;
            } catch (e) { /* ответ без тела */ }
          }
          window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, kind: 'error' } }));
        }
      } catch (e) {
        flashCell(cell, 'flash-error');
        window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'NetBox недоступен', kind: 'error' } }));
      }
    }

    // Позиционирует панель (position:fixed, см. CSS) рядом с anchorRect,
    // прижимая к границам вьюпорта — общая логика и для самого меню "ОС",
    // и для его подменю версий.
    function positionFixedPanel(anchorRect, panel) {
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      let top = Math.min(anchorRect.bottom + 4, window.innerHeight - 8 - height);
      top = Math.max(top, 8);
      let left = Math.min(anchorRect.left, window.innerWidth - 8 - width);
      left = Math.max(left, 8);
      panel.style.top = `${top}px`;
      panel.style.left = `${left}px`;
    }

    // Подменю версий выезжает справа от родительского пункта; если не
    // помещается — переворачивается влево (как обычное контекстное меню).
    function positionSubmenu(parentRect, submenu) {
      const width = submenu.offsetWidth;
      const height = submenu.offsetHeight;
      let left = parentRect.right + 2;
      if (left + width > window.innerWidth - 8) {
        left = parentRect.left - width - 2;
      }
      left = Math.max(left, 8);
      let top = Math.min(parentRect.top, window.innerHeight - 8 - height);
      top = Math.max(top, 8);
      submenu.style.left = `${left}px`;
      submenu.style.top = `${top}px`;
    }

    // "Операционная система" — собственное hover-меню (не <select>): слева
    // список имён ОС/семейств с фильтром-поиском сверху, у записей с
    // версиями в справочнике — подменю версий выезжает справа при
    // наведении (или клике — для тач/доступности). Итоговое значение —
    // "Имя" (без версий) либо "Имя Версия". Значение, введённое в фильтре
    // и не совпадающее ни с одним именем, можно сохранить "как есть" —
    // замена прежнего "Другое…"-хака. Меню всегда монтируется в <body>
    // (не внутрь ячейки) — иначе обрезалось бы горизонтальным скроллом
    // DataTables (.dt-scroll-body), как и kebab-меню в предельном случае.
    function startOsMenuEdit(cell) {
      if (document.querySelector('.os-menu')) return;
      const field = cell.dataset.field;
      const originalValue = cell.dataset.value || '';
      cell.classList.add('os-menu-editing');
      const cellRect = cell.getBoundingClientRect();
      const grouped = window.OS_CATALOG_OPTIONS || {};

      const menu = document.createElement('div');
      menu.className = 'os-menu';

      const filterInput = document.createElement('input');
      filterInput.type = 'text';
      filterInput.className = 'os-menu-filter';
      filterInput.placeholder = 'Поиск...';
      menu.appendChild(filterInput);

      const list = document.createElement('div');
      list.className = 'os-menu-list';
      menu.appendChild(list);

      const customRow = document.createElement('div');
      customRow.className = 'os-menu-custom-row';
      customRow.style.display = 'none';
      menu.appendChild(customRow);

      let settled = false;
      let openSubmenuEl = null;
      let hoverCloseTimer = null;

      function closeSubmenu() {
        if (openSubmenuEl) {
          openSubmenuEl.classList.remove('show');
          openSubmenuEl = null;
        }
        list.querySelectorAll('.os-menu-item-active').forEach((el) => el.classList.remove('os-menu-item-active'));
      }

      function finish(newValue, shouldSave) {
        if (settled) return;
        settled = true;
        clearTimeout(hoverCloseTimer);
        document.removeEventListener('mousedown', onOutsideMouseDown, true);
        menu.remove();
        cell.classList.remove('os-menu-editing');
        if (shouldSave && newValue !== originalValue) {
          saveInlineEdit(cell, field, newValue);
        }
      }

      function onOutsideMouseDown(e) {
        if (!menu.contains(e.target) && !(openSubmenuEl && openSubmenuEl.contains(e.target))) finish(originalValue, false);
      }

      function openSubmenuFor(item, submenu) {
        if (openSubmenuEl === submenu) return;
        closeSubmenu();
        item.classList.add('os-menu-item-active');
        submenu.classList.add('show');
        positionSubmenu(item.getBoundingClientRect(), submenu);
        openSubmenuEl = submenu;
      }

      function renderList(filterText) {
        closeSubmenu();
        // Подменю версий рендерятся в <body> (см. ниже), не внутри list —
        // обычная очистка list.innerHTML их не затронет, удаляем явно
        // перед каждой перерисовкой, иначе при каждом вводе в фильтр
        // копились бы осиротевшие подменю прошлых рендеров.
        document.querySelectorAll('.os-menu-submenu').forEach((el) => el.remove());
        list.innerHTML = '';
        const needle = filterText.trim().toLowerCase();
        let anyVisible = false;
        let exactMatch = false;

        Object.keys(grouped).forEach((category) => {
          const entries = grouped[category].filter((e) => !needle || e.name.toLowerCase().includes(needle));
          if (!entries.length) return;
          anyVisible = true;

          const groupLabel = document.createElement('div');
          groupLabel.className = 'os-menu-group-label';
          groupLabel.textContent = category;
          list.appendChild(groupLabel);

          entries.forEach((entry) => {
            if (entry.name.toLowerCase() === needle) exactMatch = true;
            const item = document.createElement('div');
            item.className = 'os-menu-item';
            const label = document.createElement('span');
            label.textContent = entry.name;
            item.appendChild(label);

            if (entry.versions && entry.versions.length) {
              const arrow = document.createElement('span');
              arrow.className = 'os-menu-arrow';
              arrow.textContent = '▸';
              item.appendChild(arrow);

              const submenu = document.createElement('div');
              submenu.className = 'os-menu-submenu';
              entry.versions.forEach((v) => {
                const vItem = document.createElement('div');
                vItem.className = 'os-menu-item';
                vItem.textContent = v;
                vItem.addEventListener('click', (e) => {
                  e.stopPropagation();
                  finish(`${entry.name} ${v}`, true);
                });
                submenu.appendChild(vItem);
              });
              document.body.appendChild(submenu);

              item.addEventListener('mouseenter', () => {
                clearTimeout(hoverCloseTimer);
                openSubmenuFor(item, submenu);
              });
              item.addEventListener('mouseleave', () => {
                hoverCloseTimer = setTimeout(() => {
                  if (!submenu.matches(':hover')) closeSubmenu();
                }, 250);
              });
              submenu.addEventListener('mouseenter', () => clearTimeout(hoverCloseTimer));
              submenu.addEventListener('mouseleave', () => {
                hoverCloseTimer = setTimeout(closeSubmenu, 250);
              });
              item.addEventListener('click', () => openSubmenuFor(item, submenu));
            } else {
              item.addEventListener('click', () => finish(entry.name, true));
            }

            list.appendChild(item);
          });
        });

        if (!anyVisible) {
          const empty = document.createElement('div');
          empty.className = 'os-menu-empty';
          empty.textContent = 'Ничего не найдено';
          list.appendChild(empty);
        }

        if (needle && !exactMatch) {
          const typed = filterText.trim();
          customRow.textContent = `Использовать «${typed}» как есть`;
          customRow.style.display = 'block';
          customRow.onclick = () => finish(typed, true);
        } else {
          customRow.style.display = 'none';
        }
      }

      filterInput.addEventListener('input', () => renderList(filterInput.value));
      filterInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') finish(originalValue, false);
      });

      document.body.appendChild(menu);
      renderList('');
      positionFixedPanel(cellRect, menu);
      filterInput.focus();

      // Подменю версий рендерятся прямо в <body> (см. выше) — список их
      // элементов нужно убирать вместе с основным меню при закрытии.
      const origRemove = menu.remove.bind(menu);
      menu.remove = () => {
        document.querySelectorAll('.os-menu-submenu').forEach((el) => el.remove());
        origRemove();
      };

      // mousedown, не click — клик, которым открыли меню, успевает
      // завершиться раньше, чем навешивается этот обработчик.
      document.addEventListener('mousedown', onOutsideMouseDown, true);
    }

    function startInlineEdit(cell) {
      if (cell.querySelector('.inline-input, .inline-select')) return;
      if (cell.dataset.type === 'select-or-text') {
        startOsMenuEdit(cell);
        return;
      }
      const field = cell.dataset.field;
      const type = cell.dataset.type;
      const originalValue = cell.dataset.value || '';
      const display = cell.querySelector('.cell-display');
      display.style.display = 'none';

      let input;
      if (type === 'select') {
        input = document.createElement('select');
        input.className = 'inline-input inline-select';
        window.IP_STATUS_OPTIONS.forEach((opt) => {
          const optionEl = document.createElement('option');
          optionEl.value = opt.value;
          optionEl.textContent = opt.label;
          if (opt.value === originalValue) optionEl.selected = true;
          input.appendChild(optionEl);
        });
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.className = 'inline-input';
        input.value = originalValue;
      }
      cell.appendChild(input);
      input.focus();
      if (input.select) input.select();

      let settled = false;
      function finish(newValue, shouldSave) {
        if (settled) return;
        settled = true;
        input.remove();
        display.style.display = '';
        if (shouldSave && newValue !== originalValue) {
          saveInlineEdit(cell, field, newValue);
        }
      }

      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') finish(input.value, true);
        if (e.key === 'Escape') finish(originalValue, false);
      });
      input.addEventListener('blur', () => finish(input.value, true));
      if (type === 'select') {
        input.addEventListener('change', () => finish(input.value, true));
      }
    }

    // --- Фильтр по подсети: простая проверка вхождения IPv4-адреса в CIDR ---
    function ipv4ToInt(ip) {
      const parts = ip.split('.');
      if (parts.length !== 4) return null;
      return parts.reduce((acc, octet) => (acc << 8) + (parseInt(octet, 10) & 0xff), 0) >>> 0;
    }
    function ipInPrefix(addressWithMask, prefixCidr) {
      if (!addressWithMask || !prefixCidr) return false;
      const ip = addressWithMask.split('/')[0];
      const [netIp, netBitsStr] = prefixCidr.split('/');
      const netBits = parseInt(netBitsStr, 10);
      const ipInt = ipv4ToInt(ip);
      const netInt = ipv4ToInt(netIp);
      if (ipInt === null || netInt === null || Number.isNaN(netBits)) return false;
      const mask = netBits === 0 ? 0 : (0xffffffff << (32 - netBits)) >>> 0;
      return (ipInt & mask) === (netInt & mask);
    }
    function ipIntToStr(intVal) {
      return [(intVal >>> 24) & 0xff, (intVal >>> 16) & 0xff, (intVal >>> 8) & 0xff, intVal & 0xff].join('.');
    }
    function pluralizeAddresses(n) {
      const mod10 = n % 10;
      const mod100 = n % 100;
      if (mod100 >= 11 && mod100 <= 14) return 'адресов';
      if (mod10 === 1) return 'адрес';
      if (mod10 >= 2 && mod10 <= 4) return 'адреса';
      return 'адресов';
    }

    // --- Свободные диапазоны IP внутри выбранного префикса ---
    // Чисто визуальные <tr>, вставляемые в DOM после каждой перерисовки —
    // не часть данных DataTables, поэтому не сортируются/не фильтруются/не
    // выбираются чекбоксом сами по себе. Считаются только по строкам,
    // реально показанным на ТЕКУЩЕЙ странице (см. п.5 требований) — гэп,
    // уходящий за границу страницы, виден только с той стороны, что
    // приходится на первую/последнюю страницу выборки.
    function renderFreeRanges() {
      document.querySelectorAll('#ip-table-body tr.free-range-row').forEach((row) => row.remove());

      const prefixFilter = document.getElementById('prefix-filter');
      const prefixValue = prefixFilter ? prefixFilter.value : '';
      if (!prefixValue || !ipTable) return; // без выбранного префикса — диапазоны не показываем

      const [netStr, maskStr] = prefixValue.split('/');
      const mask = parseInt(maskStr, 10);
      const netInt = ipv4ToInt(netStr);
      if (netInt === null || Number.isNaN(mask) || mask < 0 || mask > 32) return;
      const size = Math.pow(2, 32 - mask);
      if (size <= 2) return; // /31, /32 — network/broadcast не выделяются, считать "свободное" нет смысла

      const firstHost = netInt + 1;
      const lastHost = netInt + size - 2;

      const tbody = document.getElementById('ip-table-body');
      const rows = Array.from(tbody.querySelectorAll('tr[data-address]'));
      if (!rows.length) return;

      const colCount = ipTable.columns().count();
      const pageInfo = ipTable.page.info();
      const isFirstPage = pageInfo.page === 0;
      const isLastPage = pageInfo.page === pageInfo.pages - 1;

      function insertGapRow(afterRow, afterIpInt, count) {
        if (count <= 0) return;
        const tr = document.createElement('tr');
        tr.className = 'free-range-row';
        const td = document.createElement('td');
        td.colSpan = colCount;
        td.innerHTML =
          '<div class="free-range-content">' +
          '<span class="free-range-label">● Свободно: ' + count + ' ' + pluralizeAddresses(count) + '</span>' +
          '<button type="button" class="free-range-add-btn" data-after="' + ipIntToStr(afterIpInt) + '" data-count="' + count + '" title="Добавить IP в этом диапазоне">+</button>' +
          '</div>';
        tr.appendChild(td);
        if (afterRow) afterRow.insertAdjacentElement('afterend', tr);
        else tbody.insertAdjacentElement('afterbegin', tr);
      }

      let prevIpInt = null;
      let prevRow = null;
      rows.forEach((row, idx) => {
        const ipInt = ipv4ToInt(row.dataset.address.split('/')[0]);
        if (ipInt === null) return;
        if (idx === 0 && isFirstPage) {
          insertGapRow(null, firstHost - 1, ipInt - firstHost);
        } else if (prevIpInt !== null) {
          insertGapRow(prevRow, prevIpInt, ipInt - prevIpInt - 1);
        }
        prevIpInt = ipInt;
        prevRow = row;
      });
      if (isLastPage && prevIpInt !== null) {
        insertGapRow(prevRow, prevIpInt, lastHost - prevIpInt);
      }
    }
    // Регистрируется один раз: учитывается DataTables при каждом draw() наравне
    // с глобальным поиском и поколоночными фильтрами.
    $.fn.dataTable.ext.search.push(function (settings, searchData, index) {
      const prefixFilter = document.getElementById('prefix-filter');
      if (!prefixFilter || !prefixFilter.value) return true;
      const api = new $.fn.dataTable.Api(settings);
      const row = api.row(index).node();
      return !!row && ipInPrefix(row.getAttribute('data-address'), prefixFilter.value);
    });

    // Открывает модалку добавления IP с предзаполненным адресом (кнопка
    // "+" у строки свободного диапазона) — та же модалка, что у "+ Добавить
    // IP", но с динамическим query-параметром, поэтому через htmx.ajax(),
    // а не статический hx-get.
    function openAddIpModal(prefillAddress) {
      window.Alpine.store('modal').dirty = false;
      htmx.ajax('GET', `${URL_PREFIX}/ip/add/?address=${encodeURIComponent(prefillAddress)}`, {
        target: '#modal-content',
        indicator: '#modal-spinner',
      });
      window.dispatchEvent(new CustomEvent('open-modal'));
    }

    async function deleteIpRow(id, address) {
      if (!window.confirm(`Удалить IP-адрес ${address}? Это действие нельзя отменить.`)) return;
      try {
        const response = await fetchWithCsrf(`${URL_PREFIX}/ip/${id}/delete/`, 'DELETE');
        if (response.ok) {
          const row = document.getElementById(`ip-row-${id}`);
          if (row && ipTable && $.fn.DataTable.isDataTable('#ip-table')) {
            ipTable.row(row).remove().draw(false);
          } else if (row) {
            row.remove();
          }
          window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: `IP-адрес ${address} удалён`, kind: 'success' } }));
        } else {
          let message = 'Не удалось удалить IP-адрес';
          try {
            message = (await response.json()).error || message;
          } catch (e) { /* ответ без тела */ }
          window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, kind: 'error' } }));
        }
      } catch (e) {
        window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'NetBox недоступен', kind: 'error' } }));
      }
    }

    // --- Массовое удаление выбранных строк ---
    function openBulkDeleteConfirm() {
      const n = selectedRowIds.size;
      if (n === 0) return;
      const overlay = document.getElementById('bulk-delete-overlay');
      const text = document.getElementById('bulk-delete-text');
      const confirmBtn = document.getElementById('bulk-delete-confirm-btn');
      if (!overlay || !text || !confirmBtn) return;
      text.textContent = `Вы уверены, что хотите удалить ${n} записей? Это действие нельзя отменить.`;
      confirmBtn.textContent = `Удалить (${n})`;
      overlay.style.display = 'flex';
    }

    let bulkDeleteInProgress = false;

    function closeBulkDeleteConfirm() {
      if (bulkDeleteInProgress) return; // не закрывать поверх "идёт удаление"
      const overlay = document.getElementById('bulk-delete-overlay');
      if (overlay) overlay.style.display = 'none';
    }

    // DELETE параллельно через Promise.allSettled (не .all) — чтобы один
    // упавший запрос не "топил" уже успешные результаты остальных; в конце
    // отдельно сообщаем, какие именно адреса не удалились.
    async function bulkDeleteSelected() {
      const ids = Array.from(selectedRowIds);
      if (!ids.length) return;

      // Диалог подтверждения уже открыт и в фокусе пользователя — вместо
      // отдельного полноэкранного затемнения превращаем его же в индикатор
      // "идёт удаление": кнопки блокируются, на "Удалить" — тот же спиннер,
      // что в модалке редактирования. Закрывается сам по завершении.
      const confirmBtn = document.getElementById('bulk-delete-confirm-btn');
      const cancelBtn = document.getElementById('bulk-delete-cancel-btn');
      const closeXBtn = document.getElementById('bulk-delete-close-btn');
      const confirmBtnOriginalHtml = confirmBtn.innerHTML;
      bulkDeleteInProgress = true;
      confirmBtn.disabled = true;
      cancelBtn.disabled = true;
      closeXBtn.disabled = true;
      confirmBtn.innerHTML = '<span class="spinner-ring-sm"></span>Удаление…';

      const results = await Promise.allSettled(
        ids.map((id) =>
          fetchWithCsrf(`${URL_PREFIX}/ip/${id}/delete/`, 'DELETE').then((response) => ({ id, ok: response.ok }))
        )
      );

      bulkDeleteInProgress = false;
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
      closeXBtn.disabled = false;
      confirmBtn.innerHTML = confirmBtnOriginalHtml;
      closeBulkDeleteConfirm();

      const succeededIds = [];
      const failedAddresses = [];
      results.forEach((result, i) => {
        const id = ids[i];
        if (result.status === 'fulfilled' && result.value.ok) {
          succeededIds.push(id);
        } else {
          const row = document.getElementById(`ip-row-${id}`);
          failedAddresses.push(row ? row.dataset.address : id);
        }
      });

      succeededIds.forEach((id) => {
        const row = document.getElementById(`ip-row-${id}`);
        if (row && ipTable && $.fn.DataTable.isDataTable('#ip-table')) {
          ipTable.row(row).remove();
        } else if (row) {
          row.remove();
        }
        selectedRowIds.delete(id);
      });
      // Один общий draw() на все удалённые строки сразу — дешевле, чем
      // передёргивать таблицу на каждое удаление; он же сам обновит
      // счётчики "Показано X из Y" и статистику в шапке (обе уже
      // подписаны на событие 'draw', см. ipTable.on('draw', ...) ниже).
      if (ipTable && $.fn.DataTable.isDataTable('#ip-table')) ipTable.draw(false);

      updateSelectAllCheckboxState();
      updateBulkDeleteUI();

      if (succeededIds.length) {
        window.dispatchEvent(new CustomEvent('show-toast', {
          detail: { message: `Удалено ${succeededIds.length} записей`, kind: 'success' },
        }));
      }
      if (failedAddresses.length) {
        window.dispatchEvent(new CustomEvent('show-toast', {
          detail: { message: `Не удалось удалить: ${failedAddresses.join(', ')}`, kind: 'error' },
        }));
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      document.getElementById('bulk-delete-close-btn')?.addEventListener('click', closeBulkDeleteConfirm);
      document.getElementById('bulk-delete-cancel-btn')?.addEventListener('click', closeBulkDeleteConfirm);
      document.getElementById('bulk-delete-confirm-btn')?.addEventListener('click', bulkDeleteSelected);
      document.getElementById('bulk-delete-overlay')?.addEventListener('click', (e) => {
        if (e.target.id === 'bulk-delete-overlay') closeBulkDeleteConfirm();
      });
    });

    // --- Сетевые проверки из кебаб-меню: доступность (ping), порты (nmap), PTR DNS ---
    const NETWORK_ACTION_PATHS = { check: 'check', 'scan-ports': 'scan-ports', 'resolve-dns': 'resolve-dns' };
    const NETWORK_ACTION_START_MESSAGES = {
      check: (addr) => `Проверка доступности ${addr}…`,
      'scan-ports': (addr) => `Сканирование портов ${addr} (1–1000)…`,
      'resolve-dns': (addr) => `Резолв DNS для ${addr}…`,
    };

    function showToast(message, kind) {
      window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, kind } }));
    }

    function updateCellFromAction(id, field, value) {
      const cell = document.querySelector(`#ip-row-${id} td[data-field="${field}"]`);
      if (!cell) return;
      cell.dataset.value = value || '';
      renderCellDisplay(cell, field, value || '');
      flashCell(cell, 'flash-success');
    }

    async function runIpNetworkAction(action, id, address) {
      showToast(NETWORK_ACTION_START_MESSAGES[action](address), 'info');

      let response;
      try {
        response = await fetchWithCsrf(`${URL_PREFIX}/ip/${id}/${NETWORK_ACTION_PATHS[action]}/`, 'POST');
      } catch (e) {
        showToast('NetBox недоступен', 'error');
        return;
      }
      let data = {};
      try {
        data = await response.json();
      } catch (e) { /* ответ без тела */ }
      if (!response.ok) {
        showToast(data.error || 'Ошибка операции', 'error');
        return;
      }

      if (action === 'check') {
        if (data.status_changed) updateCellFromAction(id, 'status', 'active');
        const latency = data.latency_ms != null ? ` (${data.latency_ms} мс)` : '';
        const suffix = data.status_changed ? ' — статус → Active' : '';
        showToast(
          `${address}: ${data.alive ? 'доступен' + latency : 'не отвечает'}${suffix}`,
          data.alive ? 'success' : 'error',
        );
        if (data.suggest_status) {
          const ok = window.confirm(
            `${address} не отвечает на пинг, хотя статус сейчас Active. Изменить статус на Reserved?`
          );
          if (!ok) return;
          const patchResponse = await patchJson(`${URL_PREFIX}/ip/${id}/patch/`, { status: data.suggest_status });
          if (patchResponse.ok) {
            updateCellFromAction(id, 'status', data.suggest_status);
            showToast(`${address}: статус изменён на Reserved`, 'success');
          } else {
            showToast('Не удалось изменить статус', 'error');
          }
        }
        return;
      }

      if (action === 'scan-ports') {
        updateCellFromAction(id, 'open_ports', data.ports);
        showToast(
          data.ports ? `${address}: открытые порты — ${data.ports}` : `${address}: открытых портов не найдено`,
          'success',
        );
        return;
      }

      if (action === 'resolve-dns') {
        if (data.action === 'none') {
          showToast(
            data.resolved ? `${address}: имя уже совпадает с PTR (${data.resolved})` : `${address}: PTR-запись не найдена`,
            data.resolved ? 'success' : 'error',
          );
          return;
        }
        if (data.action === 'saved') {
          updateCellFromAction(id, 'dns_name', data.resolved);
          showToast(`${address}: DNS-имя сохранено — ${data.resolved}`, 'success');
          return;
        }
        if (data.action === 'confirm') {
          const ok = window.confirm(
            `Текущее DNS-имя «${data.current}» отличается от PTR «${data.resolved}». Заменить на значение из PTR?`
          );
          if (!ok) return;
          const patchResponse = await patchJson(`${URL_PREFIX}/ip/${id}/patch/`, { dns_name: data.resolved });
          if (patchResponse.ok) {
            updateCellFromAction(id, 'dns_name', data.resolved);
            showToast(`${address}: DNS-имя обновлено — ${data.resolved}`, 'success');
          } else {
            showToast('Не удалось сохранить DNS-имя', 'error');
          }
        }
      }
    }

    // .dt-scroll-body обрезает (overflow для горизонтального скролла
    // попутно клипает и по вертикали) всё, что выходит за её собственные
    // границы — а они кончаются сразу под последней строкой, а не у низа
    // вьюпорта. При маленькой выборке (мало строк, особо часто — ровно 1,
    // напр. поиск по точному адресу) этого места может не хватать ни
    // сверху, ни снизу в принципе — тогда переключаем меню в
    // position:fixed (см. .kebab-menu-fixed) — оно физически считается от
    // .table-wrapper (у него backdrop-filter), но раз у того
    // overflow:visible — меню всё равно убегает из-под обрезающего
    // .dt-scroll-body, куда бы его ни поставили top/down.
    function positionKebabMenu(kebabBtn, menu) {
      const btnRect = kebabBtn.getBoundingClientRect();
      const menuHeight = menu.offsetHeight;
      const scrollBody = document.querySelector('.dt-scroll-body');
      const sbRect = scrollBody ? scrollBody.getBoundingClientRect() : null;
      const clipBottom = sbRect ? Math.min(window.innerHeight, sbRect.bottom) : window.innerHeight;
      const clipTop = sbRect ? Math.max(0, sbRect.top) : 0;

      const fitsDown = btnRect.bottom + menuHeight <= clipBottom;
      const fitsUp = btnRect.top - menuHeight >= clipTop;

      if (fitsDown) return; // дефолтное положение и так открывается вниз
      if (fitsUp) {
        menu.classList.add('kebab-menu-up');
        return;
      }
      // Ни вверх, ни вниз в пределах таблицы — эвакуируем в fixed,
      // координаты — относительно .table-wrapper (его containing block).
      const wrapper = document.querySelector('.table-wrapper');
      const wrapperRect = wrapper.getBoundingClientRect();
      const menuWidth = menu.offsetWidth;
      let top = btnRect.bottom + 4 - wrapperRect.top;
      top = Math.min(top, window.innerHeight - 8 - menuHeight - wrapperRect.top);
      top = Math.max(top, 8 - wrapperRect.top);
      let left = btnRect.right - menuWidth - wrapperRect.left;
      left = Math.max(left, 8 - wrapperRect.left);
      menu.classList.add('kebab-menu-fixed');
      menu.style.top = `${top}px`;
      menu.style.left = `${left}px`;
    }

    // Делегирование событий на #ip-table-body: навешивается ОДИН раз (этот
    // элемент не пересоздаётся DataTables, в отличие от строк внутри него).
    (function setupRowDelegation() {
      const tbody = document.getElementById('ip-table-body');
      if (!tbody) return;

      tbody.addEventListener('dblclick', (e) => {
        if (e.target.closest('.copy-btn')) return;
        const cell = e.target.closest('td[data-editable="true"]');
        if (cell && cell.dataset.type !== 'select' && cell.dataset.type !== 'select-or-text') startInlineEdit(cell);
      });

      tbody.addEventListener('click', (e) => {
        const clearBtn = e.target.closest('.cell-clear-btn');
        if (clearBtn) {
          e.stopPropagation();
          const cell = clearBtn.closest('td[data-editable="true"]');
          if (!cell || cell.dataset.value === '') return;
          saveInlineEdit(cell, cell.dataset.field, '');
          clearBtn.remove();
          return;
        }
        const copyFieldBtn = e.target.closest('.copy-btn');
        if (copyFieldBtn) {
          navigator.clipboard.writeText(copyFieldBtn.dataset.copyValue);
          window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: 'Скопировано: ' + copyFieldBtn.dataset.copyValue, kind: 'success' } }));
          return;
        }
        const selectLikeCell = e.target.closest('td[data-editable="true"][data-type="select"], td[data-editable="true"][data-type="select-or-text"]');
        if (selectLikeCell && !e.target.closest('select, input')) {
          startInlineEdit(selectLikeCell);
          return;
        }
        const kebabBtn = e.target.closest('.btn-kebab');
        if (kebabBtn) {
          const menu = kebabBtn.nextElementSibling;
          const td = kebabBtn.closest('td');
          document.querySelectorAll('.kebab-menu.show').forEach((m) => { if (m !== menu) m.classList.remove('show'); });
          document.querySelectorAll('td.kebab-td-active').forEach((t) => { if (t !== td) t.classList.remove('kebab-td-active'); });
          // IP — зафиксированная (sticky) колонка: у каждой строки свой
          // отдельный слой отрисовки, и при открытии меню более ранней
          // строки его перекрывает sticky-ячейка следующей строки. Поднимаем
          // z-index именно той ячейке, где сейчас открыто меню.
          const willShow = !!menu && !menu.classList.contains('show');
          if (menu) {
            menu.classList.toggle('show', willShow);
            menu.classList.remove('kebab-menu-up', 'kebab-menu-fixed');
            menu.style.top = '';
            menu.style.left = '';
            if (willShow) positionKebabMenu(kebabBtn, menu);
          }
          if (td) td.classList.toggle('kebab-td-active', willShow);
          return;
        }
        const deleteBtn = e.target.closest('.kebab-delete-ip');
        if (deleteBtn) {
          deleteBtn.closest('.kebab-menu').classList.remove('show');
          deleteBtn.closest('td')?.classList.remove('kebab-td-active');
          deleteIpRow(deleteBtn.dataset.id, deleteBtn.dataset.address);
          return;
        }
        const actionBtn = e.target.closest('.kebab-action');
        if (actionBtn) {
          actionBtn.closest('.kebab-menu').classList.remove('show');
          actionBtn.closest('td')?.classList.remove('kebab-td-active');
          runIpNetworkAction(actionBtn.dataset.action, actionBtn.dataset.id, actionBtn.dataset.address);
          return;
        }
        if (e.target.closest('.ip-link')) {
          window.Alpine.store('modal').dirty = false;
          window.dispatchEvent(new CustomEvent('open-modal'));
        }
        const freeRangeAddBtn = e.target.closest('.free-range-add-btn');
        if (freeRangeAddBtn) {
          const afterInt = ipv4ToInt(freeRangeAddBtn.dataset.after);
          const prefixFilter = document.getElementById('prefix-filter');
          const maskStr = prefixFilter && prefixFilter.value ? prefixFilter.value.split('/')[1] : '';
          const firstFreeAddress = ipIntToStr(afterInt + 1) + (maskStr ? '/' + maskStr : '');
          openAddIpModal(firstFreeAddress);
        }
      });

      tbody.addEventListener('change', (e) => {
        const rowCb = e.target.closest('.row-select-checkbox');
        if (!rowCb) return;
        if (rowCb.checked) selectedRowIds.add(rowCb.dataset.id);
        else selectedRowIds.delete(rowCb.dataset.id);
        updateSelectAllCheckboxState();
        updateBulkDeleteUI();
      });

      document.addEventListener('click', (e) => {
        if (!e.target.closest('.kebab-wrap')) {
          document.querySelectorAll('.kebab-menu.show').forEach((m) => m.classList.remove('show'));
          document.querySelectorAll('td.kebab-td-active').forEach((t) => t.classList.remove('kebab-td-active'));
        }
      });
    })();

    // Select-all чекбокс живёт в <thead>, который scrollX перестраивает на
    // каждой инициализации/ресайзе — делегируем на document (стабилен
    // всегда), а не навешиваем напрямую на сам чекбокс.
    document.addEventListener('change', (e) => {
      if (e.target.id !== 'select-all-checkbox') return;
      const checked = e.target.checked;
      document.querySelectorAll('#ip-table-body .row-select-checkbox').forEach((cb) => {
        cb.checked = checked;
        if (checked) selectedRowIds.add(cb.dataset.id);
        else selectedRowIds.delete(cb.dataset.id);
      });
      updateBulkDeleteUI();
    });

    // --- Запоминание видимости колонок (кнопка "⚙ Столбцы") ---
    // Храним только видимость колонок (не поиск/страницу/сортировку) в
    // localStorage браузера — бессрочно, пока пользователь не очистит данные
    // сайта. Если позже добавится авторизация (IPA) — этот же localStorage
    // можно использовать как кэш, а источником истины сделать сервер,
    // подгружая/сохраняя ту же структуру через API per-пользователь.
    // v2 — после добавления колонки чекбоксов все индексы сдвинулись на +1;
    // старые значения под прежним ключом ссылались бы на другие колонки.
    // v3 — колонка "Принадлежность" (assigned_object) переехала с индекса 6
    // на 4, сдвинув Описание/Тэги; старые сохранённые индексы из v2 указывали
    // бы на другие колонки.
    // v4 — добавлена колонка "Роль" (индекс 5, read-only), сдвинув всё, что
    // было после "Принадлежность", на +1.
    // v5 — добавлена колонка "Guest OS" (индекс 10, read-only, после Open
    // Ports), сдвинув OS Guess/ОС/Назначение/Комментарии на +1.
    // v6 — добавлена колонка "DNS записи" (индекс 4, скрытая, после DNS-имя),
    // сдвинув Принадлежность…Комментарии на +1.
    const COLUMN_VISIBILITY_STORAGE_KEY = 'ip_table_columns_visibility_v6';
    // DNS записи(4), Тэги(8), CPE(9), Guest OS(11), OS Guess(12) — скрыты по умолчанию.
    // Принадлежность(5) и Роль(6) видимы по умолчанию.
    const DEFAULT_HIDDEN_COLUMNS = [4, 8, 9, 11, 12];

    // --- Точные размеры колонок (px) ---
    // Зафиксированные колонки (resizable:false) — никогда не двигаются.
    const FIXED_COLUMN_WIDTHS = {
      0: 48,   // чекбокс
      1: 220,  // Адрес (IP)
      2: 120,  // Статус
      4: 150,  // DNS записи
      5: 200,  // Принадлежность
      6: 100,  // Роль
      8: 86,   // Тэги
      9: 121,  // CPE
      10: 156, // Open Ports
      11: 140, // Guest OS
      12: 506, // OS Guess
    };
    // "Резиновые" (resizable:true) — делят между собой всё пространство
    // сверх суммы зафиксированных колонок и своих персональных минимумов
    // (см. STRETCHY_MIN_WIDTHS), не опускаясь ниже минимума каждой из них.
    const STRETCHY_COLUMNS = [3, 7, 13, 14, 15]; // DNS-имя, Описание, ОС, Назначение, Комментарии
    const STRETCHY_MIN_WIDTHS = {
      3: 280,  // DNS-имя
      7: 280,  // Описание
      13: 280, // Операционная система (OS Installed)
      14: 300, // Назначение
      15: 300, // Комментарии
    };

    // table-layout:fixed после любого изменения видимости/инициализации САМ
    // пропорционально растягивает ВСЕ колонки (включая отмеченные
    // !important), если сумма заданных ширин не точно равна текущей
    // ширине самой таблицы (это поведение спецификации CSS Tables, а не
    // каскад) — проверено эмпирически. Единственный надёжный способ не
    // дать зафиксированным колонкам "ехать" — каждый раз точно
    // пересчитывать и принудительно проставлять ширину КАЖДОЙ колонки
    // (не только резиновых), чтобы сумма всегда совпадала с реальной
    // шириной контейнера и браузеру не оставалось "лишнего" для
    // самостоятельного распределения.
    function applyColumnWidths() {
      if (!ipTable) return;
      const wrapper = document.querySelector('.table-wrapper');
      if (!wrapper) return;
      const containerWidth = wrapper.getBoundingClientRect().width;

      let fixedSum = 0;
      Object.entries(FIXED_COLUMN_WIDTHS).forEach(([idx, px]) => {
        if (ipTable.column(Number(idx)).visible()) fixedSum += px;
      });
      const visibleStretchy = STRETCHY_COLUMNS.filter((idx) => ipTable.column(idx).visible());
      const minTotal = visibleStretchy.reduce((sum, idx) => sum + STRETCHY_MIN_WIDTHS[idx], 0);
      const surplus = Math.max(containerWidth - fixedSum - minTotal, 0);
      const surplusEach = visibleStretchy.length ? surplus / visibleStretchy.length : 0;

      function setColWidth(idx, px) {
        document.querySelectorAll('colgroup col[data-dt-column="' + idx + '"]').forEach((col) => {
          col.style.setProperty('width', px + 'px', 'important');
        });
      }
      Object.entries(FIXED_COLUMN_WIDTHS).forEach(([idx, px]) => setColWidth(idx, px));
      visibleStretchy.forEach((idx) => setColWidth(idx, STRETCHY_MIN_WIDTHS[idx] + surplusEach));
    }

    // FixedColumns сам проставляет закреплённым слева ячейкам (.dtfc-fixed-
    // left) инлайн "left" — сумму ширин предыдущих закреплённых колонок —
    // но считает её ОДНОКРАТНО при своём внутреннем пересчёте (напр. внутри
    // columns.adjust()) и не обновляет, если ширины колонок потом меняет
    // КТО-ТО ДРУГОЙ (наш applyColumnWidths(), через прямую правку <col>).
    // Если на момент того пересчёта table-layout:fixed транзитно растянул
    // колонки шире, чем им положено (видели эмпирически при resize на
    // большой viewport), "left" застревает на этом большем числе — между
    // закреплёнными колонками появляется зазор. Пересчитываем left сами,
    // от тех же финальных colgroup-ширин, что только что выставил
    // applyColumnWidths() выше — он ВСЕГДА должен вызываться сразу после.
    function applyFixedColumnOffsets() {
      const groups = {};
      document.querySelectorAll('.dtfc-fixed-left[data-dt-column]').forEach((el) => {
        const idx = el.getAttribute('data-dt-column');
        (groups[idx] = groups[idx] || []).push(el);
      });
      const indices = Object.keys(groups).map(Number).sort((a, b) => a - b);
      let cumulative = 0;
      indices.forEach((idx) => {
        groups[idx].forEach((el) => el.style.setProperty('left', cumulative + 'px', 'important'));
        const col = document.querySelector('colgroup col[data-dt-column="' + idx + '"]');
        cumulative += col ? parseFloat(col.style.width) || 0 : 0;
      });
    }

    // --- Массовое удаление: выбор строк чекбоксами ---
    // DataTables держит в DOM только строки ТЕКУЩЕЙ страницы — поэтому
    // querySelectorAll по #ip-table-body естественным образом охватывает
    // именно "текущую страницу", без доп. фильтрации по пагинации.
    const selectedRowIds = new Set();

    function updateSelectAllCheckboxState() {
      const selectAllCb = document.getElementById('select-all-checkbox');
      if (!selectAllCb) return;
      const rowCheckboxes = Array.from(document.querySelectorAll('#ip-table-body .row-select-checkbox'));
      const checkedCount = rowCheckboxes.filter((cb) => cb.checked).length;
      if (checkedCount === 0) {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = false;
      } else if (checkedCount === rowCheckboxes.length) {
        selectAllCb.checked = true;
        selectAllCb.indeterminate = false;
      } else {
        selectAllCb.checked = false;
        selectAllCb.indeterminate = true;
      }
    }

    function updateBulkDeleteUI() {
      const group = document.getElementById('bulk-delete-group');
      const btn = document.getElementById('bulk-delete-btn');
      const countEl = document.getElementById('bulk-delete-count');
      if (!group || !btn) return;
      const n = selectedRowIds.size;
      group.classList.toggle('show', n > 0);
      btn.textContent = `Удалить (${n})`;
      if (countEl) countEl.textContent = n > 0 ? `${n} выбрано` : '';
    }

    // При смене страницы пагинации чекбоксы текущей страницы исчезают из
    // DOM сами (DataTables рендерит только текущую страницу) — но массив
    // выбранных ID без явного сброса остался бы указывать на невидимые
    // строки. Вызывается на 'page.dt' (см. initIpTable()).
    function clearSelection() {
      selectedRowIds.clear();
      document.querySelectorAll('#ip-table-body .row-select-checkbox:checked').forEach((cb) => { cb.checked = false; });
      updateSelectAllCheckboxState();
      updateBulkDeleteUI();
    }

    function saveColumnVisibility(table) {
      const visibility = [];
      table.columns().every(function (index) {
        visibility[index] = this.visible();
      });
      localStorage.setItem(COLUMN_VISIBILITY_STORAGE_KEY, JSON.stringify(visibility));
    }

    function applySavedColumnVisibility(table) {
      let saved;
      try {
        saved = JSON.parse(localStorage.getItem(COLUMN_VISIBILITY_STORAGE_KEY) || 'null');
      } catch (e) {
        saved = null;
      }
      if (!Array.isArray(saved)) return;
      const columnCount = table.columns().count();
      saved.forEach((visible, index) => {
        // Защита от устаревших записей в localStorage, сохранённых до
        // изменения количества колонок (напр. удаления колонки «Действия») —
        // без этой проверки table.column(index) на несуществующем индексе
        // бросает исключение и обрывает остальную инициализацию таблицы
        // (включая навешивание поиска/фильтров) ниже по initIpTable().
        if (index >= columnCount) return;
        if (typeof visible === 'boolean') table.column(index).visible(visible, false);
      });
      table.columns.adjust().draw(false);
    }

    function syncColumnsDropdown(table) {
      document.querySelectorAll('#columns-dropdown input[type="checkbox"][data-column-index]').forEach((checkbox) => {
        checkbox.checked = table.column(Number(checkbox.dataset.columnIndex)).visible();
      });
    }

    // Контекстное меню "Столбцы": открывается рядом с кнопкой (как меню "⋯"),
    // переключение чекбокса сразу меняет видимость колонки без перезагрузки —
    // сохранение в localStorage происходит автоматически через событие
    // 'column-visibility.dt', на которое подписываемся ниже в initIpTable().
    (function setupColumnsDropdown() {
      const wrap = document.querySelector('.columns-dropdown-wrap');
      const toggleBtn = document.getElementById('columns-toggle-btn');
      const dropdown = document.getElementById('columns-dropdown');
      const resetBtn = document.getElementById('columns-reset-btn');
      if (!wrap || !toggleBtn || !dropdown) return;

      toggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const willShow = !dropdown.classList.contains('show');
        if (willShow && ipTable) syncColumnsDropdown(ipTable);
        dropdown.classList.toggle('show', willShow);
      });

      dropdown.addEventListener('change', (e) => {
        const checkbox = e.target.closest('input[type="checkbox"][data-column-index]');
        if (!checkbox || !ipTable) return;
        ipTable.column(Number(checkbox.dataset.columnIndex)).visible(checkbox.checked);
      });

      if (resetBtn) {
        resetBtn.addEventListener('click', () => {
          if (!ipTable) return;
          ipTable.columns().every(function (index) {
            if (index === 0 || index === 1) return; // чекбокс и IP всегда видимы
            this.visible(!DEFAULT_HIDDEN_COLUMNS.includes(index));
          });
          syncColumnsDropdown(ipTable);
        });
      }

      document.addEventListener('click', (e) => {
        if (!e.target.closest('.columns-dropdown-wrap')) dropdown.classList.remove('show');
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') dropdown.classList.remove('show');
      });
    })();

    // --- Количество строк на странице: хранится в sessionStorage (не
    // localStorage), чтобы выбор сохранялся при перезагрузке страницы, но
    // сбрасывался при закрытии вкладки. ---
    const PAGE_LENGTH_STORAGE_KEY = 'ip_table_page_length';
    const PAGE_LENGTH_OPTIONS = [50, 100, 500, 1000];
    const DEFAULT_PAGE_LENGTH = 100;

    function getStoredPageLength() {
      const parsed = parseInt(sessionStorage.getItem(PAGE_LENGTH_STORAGE_KEY), 10);
      return PAGE_LENGTH_OPTIONS.includes(parsed) ? parsed : DEFAULT_PAGE_LENGTH;
    }

    // Собирает строку "Показано X из Y / пагинация / выбор кол-ва строк" в
    // один липкий (position: sticky) футер внизу таблицы. DataTables
    // пересоздаёт .dt-info/.dt-paging при каждом initIpTable() — поэтому
    // футер собирается из актуальных узлов каждый раз заново.
    function setupStickyFooter(table) {
      const dtWrapper = document.getElementById('ip-table_wrapper');
      if (!dtWrapper) return;

      const stale = dtWrapper.querySelector('.table-footer-bar');
      if (stale) stale.remove();

      const info = dtWrapper.querySelector('.dt-info');
      const paging = dtWrapper.querySelector('.dt-paging');
      if (!info || !paging) return;

      const bar = document.createElement('div');
      bar.className = 'table-footer-bar';

      const left = document.createElement('div');
      left.className = 'table-footer-left';
      left.appendChild(info);
      left.appendChild(paging);

      const lengthControl = document.createElement('label');
      lengthControl.className = 'page-length-control';
      lengthControl.innerHTML = '<span>Строк на странице:</span>';
      const select = document.createElement('select');
      select.id = 'page-length-select';
      PAGE_LENGTH_OPTIONS.forEach((value) => {
        const opt = document.createElement('option');
        opt.value = String(value);
        opt.textContent = String(value);
        select.appendChild(opt);
      });
      select.value = String(table.page.len());
      select.addEventListener('change', function () {
        const value = parseInt(this.value, 10);
        sessionStorage.setItem(PAGE_LENGTH_STORAGE_KEY, String(value));
        table.page.len(value).draw();
      });
      lengthControl.appendChild(select);

      // Кнопка массового удаления — слева от выбора строк на странице,
      // скрыта по умолчанию (.bulk-delete-group без класса .show), создаётся
      // заново при каждой инициализации (как и select выше).
      const bulkDeleteGroup = document.createElement('div');
      bulkDeleteGroup.className = 'bulk-delete-group';
      bulkDeleteGroup.id = 'bulk-delete-group';
      const countEl = document.createElement('span');
      countEl.className = 'bulk-delete-count';
      countEl.id = 'bulk-delete-count';
      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'btn-danger';
      deleteBtn.id = 'bulk-delete-btn';
      deleteBtn.textContent = 'Удалить (0)';
      deleteBtn.addEventListener('click', openBulkDeleteConfirm);
      bulkDeleteGroup.appendChild(countEl);
      bulkDeleteGroup.appendChild(deleteBtn);

      const right = document.createElement('div');
      right.className = 'table-footer-right';
      right.appendChild(bulkDeleteGroup);
      right.appendChild(lengthControl);

      bar.appendChild(left);
      bar.appendChild(right);
      dtWrapper.appendChild(bar);
      updateBulkDeleteUI();
    }

    // Инициализация DataTables: фиксация первых 2 колонок, ColVis, своя пагинация/поиск.
    let ipTable;
    function initIpTable() {
      // Страница IP-плана теперь React/TanStack (ip_plan_docker_react/
      // static_src/ip_table/TableRoot.jsx) — #ip-table в её разметке тот
      // же id, что и у старой DataTables-таблицы (переиспользован ради
      // общих CSS-правил), но это уже не DataTables и вообще не тот DOM,
      // который эта функция ожидает. Без этого guard'а DataTable()
      // пыталась бы взять React-узел под свой контроль (и на resize падала
      // с "Cannot read properties of undefined (reading 'page')", т.к.
      // инициализация не проходит до конца на чужой разметке).
      //
      // initIpTable() навешена глобально (DOMContentLoaded + htmx:oobAfterSwap
      // на document.body — см. ниже), а не только на странице IP-плана,
      // поэтому единственная защита — проверить, что мы вообще на ней:
      // #ip-table-root в DOM есть только там. Страница «Блокировки»
      // (regulator_table/TableRoot.jsx) переиспользует тот же id="ip-table"
      // на своей таблице по той же причине (общие CSS-правила) — без второй
      // проверки здесь эта функция хватала бы React-узел ЭТОЙ таблицы
      // вместо IP-плана (обнаружено вживую: DataTables конфигурирует 16
      // колонок IP-плана поверх 6-колоночной таблицы записей регулятора —
      // "Requested unknown parameter '6'" и разваленный DOM после
      // htmx:oobAfterSwap на удалении письма). Каждый новый React-island,
      // переиспользующий id="ip-table" ради стилей, должен добавить сюда
      // свой корневой div.
      if (document.getElementById('ip-table-root') || document.getElementById('regulator-entries-root')) return;
      if ($.fn.DataTable.isDataTable('#ip-table')) {
        // .destroy() сам сбрасывает видимость ВСЕХ колонок как часть
        // очистки и стреляет 'column-visibility.dt' на старом инстансе —
        // если не отписаться, ниже навешенный обработчик поймает этот
        // последний "вздох" умирающей таблицы и перезапишет localStorage
        // состоянием "все колонки видимы" (закрытая переменная ipTable в
        // этот момент ещё указывает на старый инстанс, т.к. переприсвоение
        // происходит только строкой ниже). Воспроизведено и подтверждено
        // эмпирически — именно это "сбрасывало" видимость при ресайзе.
        ipTable.off('column-visibility.dt');
        $('#ip-table').DataTable().destroy();
      }
      ipTable = new DataTable('#ip-table', {
        pageLength: getStoredPageLength(),
        deferRender: true,
        order: [[1, 'asc']],
        fixedColumns: { start: 3 },
        scrollX: true,
        // Без этого DataTables на каждой перерисовке (в т.ч. при фильтрах)
        // пересчитывает "удобную" ширину колонок по контенту ВИДИМЫХ строк —
        // после фильтра видимые строки другие, и колонки "плывут". Фиксируем
        // ширины такими, какими их задали в <th style="width:...">, раз и навсегда.
        autoWidth: false,
        layout: { topStart: null, topEnd: null, bottomStart: 'info', bottomEnd: 'paging' },
        dom: 'rtip',
        language: {
          info: 'Показано _START_–_END_ из _TOTAL_ записей',
          infoEmpty: 'Нет записей',
          infoFiltered: '(отфильтровано из _MAX_)',
          paginate: { previous: '←', next: '→' },
          zeroRecords: 'Совпадений не найдено',
        },
        columnDefs: [
          { targets: DEFAULT_HIDDEN_COLUMNS, visible: false },
          // Размеры колонок — через width в конфиге DataTables (не через
          // голый CSS): {0:чекбокс,1:IP,2:Статус,4:DNSзаписи,5:Принадлежность,
          // 6:Роль,8:Тэги,9:CPE,10:OpenPorts,11:GuestOS,12:OSGuess} жёстко
          // зафиксированы, {3:DNS-имя,7:Описание,13:OSInstalled,14:Назначение,
          // 15:Комментарии} — "резиновые" (растут под доступное пространство
          // сверх своего персонального минимума, см. applyColumnWidths()).
          ...Object.entries(FIXED_COLUMN_WIDTHS).map(([idx, px]) => ({ targets: Number(idx), width: px + 'px' })),
          ...STRETCHY_COLUMNS.map((idx) => ({ targets: idx, width: STRETCHY_MIN_WIDTHS[idx] + 'px' })),
          {
            // Колонка чекбоксов — не сортируется, без текстового
            // содержимого для глобального поиска/сортировки.
            targets: 0,
            orderable: false,
            render: function (data, type) {
              return type === 'display' ? data : '';
            },
          },
          {
            // Сортировка по IP — числовая по всем 4 октетам (через ту же
            // ipv4ToInt, что и фильтр по подсети), а не лексикографическая
            // по строке (иначе 10.1.2.x оказывается после 10.1.10.x).
            // На 'filter' отдаём исходную HTML-строку как раньше — иначе
            // глобальный поиск по подстроке адреса перестанет находить.
            targets: [1],
            type: 'num',
            render: function (data, type) {
              if (type !== 'sort' && type !== 'type') return data;
              const match = /(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/.exec(data);
              const value = match ? ipv4ToInt(match[1]) : null;
              return value === null ? 0 : value;
            },
          },
          {
            // Для sort/filter колонки "Статус" берём не текст бейджа (label),
            // а исходное значение из атрибута data-status — стабильнее и точно
            // совпадает со значениями в дропдауне фильтра.
            targets: 2,
            render: function (data, type) {
              if (type === 'display') return data;
              const match = /data-status="([^"]*)"/.exec(data);
              return match ? match[1] : data;
            },
          },
          {
            // Та же логика для "Роль" — фильтр сравнивает slug из data-role,
            // а не текст бейджа (там lowercase только визуально через CSS,
            // a сам text content — оригинальное Name, "Hypervisor"/"VM"/...).
            // Для строк без роли data-role="" (пусто, не атрибут отсутствует).
            targets: 6,
            render: function (data, type) {
              if (type === 'display') return data;
              const match = /data-role="([^"]*)"/.exec(data);
              return match ? match[1] : data;
            },
          },
        ],
      });

      applySavedColumnVisibility(ipTable);
      syncColumnsDropdown(ipTable);
      ipTable.on('column-visibility.dt', () => {
        saveColumnVisibility(ipTable);
        applyColumnWidths();
        applyFixedColumnOffsets();
      });
      // Чекбоксы имеют смысл только для строк текущей страницы — при
      // переходе на другую страницу выбор сбрасывается целиком.
      ipTable.on('page.dt', clearSelection);
      setupStickyFooter(ipTable);
      applyColumnWidths();
      applyFixedColumnOffsets();

      const updateCounter = () => {
        const info = ipTable.page.info();
        const shownEl = document.getElementById('dt-count-shown');
        const totalEl = document.getElementById('dt-count-total');
        if (shownEl) shownEl.textContent = info.recordsDisplay.toLocaleString('ru-RU');
        if (totalEl) totalEl.textContent = info.recordsTotal.toLocaleString('ru-RU');
      };

      // Показатели в шапке считаются по уже загруженным данным — без
      // отдельных запросов к API. rows({search:'applied'}) берёт ВСЕ строки,
      // прошедшие текущие фильтры/поиск (а не только текущую страницу),
      // поэтому цифры отражают именно текущую выборку.
      const updateHeroStats = () => {
        const totalEl = document.getElementById('stat-total');
        const activeEl = document.getElementById('stat-active');
        const inactiveEl = document.getElementById('stat-inactive');
        if (!totalEl || !activeEl || !inactiveEl) return;
        const nodes = ipTable.rows({ search: 'applied' }).nodes();
        let activeCount = 0;
        for (let i = 0; i < nodes.length; i++) {
          const statusCell = nodes[i].querySelector('td[data-field="status"]');
          if (statusCell && statusCell.dataset.value === 'active') activeCount++;
        }
        totalEl.textContent = nodes.length.toLocaleString('ru-RU');
        activeEl.textContent = activeCount.toLocaleString('ru-RU');
        inactiveEl.textContent = (nodes.length - activeCount).toLocaleString('ru-RU');
      };

      updateCounter();
      updateHeroStats();
      ipTable.on('draw', updateCounter);
      ipTable.on('draw', updateHeroStats);
      ipTable.on('draw', renderFreeRanges);

      const searchInput = document.getElementById('search-input');
      const searchClearBtn = document.getElementById('search-clear-btn');

      function updateSearchClearBtn() {
        if (searchClearBtn) searchClearBtn.style.display = searchInput.value ? '' : 'none';
      }

      if (searchInput) {
        searchInput.addEventListener('input', function () {
          ipTable.search(this.value).draw();
          updateSearchClearBtn();
        });
        updateSearchClearBtn();
      }

      if (searchClearBtn) {
        searchClearBtn.addEventListener('click', function () {
          searchInput.value = '';
          ipTable.search('').draw();
          updateSearchClearBtn();
          searchInput.focus();
        });
      }
      const statusFilter = document.getElementById('status-filter');
      if (statusFilter) {
        statusFilter.addEventListener('change', function () {
          ipTable.column(2).search(this.value, false, false, true).draw();
        });
      }
      const prefixFilter = document.getElementById('prefix-filter');
      if (prefixFilter) {
        prefixFilter.addEventListener('change', function () {
          ipTable.draw();
        });
      }
      const roleFilter = document.getElementById('role-filter');
      if (roleFilter) {
        roleFilter.addEventListener('change', function () {
          const value = this.value;
          if (!value) {
            // "Роль: все" — пустой поиск (regex:false) совпадает с любой
            // строкой, в т.ч. с пустым data-role у адресов без роли.
            ipTable.column(6).search('', false, false, true).draw();
            return;
          }
          // Точное совпадение через якоря ^...$ (regex:true) — иначе
          // "Switch" подстрокой совпал бы и с "l3-switch". "__none__" даёт
          // тот же якорный поиск на пустую строку — только адреса без роли.
          // Слаги NetBox ограничены [a-z0-9_-], спецсимволов regex в них
          // не бывает — экранировать нечего.
          const pattern = value === '__none__' ? '^$' : '^' + value + '$';
          ipTable.column(6).search(pattern, true, false, true).draw();
        });
      }

      // DataTables (scrollX/FixedColumns) переставляет узлы таблицы в свою
      // обёртку ПОСЛЕ того, как htmx уже разобрал документ при загрузке —
      // без повторного process() клик по IP-адресу (hx-get на .ip-link)
      // не открывает модалку, потому что htmx ни разу не видел эти узлы
      // в их финальном месте в DOM.
      htmx.process(document.getElementById('ip-table_wrapper') || document.body);

      // До этого момента сервер уже отрисовал ВСЕ колонки видимыми (он не
      // знает про сохранённую в localStorage видимость) — пользователь
      // успевал увидеть это "сырое" состояние, а затем скрытие колонок и
      // пересчёт ширин/сортировки сразу после. Открываем готовый результат
      // только при первой инициализации — на повторных (resize/сохранение)
      // таблица уже видна, скрывать её снова смысла нет.
      revealTableAfterFirstLoad();
      injectColInfoIcons();
    }
    document.addEventListener('DOMContentLoaded', initIpTable);

    // После HTMX OOB-обновления/добавления строки — переинициализировать таблицу.
    document.body.addEventListener('htmx:oobAfterSwap', initIpTable);

    // scrollX у DataTables умеет РАСШИРЯТЬ таблицу под более широкий
    // контейнер через columns.adjust(), но НЕ умеет сжимать её обратно —
    // once-grown ширина "застревает", даже если потом окно сузить и снова
    // вызвать columns.adjust()/draw() (проверено эмпирически). Надёжно
    // сбрасывает это состояние только полная переинициализация таблицы —
    // поэтому на resize пересоздаём её, предварительно сохранив и затем
    // восстановив поиск/сортировку/страницу, чтобы пользователь не терял
    // текущий вид таблицы из-за самого факта ресайза окна.
    function reinitTablePreservingState() {
      if (!ipTable) { initIpTable(); return; }
      const savedSearch = ipTable.search();
      const savedOrder = ipTable.order();
      const savedPage = ipTable.page();
      initIpTable();
      if (savedSearch) ipTable.search(savedSearch);
      const statusFilter = document.getElementById('status-filter');
      if (statusFilter && statusFilter.value) {
        ipTable.column(2).search(statusFilter.value, false, false, true);
      }
      ipTable.order(savedOrder).draw(false);
      const maxPage = Math.max(ipTable.page.info().pages - 1, 0);
      ipTable.page(Math.min(savedPage, maxPage)).draw(false);
    }

    // Полная переинициализация (~700мс на 1000 строк) нужна ТОЛЬКО для
    // сужения — расширение columns.adjust() обрабатывает корректно и
    // намного дешевле (см. find выше про "не умеет сжиматься"). Сравниваем
    // текущую ширину таблицы с доступной шириной враппера: растёт окно —
    // дёшево; контейнер становится УЖЕ текущей ширины таблицы — дорого,
    // но только тогда.
    function handleTableResize() {
      if (!ipTable) return;
      const wrapper = document.querySelector('.table-wrapper');
      const table = document.getElementById('ip-table');
      if (!wrapper || !table) return;
      const wrapperWidth = wrapper.getBoundingClientRect().width;
      const tableWidth = table.getBoundingClientRect().width;
      if (wrapperWidth >= tableWidth - 1) {
        ipTable.columns.adjust();
        applyColumnWidths();
        applyFixedColumnOffsets();
      } else {
        reinitTablePreservingState();
      }
    }

    // Регистрируется один раз (не внутри initIpTable), иначе при каждой
    // переинициализации таблицы накопились бы дублирующиеся обработчики.
    let resizeDebounceTimer;
    window.addEventListener('resize', () => {
      clearTimeout(resizeDebounceTimer);
      resizeDebounceTimer = setTimeout(handleTableResize, 150);
    });

    // --- Иконки-подсказки ⓘ в заголовках колонок ---
    // DataTables при scrollX клонирует оригинальный <thead> в отдельную
    // таблицу (.dt-scroll-headInner) — инжектировать иконки нужно ПОСЛЕ
    // этого клонирования, иначе они попадают только в скрытый оригинал.
    // injectColInfoIcons() вызывается изнутри initIpTable() (после new
    // DataTable(...)) и повторно при каждой переинициализации таблицы.
    // Tooltip — один общий плавающий div (position:fixed), не зависит
    // от overflow/z-index заголовков.
    let _colInfoTooltip = null;

    // ── DNS-записи: tooltip на ховере по ячейке DNS-имя ──────────────────────
    (function initDnsRecordsTooltip() {
      const tip = document.createElement('div');
      tip.className = 'dns-records-tooltip';
      document.body.appendChild(tip);

      function parseDnsRecords(raw) {
        const groups = { A: [], CNAME: [], PTR: [] };
        if (!raw) return groups;
        raw.split(';').forEach((part) => {
          const colon = part.indexOf(':');
          if (colon < 0) return;
          const type = part.slice(0, colon).toUpperCase();
          const value = part.slice(colon + 1);
          if (groups[type]) groups[type].push(value);
        });
        return groups;
      }

      function buildTooltipHTML(groups) {
        const sections = [];
        [['A', 'A-запись'], ['CNAME', 'CNAME'], ['PTR', 'PTR']].forEach(([key, label]) => {
          if (!groups[key].length) return;
          const items = groups[key].map((v) => `<li>${escapeHtml(v)}</li>`).join('');
          sections.push(`<div class="dns-tip-group"><span class="dns-tip-label">${label}</span><ul>${items}</ul></div>`);
        });
        return sections.join('');
      }

      let currentTd = null;

      document.addEventListener('mouseover', (e) => {
        const td = e.target.closest('td[data-dns-records]');
        if (!td || td === currentTd) return;
        currentTd = td;
        const raw = td.dataset.dnsRecords;
        if (!raw) { tip.classList.remove('visible'); return; }
        const groups = parseDnsRecords(raw);
        const html = buildTooltipHTML(groups);
        if (!html) { tip.classList.remove('visible'); return; }
        tip.innerHTML = html;
        const r = td.getBoundingClientRect();
        tip.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 330)) + 'px';
        tip.style.top = (r.bottom + 4) + 'px';
        tip.classList.add('visible');
      });

      document.addEventListener('mouseout', (e) => {
        if (e.target.closest('td[data-dns-records]')) {
          currentTd = null;
          tip.classList.remove('visible');
        }
      });
    })();

    function injectColInfoIcons() {
      if (!window.COLUMN_INFO || !Object.keys(window.COLUMN_INFO).length) return;

      // Создаём тултип один раз за всё время жизни страницы.
      if (!_colInfoTooltip) {
        _colInfoTooltip = document.createElement('div');
        _colInfoTooltip.className = 'col-info-tooltip';
        document.body.appendChild(_colInfoTooltip);

        document.addEventListener('mouseover', (e) => {
          const icon = e.target.closest('.col-info-icon');
          if (!icon) return;
          const th = icon.closest('th');
          const key = th && th.dataset.colKey;
          const text = key && window.COLUMN_INFO[key];
          if (!text) return;
          _colInfoTooltip.textContent = text;
          const r = icon.getBoundingClientRect();
          _colInfoTooltip.style.top = (r.bottom + 6) + 'px';
          _colInfoTooltip.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 316)) + 'px';
          _colInfoTooltip.classList.add('visible');
        });

        document.addEventListener('mouseout', (e) => {
          if (e.target.closest('.col-info-icon')) _colInfoTooltip.classList.remove('visible');
        });
      }

      // Глобальная очистка: удалить все иконки отовсюду (на случай, если
      // предыдущий вызов или старый кеш успел воткнуть иконки в скрытый
      // <thead> внутри #ip-table / .dt-scroll-body).
      document.querySelectorAll('th[data-col-key] .col-info-icon').forEach((el) => el.remove());
      // Инжектируем только в ВИДИМУЮ шапку (.dt-scroll-headInner) — клон,
      // который DataTables создаёт для прокручиваемой области заголовков.
      // Оригинальный <thead> в #ip-table остаётся в .dt-scroll-body и
      // скрывается через CSS (см. style.css: .dt-scroll-body thead).
      document.querySelectorAll('.dt-scroll-headInner th[data-col-key]').forEach((th) => {
        const key = th.dataset.colKey;
        if (!window.COLUMN_INFO[key]) return;
        const icon = document.createElement('span');
        icon.className = 'col-info-icon';
        icon.textContent = 'ⓘ';
        th.appendChild(icon);
      });
    }
