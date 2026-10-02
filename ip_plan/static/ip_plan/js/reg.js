/* Раздел «Блокировки» (новый дизайн по макету test_des, SPEC-reg.md) —
   Alpine-компоненты главной, карточки письма, экрана «Проверьте, что
   распознано» и мастера команд. Подключается только на страницах раздела
   (reg_home / reg_document / reg_commands), в base.html его нет.

   Данные (строки, счётчики со стороны сервера) рендерит сервер; здесь —
   клиентские фильтры и пагинация таблиц, меню действий записи (JSON-
   эндпоинты revoke/reactivate/delete), удаление письма, приём файлов
   перетаскиванием, выбор строк в предпросмотре и копирование команд.
   Показ/скрытие — классами is-hidden / is-open / is-filtered через :class
   или classList, не x-show (CLAUDE.md, «Оболочка»). */
(function () {
  'use strict';

  var PAGE = 200;   // клиентская пагинация больших таблиц: по 200 строк

  function ruPlural(n, forms) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return forms[0];
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
    return forms[2];
  }
  // ru-RU с узким неразрывным пробелом, как фильтр ru_number в шаблонах
  function ruNum(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); }
  window.regPlural = ruPlural;
  window.regNum = ruNum;

  function toast(message, kind) {
    window.dispatchEvent(new CustomEvent('show-toast', { detail: { message: message, kind: kind || 'success' } }));
  }

  // CSRF: cookie с именем из base.html (window.CSRF_COOKIE_NAME); запасной
  // путь — токен из hx-headers на <body>, который base.html кладёт для htmx.
  function csrfToken() {
    var name = window.CSRF_COOKIE_NAME || 'csrftoken';
    var m = document.cookie.match(new RegExp('(^|; )' + name.replace(/[-.]/g, '\\$&') + '=([^;]*)'));
    if (m) return decodeURIComponent(m[2]);
    try { return JSON.parse(document.body.getAttribute('hx-headers') || '{}')['X-CSRFToken'] || ''; } catch (e) { return ''; }
  }

  function postJson(url, body) {
    var opts = { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrfToken() } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts);
  }

  function prefix() { return window.URL_PREFIX || ''; }

  // Фильтр + пагинация строк таблицы: rows — NodeList/массив, pass(el) —
  // проходит ли строка фильтр. Скрытые фильтром получают is-filtered, сверх
  // лимита — is-paged; зебра (is-even) — заново по видимым. Возвращает
  // {match, shown}.
  function applyRows(rows, pass, limit) {
    var match = 0, shown = 0;
    Array.prototype.forEach.call(rows, function (el) {
      var ok = pass(el);
      el.classList.toggle('is-filtered', !ok);
      if (!ok) { el.classList.remove('is-paged'); return; }
      match += 1;
      var paged = limit > 0 && match > limit;
      el.classList.toggle('is-paged', paged);
      if (!paged) { el.classList.toggle('is-even', shown % 2 === 1); shown += 1; }
    });
    return { match: match, shown: shown };
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // http:// по IP — не secure context, navigator.clipboard нет
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } finally { document.body.removeChild(ta); }
    if (!ok) throw new Error('copy failed');
  }

  document.addEventListener('alpine:init', function () {

    // ── Главная, вкладка «Все записи» (reg_home.html) ────────────────────
    // Строки .reg-entry несут data-kind / data-status / data-fw / data-search.
    // Фильтр по типу (Все / IP / Домены / URL), по состоянию (Все / Ждут
    // применения / Сняты, но стоят / Снятые), поиск по значению и «Показать
    // хэши». Хэши видны только при kind=all и включённом чекбоксе.
    Alpine.data('regEntries', function () {
      return {
        kind: 'all',
        st: 'all',
        hashes: false,
        q: '',
        limit: PAGE,
        match: 0,
        shown: 0,
        counts: { all: 0, ip: 0, fqdn: 0, url: 0, hash: 0, pending: 0, revoked_present: 0, revoked: 0 },
        init: function () {
          var c = this.counts;
          this._rows().forEach(function (el) {
            var k = el.dataset.kind;
            if (k === 'hash') c.hash += 1; else { c.all += 1; c[k] = (c[k] || 0) + 1; }
            if (el.dataset.fw === 'pending') c.pending += 1;
            if (el.dataset.fw === 'revoked_present') c.revoked_present += 1;
            if (el.dataset.status === 'revoked') c.revoked += 1;
          });
          var self = this;
          this.$nextTick(function () { self.apply(); });
        },
        _rows: function () { return Array.prototype.slice.call(this.$root.querySelectorAll('.reg-entry')); },
        pass: function (el) {
          var k = el.dataset.kind;
          if (k === 'hash') { if (!(this.hashes && this.kind === 'all')) return false; }
          else if (this.kind !== 'all' && this.kind !== k) return false;
          if (this.st === 'pending' && el.dataset.fw !== 'pending') return false;
          if (this.st === 'revoked_present' && el.dataset.fw !== 'revoked_present') return false;
          if (this.st === 'revoked' && el.dataset.status !== 'revoked') return false;
          var q = this.q.trim().toLowerCase();
          if (q && (el.dataset.search || '').indexOf(q) < 0) return false;
          return true;
        },
        apply: function () {
          var self = this;
          var r = applyRows(this._rows(), function (el) { return self.pass(el); }, this.limit);
          this.match = r.match; this.shown = r.shown;
        },
        setKind: function (k) { this.kind = k; this.limit = PAGE; this.apply(); },
        setSt: function (s) { this.st = s; this.limit = PAGE; this.apply(); },
        more: function () { this.limit += PAGE; this.apply(); },
        rest: function () { return this.match - this.shown; },
        num: ruNum,
      };
    });

    // ── Главная, «Сверка с xFW»: секция с длинным списком ────────────────
    // Первые 200 строк, «Показать все N» — снимает лимит (класс is-paged).
    Alpine.data('regSyncSection', function () {
      return {
        all: false,
        rows: function () { return this.$root.querySelectorAll('.reg-sline'); },
        init: function () { this.apply(); },
        apply: function () {
          var all = this.all;
          applyRows(this.rows(), function () { return true; }, all ? 0 : PAGE);
        },
        showAll: function () { this.all = true; this.apply(); },
      };
    });

    // ── Шапка письма: «Изменить» и меню «⋯» (удалить письмо) ─────────────
    // listUrl — куда уйти после удаления. Изменение письма — существующая
    // модалка regulator_document_edit (hx-get в #modal-content); её успешный
    // POST перерисовывает список писем OOB (на этой странице — в скрытый
    // приёмник), поэтому шапку обновляем перезагрузкой страницы.
    Alpine.data('regDocHead', function (deleteUrl, listUrl, editUrl) {
      return {
        open: false,
        confirm: false,
        busy: false,
        err: '',
        init: function () {
          document.body.addEventListener('htmx:afterRequest', function (e) {
            var cfg = e.detail && e.detail.requestConfig;
            if (!cfg || String(cfg.verb).toLowerCase() !== 'post') return;
            if ((cfg.path || '').indexOf(editUrl) !== 0) return;
            if (e.detail.xhr && e.detail.xhr.status === 200) {
              setTimeout(function () { window.location.reload(); }, 300);
            }
          });
        },
        toggle: function () { this.open = !this.open; this.confirm = false; this.err = ''; },
        close: function () { this.open = false; this.confirm = false; this.err = ''; },
        remove: async function () {
          if (this.busy) return;
          this.busy = true; this.err = '';
          try {
            var resp = await postJson(deleteUrl);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            window.location.href = listUrl;
          } catch (e) {
            this.err = 'Не удалось удалить письмо (' + e.message + ')';
            this.busy = false;
          }
        },
      };
    });

    // ── Карточка письма: фильтр таблицы, меню записи, приём файлов ───────
    // Таблица (_reg_doc_entries.html, #reg-doc-entries) перерисовывается
    // htmx-ом по событию regulator-entries-changed — после каждой замены
    // фильтр и пагинация применяются заново (htmx:afterSettle).
    Alpine.data('regDoc', function () {
      return {
        q: '',
        hashes: false,
        limit: PAGE,
        match: 0,
        shown: 0,
        // меню действий записи: одно на страницу, у нажатой «⋯»
        menu: { open: false, id: null, status: '', value: '', mode: 'menu', x: 0, y: 0, busy: false, err: '', reason: '' },
        over: false,
        init: function () {
          var self = this;
          this.$nextTick(function () { self.apply(); });
          document.body.addEventListener('htmx:afterSettle', function (e) {
            var t = e.detail && e.detail.elt;
            if (t && (t.id === 'reg-doc-entries' || (t.closest && t.closest('#reg-doc-entries')))) self.apply();
          });
          window.addEventListener('resize', function () { self.closeMenu(); });
        },
        _rows: function () { return Array.prototype.slice.call(document.querySelectorAll('#reg-doc-entries .reg-doc-row')); },
        apply: function () {
          var self = this, q = this.q.trim().toLowerCase();
          var r = applyRows(this._rows(), function (el) {
            if (el.dataset.kind === 'hash' && !self.hashes) return false;
            if (q && (el.dataset.search || '').indexOf(q) < 0) return false;
            return true;
          }, this.limit);
          this.match = r.match; this.shown = r.shown;
          var empty = document.getElementById('reg-doc-filtered-empty');
          var total = this._rows().length;
          if (empty) empty.classList.toggle('is-hidden', !(total > 0 && r.match === 0));
        },
        more: function () { this.limit += PAGE; this.apply(); },
        rest: function () { return this.match - this.shown; },
        num: ruNum,

        // Один обработчик кликов на окно: «⋯» строки открывает меню, клик
        // внутри меню — его собственный, всё остальное меню закрывает.
        onWindowClick: function (e) {
          var t = e.target;
          if (t.closest && t.closest('.reg-rowmenu')) return;
          var btn = t.closest && t.closest('[data-row-act]');
          if (btn) { this.openMenu(btn); return; }
          if (this.menu.open) this.closeMenu();
        },
        openMenu: function (btn) {
          var id = btn.getAttribute('data-id');
          if (this.menu.open && this.menu.id === id) { this.closeMenu(); return; }
          document.querySelectorAll('.reg-row-act.is-open').forEach(function (b) { b.classList.remove('is-open'); });
          btn.classList.add('is-open');
          var r = btn.getBoundingClientRect();
          var w = 260, h = 170;
          var x = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
          var y = r.bottom + 4;
          if (y + h > window.innerHeight - 8) y = Math.max(8, r.top - h - 4);
          this.menu = { open: true, id: id, status: btn.getAttribute('data-status'), value: btn.getAttribute('data-value'),
                        mode: 'menu', x: x, y: y, busy: false, err: '', reason: '' };
        },
        closeMenu: function () {
          document.querySelectorAll('.reg-row-act.is-open').forEach(function (b) { b.classList.remove('is-open'); });
          this.menu.open = false; this.menu.mode = 'menu'; this.menu.err = ''; this.menu.busy = false;
        },
        menuStyle: function () { return 'left:' + this.menu.x + 'px;top:' + this.menu.y + 'px'; },
        askRevoke: function () {
          this.menu.mode = 'revoke'; this.menu.reason = '';
          var self = this;
          this.$nextTick(function () { var i = self.$root.querySelector('.reg-rowmenu input'); if (i) i.focus(); });
        },
        askDelete: function () { this.menu.mode = 'delete'; },
        _act: async function (action, body, okMsg) {
          if (this.menu.busy) return;
          this.menu.busy = true; this.menu.err = '';
          try {
            var resp = await postJson(prefix() + '/regulator/entry/' + this.menu.id + '/' + action + '.json', body);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            this.closeMenu();
            toast(okMsg, 'success');
            window.dispatchEvent(new CustomEvent('regulator-entries-changed'));
          } catch (e) {
            this.menu.err = 'Не получилось: ' + e.message;
            this.menu.busy = false;
          }
        },
        // revoke.json читает JSON-тело с ключом revoked_reason (views.regulator_entry_revoke_api)
        revoke: function () { return this._act('revoke', { revoked_reason: (this.menu.reason || '').trim() }, 'Запись снята'); },
        reactivate: function () { return this._act('reactivate', undefined, 'Запись возвращена'); },
        remove: function () { return this._act('delete', undefined, 'Запись удалена'); },

        // Перетаскивание файлов в зону загрузки: кладём их в скрытый input
        // ioc_files и отправляем ту же htmx-форму, что и «выберите файлы».
        drop: function (e) {
          this.over = false;
          var files = e.dataTransfer && e.dataTransfer.files;
          if (!files || !files.length) return;
          var form = this.$root.querySelector('#reg-upload-form');
          var input = form && form.querySelector('input[data-files]');
          var folder = form && form.querySelector('input[data-folder]');
          if (!input) return;
          if (folder) folder.value = '';
          try {
            var dt = new DataTransfer();
            Array.prototype.forEach.call(files, function (f) { dt.items.add(f); });
            input.files = dt.files;
          } catch (err) { input.files = files; }
          form.requestSubmit();
        },
      };
    });

    // ── «Проверьте, что распознано» (_regulator_ioc_preview.html) ─────────
    // Снятая галочка отключает поля строки (disabled не уходят в POST), так
    // что regulator_document_ioc_save получает только выбранные ioc_kind[] /
    // ioc_value[] — параллельные массивы в порядке DOM.
    Alpine.data('regPreview', function (total) {
      return {
        total: total || 0,
        selected: 0,
        tick: 0,      // меняется при вводе номера письма — чтобы кнопка пересчитала hasStamp()
        init: function () {
          var self = this;
          this.$nextTick(function () { self.recount(); });
        },
        _boxes: function () { return Array.prototype.slice.call(this.$root.querySelectorAll('.reg-pv-row input[data-pick]')); },
        onChange: function (e) {
          var box = e.target;
          if (!box.matches || !box.matches('input[data-pick]')) return;
          this._setRow(box, box.checked);
          this.recount();
        },
        _setRow: function (box, on) {
          box.checked = on;
          var row = box.closest('.reg-pv-row');
          row.querySelectorAll('select, input[type="text"]').forEach(function (f) { f.disabled = !on; });
        },
        toggleAll: function (e) {
          var on = e.target.checked, self = this;
          this._boxes().forEach(function (b) { self._setRow(b, on); });
          this.recount();
        },
        recount: function () {
          var boxes = this._boxes();
          var n = boxes.filter(function (b) { return b.checked; }).length;
          this.selected = n;
          var all = this.$root.querySelector('input[data-pick-all]');
          if (all) {
            all.checked = n > 0 && n === boxes.length;
            all.indeterminate = n > 0 && n < boxes.length;
          }
        },
        hasStamp: function () {
          void this.tick;
          return Array.prototype.some.call(this.$root.querySelectorAll('input[name="stamp_value"]'), function (i) { return i.value.trim() !== ''; });
        },
        saveLabel: function () {
          var n = this.selected;
          if (n === 0) return this.hasStamp() ? 'Сохранить только номер письма' : 'Нечего сохранять';
          return 'Сохранить ' + ruNum(n) + ' ' + ruPlural(n, ['запись', 'записи', 'записей']) + ' в письмо';
        },
        cancel: function () {
          var panel = document.getElementById('ioc-preview-panel');
          if (panel) panel.innerHTML = '';
        },
        num: ruNum,
      };
    });

    // ── Мастер «Команды для xFW» (reg_commands.html) ─────────────────────
    Alpine.data('regCmd', function () {
      return {
        copied: '',
        wrapped: {},
        confirm: false,
        copy: async function (key) {
          var src = this.$root.querySelector('[data-cmd-full="' + key + '"]');
          if (!src) return;
          try {
            await copyText(src.textContent.trim());
            this.copied = key;
            var self = this;
            setTimeout(function () { if (self.copied === key) self.copied = ''; }, 1500);
          } catch (e) {
            toast('Не удалось скопировать — выделите команду вручную («Целиком»)', 'error');
          }
        },
        toggleWrap: function (key) { this.wrapped[key] = !this.wrapped[key]; },
      };
    });
  });
})();
