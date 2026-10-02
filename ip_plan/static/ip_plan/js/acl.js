/* Раздел «ACL» (новый дизайн по макету test_des, SPEC-acl.md) — Alpine-
   компоненты трёх экранов и панели группы. Подключается только на страницах
   раздела (acl_home / acl_where / acl_list), в base.html его нет.

   Общее правило: показ/скрытие — классами is-open / is-hidden через :class,
   не x-show (см. CLAUDE.md, «Оболочка»: x-show на элементах оболочки в этой
   вёрстке не срабатывал). Данные (строки, группы, счётчики) рендерит сервер,
   здесь — только фильтры, раскрытие, навигация и открытие правой панели. */
(function () {
  'use strict';

  // Склонение «правило / правила / правил» по числу — для счётчиков, которые
  // пересчитываются на клиенте (в шаблонах то же делает фильтр |plural).
  function ruPlural(n, forms) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return forms[0];
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
    return forms[2];
  }

  // Открыть правую панель оболочки (base.html: .drawer-overlay/#drawer-content)
  // с содержимым по url — так же, как openEditPanel в React-острове адресов
  // (static_src/ip_table/TableRoot.jsx) и кнопка «Добавить IP». Глобальная,
  // потому что зовётся и из Alpine-компонентов, и из строк каталога групп.
  function openPanel(url) {
    if (!url) return;
    if (window.htmx) {
      window.htmx.ajax('GET', url, { target: '#drawer-content', indicator: '#drawer-spinner' });
    }
    if (window.Alpine && window.Alpine.store('drawer')) window.Alpine.store('drawer').dirty = false;
    window.dispatchEvent(new CustomEvent('open-drawer'));
  }
  window.aclOpenPanel = openPanel;

  function readJsonScript(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  document.addEventListener('alpine:init', function () {

    // Блок вопроса (_acl_question.html): поповер выбора устройств. Чекбоксы
    // устройств сами лежат в GET-форме под именем device, поэтому компонент
    // не собирает hidden-поля — только открывает/закрывает поповер, фильтрует
    // список по имени и площадке и пересчитывает подпись кнопки и подвала.
    // total — число устройств со списками (question.devices_total).
    Alpine.data('aclQuestion', function (total) {
      return {
        open: false,
        filter: '',
        selected: [],
        sites: [],
        init: function () {
          this.sites = readJsonScript('acl-q-sites') || [];
          var boxes = this.$el.querySelectorAll('input[name="device"]');
          var sel = [];
          boxes.forEach(function (b) { if (b.checked) sel.push(b.value); });
          this.selected = sel;
          this._notify();
        },
        // Каталог на главной (aclCatalog) фильтруется по тому же выбору:
        // сообщаем о каждом изменении событием, а не лезем в чужой компонент.
        _notify: function () {
          window.dispatchEvent(new CustomEvent('acl-devices-changed', { detail: { selected: this.selected.slice() } }));
        },
        toggle: function (name, checked) {
          var i = this.selected.indexOf(name);
          if (checked && i < 0) this.selected.push(name);
          if (!checked && i >= 0) this.selected.splice(i, 1);
          this._notify();
        },
        selectAll: function () {
          this.selected = [];
          this.$el.querySelectorAll('input[name="device"]').forEach(function (b) { b.checked = false; });
          this._notify();
        },
        _q: function () { return this.filter.trim().toLowerCase(); },
        deviceVisible: function (name, site) {
          var q = this._q();
          if (!q) return true;
          return name.toLowerCase().indexOf(q) >= 0 || (site || '').toLowerCase().indexOf(q) >= 0;
        },
        siteVisible: function (label) {
          var q = this._q();
          if (!q) return true;
          if ((label || '').toLowerCase().indexOf(q) >= 0) return true;
          var site = this.sites.find(function (s) { return s.label === label; });
          if (!site) return false;
          return site.devices.some(function (d) { return d.name.toLowerCase().indexOf(q) >= 0; });
        },
        buttonLabel: function () {
          var n = this.selected.length;
          if (n === 0) return 'Все устройства';
          if (n === 1) return this.selected[0];
          return 'Выбрано устройств: ' + n;
        },
        buttonCount: function () { return this.selected.length === 0 && total ? String(total) : ''; },
        footerText: function () {
          var n = this.selected.length;
          if (n === 0) return 'Поиск по всем ' + total + ' ' + ruPlural(total, ['устройству', 'устройствам', 'устройствам']);
          return 'Выбрано: ' + n;
        },
      };
    });

    // Каталог на главной (acl_home.html): строки .acl-cat-row несут
    // data-device; выбор устройств в блоке вопроса (событие
    // acl-devices-changed) прячет чужие строки, пересчитывает зебру и
    // счётчик активной вкладки («N из M»). Пустой выбор — показать всё.
    Alpine.data('aclCatalog', function (total) {
      return {
        selected: [],
        shown: total,
        init: function () {
          var self = this;
          // Блок вопроса мог инициализироваться раньше нас — снимем текущий выбор сами.
          var sel = [];
          document.querySelectorAll('input[name="device"]').forEach(function (b) { if (b.checked) sel.push(b.value); });
          this.selected = sel;
          window.addEventListener('acl-devices-changed', function (e) {
            self.selected = (e.detail && e.detail.selected) || [];
            self.apply();
          });
          this.$nextTick(function () { self.apply(); });
        },
        apply: function () {
          var sel = this.selected;
          var i = 0;
          this.$root.querySelectorAll('.acl-cat-row').forEach(function (el) {
            var hidden = sel.length > 0 && sel.indexOf(el.getAttribute('data-device')) < 0;
            el.classList.toggle('is-hidden', hidden);
            if (!hidden) { el.classList.toggle('is-even', i % 2 === 1); i += 1; }
          });
          this.shown = i;
          // Вкладки каталога — ссылки: дописываем выбор устройств в href,
          // чтобы переключение «Списки / Группы» его не теряло.
          this.$root.querySelectorAll('.acl-cat-tab').forEach(function (a) {
            var base = a.getAttribute('data-href') || a.getAttribute('href');
            if (!a.getAttribute('data-href')) a.setAttribute('data-href', base);
            var params = sel.map(function (d) { return 'device=' + encodeURIComponent(d); }).join('&');
            a.setAttribute('href', params ? base + '&' + params : base);
          });
        },
        countText: function () {
          if (this.selected.length === 0 || this.shown === total) return String(total);
          return this.shown + ' из ' + total;
        },
        filteredEmpty: function () { return this.selected.length > 0 && this.shown === 0 && total > 0; },
      };
    });

    // Результат «Где встречается адрес» (acl_where.html). Строки правил
    // (.acl-hit) несут data-role / data-cat / data-group; видимость считает
    // visible(), группы без видимых строк прячутся, счётчик в заголовке
    // группы — «13 правил» без фильтра и «3 из 13» с фильтром. Счётчики чипов
    // категорий приходят с сервера (result.cat_counts[cat][role]) — фильтр
    // роли их переключает, а не пересчитывает. Раскрыто всегда одно правило;
    // смена роли или категории его сворачивает. Зебра — по сквозному счётчику
    // видимых строк через все группы (класс is-even), после каждого фильтра.
    Alpine.data('aclWhere', function () {
      return {
        role: 'any',
        cat: 'all',
        open: '',
        broadOpen: false,
        broadAll: false,
        anyOpen: false,
        anyAll: false,
        counts: {},
        init: function () {
          this.counts = readJsonScript('acl-cat-counts') || {};
          var self = this;
          this.$nextTick(function () { self.zebra(); });
        },
        visible: function (role, cat) {
          var roleOk = this.role === 'any' || role === this.role || role === 'both';
          var catOk = this.cat === 'all' || cat === this.cat;
          return roleOk && catOk;
        },
        _rows: function (groupKey) {
          var sel = groupKey ? '.acl-hit[data-group="' + CSS.escape(groupKey) + '"]' : '.acl-hit';
          return Array.prototype.slice.call(this.$root.querySelectorAll(sel));
        },
        groupVisible: function (groupKey) {
          var self = this;
          return this._rows(groupKey).filter(function (el) {
            return self.visible(el.dataset.role, el.dataset.cat);
          }).length;
        },
        groupCountText: function (groupKey, total) {
          var n = this.groupVisible(groupKey);
          if (n === total) return n + ' ' + ruPlural(n, ['правило', 'правила', 'правил']);
          return n + ' из ' + total;
        },
        visibleTotal: function () { return this.groupVisible(null); },
        catCount: function (cat) {
          var byRole = this.counts[cat] || {};
          var v = byRole[this.role];
          return v === undefined ? (byRole.any !== undefined ? byRole.any : 0) : v;
        },
        setRole: function (r) { this.role = r; this.open = ''; this._refresh(); },
        setCat: function (c) { this.cat = c; this.open = ''; this._refresh(); },
        toggleOpen: function (key) { this.open = this.open === key ? '' : key; },
        _refresh: function () { var self = this; this.$nextTick(function () { self.zebra(); }); },
        zebra: function () {
          var self = this, i = 0;
          this._rows(null).forEach(function (el) {
            var shown = self.visible(el.dataset.role, el.dataset.cat);
            el.classList.toggle('is-even', shown && (i % 2 === 1));
            if (shown) i++;
          });
        },
        openPanel: openPanel,
      };
    });

    // Список правил (acl_list.html): навигация по совпадениям (cur из total,
    // ↑↓ с прокруткой к строке и подсветкой is-current), «Только совпадения»
    // (класс is-only на корне — скрытие делает CSS), зебра по счётчику
    // видимых правил (ремарки и свёртки её не сдвигают). При загрузке с
    // якорем #rule-N — текущим становится это правило; иначе первое
    // совпадение, к которому и прокручиваем.
    Alpine.data('aclList', function (total) {
      return {
        total: total || 0,
        cur: total ? 1 : 0,
        only: false,
        act: 'all',   // фильтр по действию: all | pass | drop | nat
        st: 'all',    // фильтр по состоянию: all | on | off
        passes: function (action, inactive) {
          if (this.act === 'pass' && action !== 'permit') return false;
          if (this.act === 'drop' && action !== 'deny') return false;
          if (this.act === 'nat' && action !== 'nat') return false;
          if (this.st === 'on' && inactive) return false;
          if (this.st === 'off' && !inactive) return false;
          return true;
        },
        setAct: function (v) { this.act = v; this._afterFilter(); },
        setSt: function (v) { this.st = v; this._afterFilter(); },
        _afterFilter: function () {
          var self = this;
          this.$nextTick(function () {
            self.zebra();
            var vis = self._visibleMatches();
            if (vis.length && vis.indexOf(self.cur) < 0) self.cur = vis[0];
          });
        },
        _rowPasses: function (el) {
          return this.passes(el.getAttribute('data-action'), el.getAttribute('data-inactive') === '1');
        },
        _visibleMatches: function () {
          var self = this, out = [];
          this.$root.querySelectorAll('.acl-lrow[data-match]').forEach(function (el) {
            if (self._rowPasses(el)) out.push(parseInt(el.getAttribute('data-match'), 10));
          });
          return out;
        },
        visibleTotal: function () { return this._visibleMatches().length; },
        pos: function () {
          var vis = this._visibleMatches(), i = vis.indexOf(this.cur);
          return vis.length ? (i >= 0 ? i + 1 : 0) : 0;
        },
        shownCount: function () {
          var self = this, n = 0;
          this.$root.querySelectorAll('.acl-lrow').forEach(function (el) { if (self._rowPasses(el)) n++; });
          return n;
        },
        init: function () {
          var self = this;
          this.$nextTick(function () {
            self.zebra();
            var hash = (window.location.hash || '').replace(/^#/, '');
            if (hash) {
              var el = document.getElementById(hash);
              if (el && el.dataset.match) self.cur = parseInt(el.dataset.match, 10) || self.cur;
              if (el) { self._reveal(el); return; }
            }
            if (self.total) self._scrollToCurrent();
          });
        },
        go: function (delta) {
          var vis = this._visibleMatches();
          if (!vis.length) return;
          var i = vis.indexOf(this.cur);
          if (i < 0) i = 0;
          this.cur = vis[((i + delta) % vis.length + vis.length) % vis.length];
          this._scrollToCurrent();
        },
        _scrollToCurrent: function () {
          var el = this.$root.querySelector('.acl-lrow[data-match="' + this.cur + '"]');
          if (el) this._reveal(el);
        },
        _reveal: function (el) {
          // Строка внутри свёрнутого блока — сначала развернуть его
          var gap = el.closest('.acl-gap');
          if (gap && window.Alpine) {
            var data = Alpine.$data(gap);
            if (data && data.gapOpen === false) data.gapOpen = true;
          }
          var self = this;
          this.$nextTick(function () {
            el.scrollIntoView({ block: 'center', behavior: 'smooth' });
            self.zebra();
          });
        },
        zebra: function () {
          var i = 0, only = this.only;
          this.$root.querySelectorAll('.acl-lrow').forEach(function (el) {
            var hidden = (only && !el.classList.contains('is-match')) || !el.offsetParent;
            el.classList.toggle('is-even', !hidden && (i % 2 === 1));
            if (!hidden) i++;
          });
        },
        openPanel: openPanel,
      };
    });

    // Панель группы адресов (_acl_group_panel.html): поиск по членам —
    // подстрокой, без учёта регистра; содержимое рендерит сервер.
    Alpine.data('aclGroupPanel', function () {
      return {
        q: '',
        missingOnly: false,   // фильтр «нет в iplir» (узлы, которых координатор не знает)
        match: function (text) {
          var q = this.q.trim().toLowerCase();
          return !q || text.toLowerCase().indexOf(q) >= 0;
        },
        show: function (text, missing) {
          if (this.missingOnly && !missing) return false;
          return this.match(text);
        },
        visibleCount: function () {
          var self = this, n = 0;
          this.$root.querySelectorAll('.acl-member[data-search]').forEach(function (el) {
            if (self.show(el.getAttribute('data-search') || '', el.getAttribute('data-missing') === '1')) n++;
          });
          return n;
        },
      };
    });
  });
})();
