/* Раздел «Настройки» (новый дизайн по макету test_des/Settings*.html,
   SPEC-settings.md) — Alpine-компоненты экранов. Подключается только на
   страницах раздела (settings_base.html), в base.html его нет.

   Данные рендерит сервер; здесь — клиентские фильтры таблиц (поиск,
   производитель) с пересчётом зебры, выбор шагов синка, меню строки «⋯»,
   переключение веток транспорта в карточке устройства и живая карточка
   модели (парсер → доступность MAC/LLDP, выпадающие списки производителя и
   роли со вводом нового значения, список изменений, кого заденет смена
   флага). Показ/скрытие — классами is-hidden / is-open через :class, не
   x-show (CLAUDE.md, «Оболочка»). */
(function () {
  'use strict';

  function jsonFrom(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent); } catch (e) { return fallback; }
  }

  // Пересчёт зебры по видимым строкам: нечётная видимая — фон --softer.
  function rezebra(rows) {
    var i = 0;
    rows.forEach(function (row) {
      if (row.classList.contains('is-filtered')) return;
      row.classList.toggle('is-even', i % 2 === 1);
      i += 1;
    });
    return i;
  }

  document.addEventListener('alpine:init', function () {

    /* Клиентский фильтр строк таблицы: поиск по data-search, производитель
       по data-vendor. count — число видимых строк. */
    Alpine.data('stFilter', function (total) {
      return {
        q: '',
        vendor: '',
        count: total || 0,
        rows: function () { return Array.prototype.slice.call(this.$root.querySelectorAll('[data-search]')); },
        apply: function () {
          var q = this.q.trim().toLowerCase();
          var vendor = this.vendor;
          var rows = this.rows();
          rows.forEach(function (row) {
            var okQ = !q || (row.getAttribute('data-search') || '').indexOf(q) !== -1;
            var okV = !vendor || row.getAttribute('data-vendor') === vendor;
            row.classList.toggle('is-filtered', !(okQ && okV));
          });
          this.count = rezebra(rows);
        },
        pickVendor: function (v) { this.vendor = v; this.apply(); },
        plural: function (n, forms) {
          var m10 = n % 10, m100 = n % 100;
          if (m10 === 1 && m100 !== 11) return forms[0];
          if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
          return forms[2];
        }
      };
    });

    /* Меню строки «⋯». Само меню — position:fixed: таблица прокручивается
       по горизонтали (overflow-x:auto) и обрезала бы выпадающий блок. */
    Alpine.data('stMenu', function () {
      return {
        open: false,
        pos: '',
        _onScroll: null,
        toggle: function (btn) {
          if (this.open) { this.close(); return; }
          var r = btn.getBoundingClientRect();
          var self = this;
          this.pos = 'top:' + (r.bottom + 4) + 'px; right:' + Math.max(8, window.innerWidth - r.right) + 'px;';
          this.open = true;
          this.$nextTick(function () {
            var menu = self.$refs.menu;
            if (!menu) return;
            var h = menu.offsetHeight;
            if (r.bottom + 4 + h > window.innerHeight - 8) {
              self.pos = 'top:' + Math.max(8, r.top - 4 - h) + 'px; right:' + Math.max(8, window.innerWidth - r.right) + 'px;';
            }
          });
          this._onScroll = function () { self.close(); };
          window.addEventListener('scroll', this._onScroll, true);
          window.addEventListener('resize', this._onScroll);
        },
        close: function () {
          this.open = false;
          if (this._onScroll) {
            window.removeEventListener('scroll', this._onScroll, true);
            window.removeEventListener('resize', this._onScroll);
            this._onScroll = null;
          }
        }
      };
    });

    /* Шаги синка: отмеченные чекбоксы → «Выбрано: N · Запустить выбранные». */
    Alpine.data('stSteps', function () {
      return { picked: [] };
    });

    /* Карточка устройства: транспорт модели (ssh|snmp) и версия SNMP.
       Поля обеих веток всегда в DOM с прежними name — сервер сам вычищает
       лишнее (views_settings.device_save), здесь их только прячем. */
    Alpine.data('stDevice', function (transports, model, snmpVersion) {
      return {
        tr: transports || {},
        model: model || '',
        v: snmpVersion || '',
        adv: false,
        isSnmp: function () { return this.tr[this.model] === 'snmp'; }
      };
    });

    /* Строка флага в карточке устройства: подпись происхождения меняется
       сразу при клике. */
    Alpine.data('stFlagRow', function (on, inherited, onModel) {
      return {
        on: !!on, inh: !!inherited, onModel: !!onModel, start: !!on,
        note: function () {
          if (this.on && this.inh && this.start) return 'model';
          if (!this.on && this.onModel) return 'off';
          return '';
        }
      };
    });

    /* Карточка модели. Исходные данные — json_script-блоки шаблона
       settings_model.html (флаги, участники, каталог парсеров). */
    Alpine.data('stModel', function (opts) {
      opts = opts || {};
      var flagRows = jsonFrom('st-model-flags', []);
      var members = jsonFrom('st-model-members', []);
      var parsers = jsonFrom('st-model-parsers', []);
      var flags = {};
      var titles = {};
      flagRows.forEach(function (f) { flags[f.key] = !!f.on; titles[f.key] = f.title; });
      return {
        parser: opts.parser || '',
        parsers: parsers,
        mac: opts.mac || jsonFrom('st-model-mac', []),
        lldp: opts.lldp || jsonFrom('st-model-lldp', []),
        flags: flags,
        flagStart: Object.assign({}, flags),
        flagTitles: titles,
        members: members,
        vendor: opts.vendor || '',
        vendorOpen: false,
        vendorCustom: !!opts.vendorCustom,
        role: opts.role || '',
        roleOpen: false,
        roleCustom: !!opts.roleCustom,
        adv: !!opts.adv,
        initial: {},
        changes: [],
        notes: [],

        init: function () {
          var self = this;
          this.$nextTick(function () {
            self.initial = self.snapshot();
            self.recompute();
          });
        },
        transport: function () {
          var p = this.parser;
          var found = this.parsers.filter(function (x) { return x.key === p; })[0];
          return found ? found.transport : 'ssh';
        },
        isSnmp: function () { return this.transport() === 'snmp'; },
        capable: function (key) {
          if (key === 'sync_macs') return this.mac.indexOf(this.parser) !== -1;
          if (key === 'sync_lldp') return this.lldp.indexOf(this.parser) !== -1;
          return true;
        },
        pickParser: function (key) { this.parser = key; this.later(); },
        pickVendor: function (name) { this.vendor = name; this.vendorOpen = false; this.later(); },
        newVendor: function () {
          var self = this;
          this.vendorCustom = true; this.vendorOpen = false; this.vendor = '';
          this.$nextTick(function () { if (self.$refs.vendorInput) self.$refs.vendorInput.focus(); self.recompute(); });
        },
        pickRole: function (name) { this.role = name; this.roleOpen = false; this.later(); },
        newRole: function () {
          var self = this;
          this.roleCustom = true; this.roleOpen = false; this.role = '';
          this.$nextTick(function () { if (self.$refs.roleInput) self.$refs.roleInput.focus(); self.recompute(); });
        },
        later: function () { var self = this; this.$nextTick(function () { self.recompute(); }); },

        /* Значения всех подписанных полей формы (data-label): текст — как
           есть, флажок — включено/выключено (недоступный = выключено, такой
           не уйдёт с формой). */
        snapshot: function () {
          var out = {};
          var form = this.$root.querySelector('form[data-diff]');
          if (!form) return out;
          Array.prototype.forEach.call(form.querySelectorAll('[data-label]'), function (el) {
            var key = el.name || el.getAttribute('data-key');
            if (!key) return;
            var value;
            if (el.type === 'checkbox') value = (el.checked && !el.disabled) ? 'включено' : 'выключено';
            else if (el.type === 'radio') { if (!el.checked) return; value = el.value; }
            else value = (el.value || '').trim();
            out[key] = { label: el.getAttribute('data-label'), value: value };
          });
          return out;
        },
        recompute: function () {
          var now = this.snapshot();
          var start = this.initial;
          var list = [];
          var n = this.members.length;
          var self = this;
          Object.keys(now).forEach(function (key) {
            var a = start[key] ? start[key].value : '';
            var b = now[key].value;
            if (a === b) return;
            if (Object.prototype.hasOwnProperty.call(self.flags, key)) {
              list.push(now[key].label + ': ' + b + (n ? ' у ' + n + ' ' + self.plural(n, ['устройства', 'устройств', 'устройств']) : ' по умолчанию'));
            } else {
              list.push(now[key].label + ': ' + (a || 'пусто') + ' → ' + (b || 'пусто'));
            }
          });
          this.changes = list;

          // Кого заденет смена флага модели: личное значение снимается
          // (массовый выключатель, device_type_save).
          var notes = [];
          Object.keys(this.flags).forEach(function (key) {
            var cur = !!(self.flags[key] && self.capable(key));
            if (cur === !!self.flagStart[key]) return;
            self.members.forEach(function (m) {
              if (m.explicit && Object.prototype.hasOwnProperty.call(m.explicit, key)) {
                notes.push('У ' + m.name + ' личное значение «' + self.flagTitles[key] + '» будет снято — устройство будет наследовать модель.');
              }
            });
          });
          this.notes = notes;
        },
        plural: function (n, forms) {
          var m10 = n % 10, m100 = n % 100;
          if (m10 === 1 && m100 !== 11) return forms[0];
          if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
          return forms[2];
        }
      };
    });
  });
})();
