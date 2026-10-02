import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { columns } from './columns.jsx';
import { fetchIpList } from './api.js';
import { loadJSON, saveJSON, STORAGE_KEYS, removeKey } from './storage.js';
import {
  COLUMN_DEFS, DEFAULT_COLUMN_ORDER, DEFAULT_PAGE_SIZE, STATUS_COLOR_VAR, STATUS_ORDER, STATUS_TABS,
  defaultVisibility, fmtNum, pluralize, statusLabel,
} from './constants.js';
import { computeGaps, firstFreeAddresses, ipInPrefix, ipIntToStr, ipToInt, parsePrefix, stripMask } from './ip.js';
import { copyText, showToast, StatusDot, StatusPicker } from './components/cells.jsx';
import { performDelete, performNetworkAction, patchField, runPool } from './components/actions.js';
import { CONNECT_ACTIONS, CONNECT_DEFAULTS, openConnection } from './components/connect.js';
import { FilterMenu } from './components/filter-menu.jsx';
import { ViewMenu } from './components/view-menu.jsx';
import {
  IconActivity, IconArrowDown, IconArrowRight, IconArrowUp, IconChevronLeft, IconChevronRight, IconGlobe,
  IconPlus, IconScan, IconSearch, IconSliders, IconTag, IconTrash, IconX,
} from './components/icons.jsx';

const EMPTY_FILTERS = { prefix: [], status: [], role: [], tags: [] };
const NO_ROLE = '__none__';

// ---------------------------------------------------------------- helpers

function urlPrefix() { return window.URL_PREFIX || ''; }

/** Открыть правую панель карточки адреса (HTMX → #drawer-content). Если
 * оболочка ещё без drawer'а — падаем в модалку, чтобы кнопка не молчала. */
function openEditPanel(id) {
  const hasDrawer = !!document.getElementById('drawer-content');
  window.htmx?.ajax('GET', `${urlPrefix()}/ip/${id}/edit/`, {
    target: hasDrawer ? '#drawer-content' : '#modal-content',
    indicator: hasDrawer ? '#drawer-spinner' : '#modal-spinner',
  });
  if (hasDrawer) {
    window.dispatchEvent(new CustomEvent('open-drawer'));
  } else {
    if (window.Alpine?.store('modal')) window.Alpine.store('modal').dirty = false;
    window.dispatchEvent(new CustomEvent('open-modal'));
  }
}

/** Открыть модалку «Новый IP-адрес» с контекстом: address, prefix, free[]. */
function openAddModal(params = {}) {
  const qs = new URLSearchParams();
  if (params.address) qs.set('address', params.address);
  if (params.prefix) qs.set('prefix', params.prefix);
  if (params.free && params.free.length) qs.set('free', params.free.join(','));
  const q = qs.toString();
  window.htmx?.ajax('GET', `${urlPrefix()}/ip/add/${q ? '?' + q : ''}`, {
    target: '#modal-content',
    indicator: '#modal-spinner',
  });
  if (window.Alpine?.store('modal')) window.Alpine.store('modal').dirty = false;
  window.dispatchEvent(new CustomEvent('open-modal'));
}

/** prefixes с сервера — строки (старый формат) или объекты (новый). */
function normalizePrefix(p) {
  if (typeof p === 'string') return { prefix: p, vlan: null, description: '', site: null };
  if (!p || typeof p !== 'object') return null;
  return {
    prefix: p.prefix || '',
    vlan: p.vlan && (p.vlan.vid != null || p.vlan.name) ? p.vlan : null,
    description: p.description || '',
    site: p.site || null,
  };
}

function prefixNote(p) {
  const parts = [];
  if (p.vlan?.vid != null) parts.push(`VLAN ${p.vlan.vid}`);
  const desc = p.description || p.vlan?.name || '';
  if (desc) parts.push(desc);
  return parts.join(' · ');
}

function normalizeOrder(saved) {
  const known = new Set(DEFAULT_COLUMN_ORDER);
  const list = Array.isArray(saved) ? saved.filter((id) => known.has(id)) : [];
  DEFAULT_COLUMN_ORDER.forEach((id) => { if (!list.includes(id)) list.push(id); });
  return list;
}

function isEditableTarget(el) {
  if (!el || el.nodeType !== 1) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

function matchesSearch(row, q) {
  if (!q) return true;
  return [
    row.address, row.dns_name, row.dns_records, row.assignment, row.description,
    row.effective_description, row.cpe, row.open_ports, row.guest_os, row.os_guess,
    row.os_installed, row.purpose, row.comments, row.site?.name, row.role?.name, ...(row.tags || []),
  ].some((v) => v && String(v).toLowerCase().includes(q));
}

/** Номера страниц с многоточиями: 1 … 4 5 6 … 20. */
function pageItems(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const items = [1];
  const from = Math.max(2, current - 1);
  const to = Math.min(total - 1, current + 1);
  if (from > 2) items.push('…');
  for (let p = from; p <= to; p += 1) items.push(p);
  if (to < total - 1) items.push('…');
  items.push(total);
  return items;
}

// ---------------------------------------------------------------- component

export default function TableRoot() {
  // --- данные
  const [rows, setRows] = useState([]);
  const [prefixes, setPrefixes] = useState([]);
  const [statusChoices, setStatusChoices] = useState([]);
  const [roleChoices, setRoleChoices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(null);

  // --- контекст площадки из оболочки
  const site = (typeof window !== 'undefined' && window.IP_PLAN_SITE) || 'all';
  const showSite = site === 'all';

  // --- фильтры (не сохраняются)
  const [search, setSearch] = useState('');
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [freeOnly, setFreeOnly] = useState(false);

  // --- вид (localStorage, ip_table_v2_*)
  const [density, setDensity] = useState(() => loadJSON('density', 'normal'));
  const [columnVisibility, setColumnVisibility] = useState(() => ({ ...defaultVisibility(), ...loadJSON('columnVisibility', {}) }));
  const [columnOrder, setColumnOrder] = useState(() => normalizeOrder(loadJSON('columnOrder', DEFAULT_COLUMN_ORDER)));
  const [showGaps, setShowGaps] = useState(() => loadJSON('showGaps', true));
  const [pagination, setPagination] = useState(() => ({ pageIndex: 0, pageSize: loadJSON('pageSize', DEFAULT_PAGE_SIZE) }));

  useEffect(() => { saveJSON('density', density); }, [density]);
  useEffect(() => { saveJSON('columnVisibility', columnVisibility); }, [columnVisibility]);
  useEffect(() => { saveJSON('columnOrder', columnOrder); }, [columnOrder]);
  useEffect(() => { saveJSON('showGaps', showGaps); }, [showGaps]);
  useEffect(() => { saveJSON('pageSize', pagination.pageSize); }, [pagination.pageSize]);
  // Имена пользователей для SSH/RDP (меню «Вид» → «Подключение к хостам»);
  // не входят в сброс вида — это не про таблицу.
  const [connectPrefs, setConnectPrefs] = useState(() => ({ ...CONNECT_DEFAULTS, ...(loadJSON('connect', {}) || {}) }));
  useEffect(() => { saveJSON('connect', connectPrefs); }, [connectPrefs]);

  // --- состояние таблицы
  const [sorting, setSorting] = useState([{ id: 'address', desc: false }]);
  const [rowSelection, setRowSelection] = useState({});
  const [menuRowId, setMenuRowId] = useState(null);
  const hoveredRowRef = useRef(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [viewOpen, setViewOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(null);
  const [bulkStatusOpen, setBulkStatusOpen] = useState(false);
  const filterBtnRef = useRef(null);
  const viewBtnRef = useRef(null);
  const bulkStatusRef = useRef(null);

  // --- загрузка
  // Гвард от параллельных загрузок: автоповтор по таймеру поверх ещё не
  // завершившегося fetch копил бы запросы и упирался в лимит соединений.
  const loadingRef = useRef(false);
  const loadData = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchIpList();
      setRows(data.results || []);
      setPrefixes((data.prefixes || []).map(normalizePrefix).filter((p) => p && p.prefix));
      setStatusChoices(data.status_choices || []);
      setRoleChoices(data.role_choices || []);
      setLoaded(true);
      // Выделение — только по ещё существующим строкам.
      const ids = new Set((data.results || []).map((r) => String(r.id)));
      setRowSelection((prev) => {
        const next = {};
        Object.keys(prev).forEach((id) => { if (ids.has(id)) next[id] = true; });
        return Object.keys(next).length === Object.keys(prev).length ? prev : next;
      });
    } catch (e) {
      setError(e.message || 'Не удалось загрузить список IP-адресов');
    } finally {
      setLoading(false);
      loadingRef.current = false;
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Автоповтор при ошибке (например, пока идёт скан портов и контейнер
  // временно теряет связность с NetBox) — interval, а не timeout: при той
  // же ошибке значение error не меняется и эффект не перезапустился бы.
  useEffect(() => {
    if (!error) return undefined;
    const timer = setInterval(loadData, 8000);
    return () => clearInterval(timer);
  }, [error, loadData]);

  // Сервер после сохранения модалки/панели шлёт HX-Trigger ip-addresses-changed.
  useEffect(() => {
    const handler = () => { loadData(); };
    window.addEventListener('ip-addresses-changed', handler);
    return () => window.removeEventListener('ip-addresses-changed', handler);
  }, [loadData]);

  const updateRow = useCallback((id, patch) => {
    setRows((prev) => prev.map((r) => (String(r.id) === String(id) ? { ...r, ...patch } : r)));
  }, []);

  const removeRow = useCallback((id) => {
    setRows((prev) => prev.filter((r) => String(r.id) !== String(id)));
    setRowSelection((prev) => {
      if (!(String(id) in prev)) return prev;
      const next = { ...prev };
      delete next[String(id)];
      return next;
    });
  }, []);

  const labelOf = useCallback((value) => statusLabel(value, statusChoices), [statusChoices]);

  // --- производные данные
  const parsedPrefixes = useMemo(
    () => prefixes.map((p) => ({ ...p, parsed: parsePrefix(p.prefix) })).filter((p) => p.parsed),
    [prefixes],
  );

  const rowsWithInt = useMemo(() => rows.map((r) => ({ ...r, _ip: ipToInt(stripMask(r.address)) })), [rows]);
  const rowsById = useMemo(() => new Map(rowsWithInt.map((r) => [String(r.id), r])), [rowsWithInt]);

  // Область: площадка из оболочки.
  const scopeRows = useMemo(() => {
    if (site === 'all') return rowsWithInt;
    if (site === NO_ROLE) return rowsWithInt.filter((r) => !r.site);
    return rowsWithInt.filter((r) => r.site?.slug === site);
  }, [rowsWithInt, site]);

  // Подсети, относящиеся к области (для списка значений фильтра).
  const scopePrefixes = useMemo(() => {
    if (site === 'all') return parsedPrefixes;
    return parsedPrefixes.filter((p) => (
      (site !== NO_ROLE && p.site?.slug === site)
      || scopeRows.some((r) => ipInPrefix(r._ip, p.parsed))
    ));
  }, [parsedPrefixes, scopeRows, site]);

  const selectedParsed = useMemo(
    () => filters.prefix.map((pfx) => parsedPrefixes.find((p) => p.prefix === pfx)?.parsed || parsePrefix(pfx)).filter(Boolean),
    [filters.prefix, parsedPrefixes],
  );
  const singlePrefix = filters.prefix.length === 1
    ? (parsedPrefixes.find((p) => p.prefix === filters.prefix[0]) || { prefix: filters.prefix[0], parsed: parsePrefix(filters.prefix[0]) })
    : null;

  const rowPasses = useCallback((row, f, skip) => {
    if (skip !== 'prefix' && f.prefix.length && !selectedParsed.some((p) => ipInPrefix(row._ip, p))) return false;
    if (skip !== 'status' && f.status.length && !f.status.includes(row.status?.value)) return false;
    if (skip !== 'role' && f.role.length && !f.role.includes(row.role?.slug || NO_ROLE)) return false;
    if (skip !== 'tags' && f.tags.length && !(row.tags || []).some((t) => f.tags.includes(t))) return false;
    return true;
  }, [selectedParsed]);

  const q = search.trim().toLowerCase();
  const baseRows = useMemo(() => scopeRows.filter((r) => matchesSearch(r, q)), [scopeRows, q]);
  const filteredRows = useMemo(() => baseRows.filter((r) => rowPasses(r, filters)), [baseRows, filters, rowPasses]);

  // Счётчики вкладок статусов — по области и остальным фильтрам, до статуса.
  const statusCounts = useMemo(() => {
    const counts = { total: 0 };
    baseRows.forEach((r) => {
      if (!rowPasses(r, filters, 'status')) return;
      counts.total += 1;
      const v = r.status?.value || 'unknown';
      counts[v] = (counts[v] || 0) + 1;
    });
    return counts;
  }, [baseRows, filters, rowPasses]);

  // Заполненность и свободные адреса одной выбранной подсети (по области,
  // без учёта поиска и прочих фильтров — это свойство подсети).
  const occupancy = useMemo(() => {
    if (!singlePrefix?.parsed) return null;
    const parsed = singlePrefix.parsed;
    const inPrefix = scopeRows.filter((r) => ipInPrefix(r._ip, parsed));
    const ints = [...new Set(inPrefix.map((r) => r._ip))].sort((a, b) => a - b);
    const hostInts = ints.filter((v) => v >= parsed.firstHost && v <= parsed.lastHost);
    const gaps = computeGaps(hostInts, parsed);
    const free = gaps.reduce((s, g) => s + g.count, 0);
    const byStatus = {};
    inPrefix.forEach((r) => { const v = r.status?.value || 'unknown'; byStatus[v] = (byStatus[v] || 0) + 1; });
    const used = inPrefix.length;
    const total = parsed.hostCount;
    return { parsed, used, total, free, gaps, byStatus, hostInts, percent: total ? Math.round((used / total) * 100) : 0 };
  }, [singlePrefix, scopeRows]);

  // Значения для меню «Фильтр» — счётчики с учётом остальных полей.
  const filterFields = useMemo(() => {
    const exceptPrefix = baseRows.filter((r) => rowPasses(r, filters, 'prefix'));
    const prefixValues = scopePrefixes.map((p) => ({
      value: p.prefix,
      label: p.prefix,
      note: prefixNote(p),
      count: exceptPrefix.reduce((n, r) => n + (ipInPrefix(r._ip, p.parsed) ? 1 : 0), 0),
    }));
    filters.prefix.forEach((pfx) => {
      if (!prefixValues.some((v) => v.value === pfx)) prefixValues.push({ value: pfx, label: pfx, note: '', count: 0 });
    });

    const exceptStatus = baseRows.filter((r) => rowPasses(r, filters, 'status'));
    const statusMap = new Map();
    statusChoices.forEach(([v, l]) => statusMap.set(v, l));
    exceptStatus.forEach((r) => { const v = r.status?.value; if (v && !statusMap.has(v)) statusMap.set(v, statusLabel(r.status)); });
    const statusValues = [...statusMap.keys()]
      .sort((a, b) => (STATUS_ORDER.indexOf(a) + 100) % 100 - (STATUS_ORDER.indexOf(b) + 100) % 100)
      .map((v) => ({
        value: v, label: statusMap.get(v), dot: v,
        count: exceptStatus.reduce((n, r) => n + (r.status?.value === v ? 1 : 0), 0),
      }));

    const exceptRole = baseRows.filter((r) => rowPasses(r, filters, 'role'));
    const roleMap = new Map();
    roleChoices.forEach(([slug, name]) => roleMap.set(slug, name));
    exceptRole.forEach((r) => { if (r.role?.slug && !roleMap.has(r.role.slug)) roleMap.set(r.role.slug, r.role.name); });
    const roleValues = [...roleMap.entries()].map(([slug, name]) => ({
      value: slug, label: slug, note: name && name !== slug ? name : '',
      count: exceptRole.reduce((n, r) => n + (r.role?.slug === slug ? 1 : 0), 0),
    }));
    roleValues.push({ value: NO_ROLE, label: 'Без роли', note: '', count: exceptRole.reduce((n, r) => n + (r.role ? 0 : 1), 0) });

    const exceptTags = baseRows.filter((r) => rowPasses(r, filters, 'tags'));
    const tagCounts = new Map();
    exceptTags.forEach((r) => (r.tags || []).forEach((t) => tagCounts.set(t, (tagCounts.get(t) || 0) + 1)));
    filters.tags.forEach((t) => { if (!tagCounts.has(t)) tagCounts.set(t, 0); });
    const tagValues = [...tagCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'ru'))
      .map(([t, n]) => ({ value: t, label: t, note: '', count: n }));

    return [
      { key: 'prefix', label: 'Подсеть', placeholder: 'Адрес подсети или VLAN', mono: true, values: prefixValues },
      { key: 'status', label: 'Статус', placeholder: 'Найти статус', values: statusValues },
      { key: 'role', label: 'Роль', placeholder: 'Найти роль', mono: true, values: roleValues },
      { key: 'tags', label: 'Тэги', placeholder: 'Найти тэг', values: tagValues },
    ];
  }, [baseRows, filters, rowPasses, scopePrefixes, statusChoices, roleChoices]);

  // --- таблица
  const effectiveVisibility = useMemo(() => {
    const vis = { ...columnVisibility };
    if (!showSite) vis.site = false;
    return vis;
  }, [columnVisibility, showSite]);
  const effectiveOrder = useMemo(() => ['select', 'address', ...columnOrder, 'actions'], [columnOrder]);

  // Обработчик действий строки — через ref, чтобы meta не пересоздавалась
  // на каждый рендер (объявлен до meta, наполняется ниже).
  const handlersRef = useRef({});
  const meta = useMemo(() => ({
    updateRow,
    removeRow,
    openEdit: openEditPanel,
    statusChoices,
    menuRowId,
    setMenuRowId,
    onRowAction: (action, row) => handlersRef.current.onRowAction(action, row),
  }), [updateRow, removeRow, statusChoices, menuRowId]);

  const tableData = freeOnly ? [] : filteredRows;
  const table = useReactTable({
    data: tableData,
    columns,
    getRowId: (row) => String(row.id),
    state: { columnVisibility: effectiveVisibility, columnOrder: effectiveOrder, sorting, rowSelection, pagination },
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    onPaginationChange: setPagination,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    autoResetPageIndex: false,
    meta,
  });

  // Смена фильтров/поиска — на первую страницу; после перезагрузки данных
  // индекс не должен указывать за последнюю страницу.
  useEffect(() => { setPagination((p) => (p.pageIndex === 0 ? p : { ...p, pageIndex: 0 })); }, [search, filters, freeOnly]);
  const pageCount = table.getPageCount();
  useEffect(() => {
    if (pageCount > 0 && pagination.pageIndex > pageCount - 1) setPagination((p) => ({ ...p, pageIndex: pageCount - 1 }));
  }, [pageCount, pagination.pageIndex]);

  const visibleColumns = table.getVisibleLeafColumns();
  const gridTemplate = useMemo(() => visibleColumns.map((col) => {
    if (col.id === 'select' || col.id === 'actions') return '48px';
    if (col.id === 'address') return `minmax(140px, ${showSite ? 150 : 184}fr)`;
    const def = COLUMN_DEFS.find((c) => c.id === col.id);
    if (!def) return 'minmax(80px, 120fr)';
    const width = showSite ? (def.widthSite || def.width) : def.width;
    const min = showSite ? (def.minSite || def.min) : def.min;
    if (def.fixed) return `${width}px`;
    return `minmax(${min}px, ${width}fr)`;
  }).join(' '), [visibleColumns, showSite]);

  // --- действия со строкой (кебаб + горячие клавиши)
  handlersRef.current.onRowAction = (action, row) => {
    const addr = stripMask(row.address);
    switch (action) {
      case 'edit': openEditPanel(row.id); break;
      case 'copy': copyText(addr); break;
      case 'check':
      case 'scan-ports':
      case 'resolve-dns':
        performNetworkAction(action, row, { updateRow, interactive: true, labelOf });
        break;
      case 'delete': performDelete(row, { removeRow, confirm: true }); break;
      default:
        if (CONNECT_ACTIONS.includes(action)) openConnection(action, row, connectPrefs);
        break;
    }
  };

  useEffect(() => {
    function onKey(e) {
      if (isEditableTarget(e.target)) return;
      if (e.target.closest?.('[data-ipt-pop]:not(.ipt-pop-menu)')) return;
      const row = menuRowId ? rowsById.get(menuRowId) : hoveredRowRef.current;
      if (!row) return;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.code === 'KeyC') {
        const sel = window.getSelection();
        if (sel && !sel.isCollapsed) return; // пользователь копирует выделенный текст
        e.preventDefault();
        handlersRef.current.onRowAction('copy', row);
        setMenuRowId(null);
        return;
      }
      if (mod || e.altKey || e.shiftKey) return;
      const map = { KeyE: 'edit', KeyP: 'check', KeyS: 'scan-ports', KeyD: 'resolve-dns', Backspace: 'delete', Delete: 'delete' };
      const action = map[e.code];
      if (!action) return;
      e.preventDefault();
      setMenuRowId(null);
      handlersRef.current.onRowAction(action, row);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [menuRowId, rowsById]);

  // --- публичная кнопка «Добавить IP» в шапке оболочки
  const addContextRef = useRef({});
  addContextRef.current = occupancy
    ? {
      prefix: singlePrefix.prefix,
      free: firstFreeAddresses(occupancy.hostInts, occupancy.parsed, 8),
    }
    : {};
  useEffect(() => {
    window.ipTableOpenAdd = () => {
      const ctx = addContextRef.current;
      const params = { ...ctx };
      if (ctx.free?.length && ctx.prefix) params.address = `${ctx.free[0]}/${ctx.prefix.split('/')[1]}`;
      openAddModal(params);
    };
    return () => { if (window.ipTableOpenAdd) delete window.ipTableOpenAdd; };
  }, []);

  // --- массовые действия
  const selectedIds = Object.keys(rowSelection);
  const selectedRows = selectedIds.map((id) => rowsById.get(id)).filter(Boolean);

  async function runBulk(action) {
    const targets = selectedRows;
    const n = targets.length;
    if (!n || bulkBusy) return;
    if (action === 'delete') {
      const ok = window.confirm(`Удалить ${n} ${pluralize(n, 'адрес', 'адреса', 'адресов')}? Это действие нельзя отменить.`);
      if (!ok) return;
    }
    const labels = { check: 'Проверка доступности', 'scan-ports': 'Сканирование портов', 'resolve-dns': 'Резолв DNS', delete: 'Удаление' };
    setBulkBusy({ done: 0, total: n, label: labels[action] });
    const results = await runPool(
      targets, 3,
      (row) => (action === 'delete'
        ? performDelete(row, { removeRow, confirm: false })
        : performNetworkAction(action, row, { updateRow, interactive: false, labelOf })),
      (done, total) => setBulkBusy({ done, total, label: labels[action] }),
    );
    setBulkBusy(null);
    const errors = results.filter((r) => !r.ok).length;
    const errTail = errors ? `, ошибок: ${errors}` : '';
    if (action === 'check') {
      const alive = results.filter((r) => r.ok && r.alive).length;
      const dead = results.filter((r) => r.ok && !r.alive).length;
      const suggested = results.filter((r) => r.ok && r.suggested).length;
      const changed = results.filter((r) => r.ok && r.statusChanged).length;
      let msg = `Доступность: отвечают ${alive} из ${n}`;
      if (dead) msg += `, не отвечают ${dead}`;
      if (changed) msg += `, статус → «${labelOf('active')}»: ${changed}`;
      if (suggested) msg += ` · ${suggested} не отвечают при статусе «${labelOf('active')}» — проверьте вручную`;
      showToast(msg + errTail, errors || dead ? 'error' : 'success');
    } else if (action === 'scan-ports') {
      const withPorts = results.filter((r) => r.ok && r.ports).length;
      showToast(`Порты: просканировано ${n - errors} из ${n}, с открытыми портами — ${withPorts}${errTail}`, errors ? 'error' : 'success');
    } else if (action === 'resolve-dns') {
      const saved = results.filter((r) => r.ok && r.dnsAction === 'saved').length;
      const confirm = results.filter((r) => r.ok && r.dnsAction === 'confirm').length;
      const none = results.filter((r) => r.ok && r.dnsAction === 'none').length;
      let msg = `DNS: сохранено ${saved}, без изменений ${none}`;
      if (confirm) msg += `, требуют подтверждения (имя отличается от PTR): ${confirm}`;
      showToast(msg + errTail, errors ? 'error' : 'success');
    } else if (action === 'delete') {
      const deleted = results.filter((r) => r.ok).length;
      showToast(`Удалено ${deleted} из ${n}${errTail}`, errors ? 'error' : 'success');
      setRowSelection({});
    }
  }

  async function runBulkStatus(value, label) {
    const targets = selectedRows;
    const n = targets.length;
    if (!n || bulkBusy) return;
    setBulkBusy({ done: 0, total: n, label: `Статус → «${label}»` });
    const results = await runPool(
      targets, 3,
      (row) => patchField(row, 'status', value, { updateRow, patch: { status: { value, label } }, silent: true }),
      (done, total) => setBulkBusy({ done, total, label: `Статус → «${label}»` }),
    );
    setBulkBusy(null);
    const okCount = results.filter((r) => r.ok).length;
    showToast(`Статус «${label}» установлен для ${okCount} из ${n}`, okCount === n ? 'success' : 'error');
  }

  // --- вид
  function setFilterField(key, values) {
    setFilters((f) => ({ ...f, [key]: values }));
    if (key === 'status' && values.length) setFreeOnly(false);
  }
  function resetView() {
    setDensity('normal');
    setColumnVisibility(defaultVisibility());
    setColumnOrder(DEFAULT_COLUMN_ORDER);
    setShowGaps(true);
    setPagination((p) => ({ ...p, pageSize: DEFAULT_PAGE_SIZE }));
    STORAGE_KEYS.forEach(removeKey);
  }
  const viewColumns = columnOrder
    .map((id) => COLUMN_DEFS.find((c) => c.id === id))
    .filter((c) => c && (showSite || c.id !== 'site'))
    .map((c) => ({ id: c.id, label: c.label, scan: !!c.scan, visible: !!columnVisibility[c.id] }));

  // --- вкладки статусов
  const activeTab = freeOnly ? 'free' : filters.status.length === 0 ? 'all' : filters.status.length === 1 ? filters.status[0] : null;
  const tabs = [
    { key: 'all', label: 'Все', count: statusCounts.total },
    ...STATUS_TABS
      .filter((t) => t.value !== 'slaac' || (statusCounts.slaac || 0) > 0 || filters.status.includes('slaac'))
      .map((t) => ({ key: t.value, label: t.label, count: statusCounts[t.value] || 0, dot: t.value })),
    { key: 'free', label: 'Свободные', count: occupancy ? occupancy.free : '—', free: true, disabled: !occupancy },
  ];
  function pickTab(key) {
    setMenuRowId(null);
    if (key === 'free') { setFreeOnly(true); return; }
    setFreeOnly(false);
    setFilters((f) => ({ ...f, status: key === 'all' ? [] : [key] }));
  }

  // --- строки к показу (с гэпами)
  const pageRows = table.getRowModel().rows;
  const gapsAllowed = !!occupancy && showGaps && !q && !filters.status.length && !filters.role.length && !filters.tags.length;
  const maskStr = singlePrefix ? singlePrefix.prefix.split('/')[1] : '';
  const displayItems = [];
  if (freeOnly) {
    (occupancy?.gaps || []).forEach((g) => displayItems.push({ kind: 'gap', key: `gap-${g.start}`, gap: g }));
  } else if (gapsAllowed) {
    const { parsed } = occupancy;
    const isFirstPage = pagination.pageIndex === 0;
    const isLastPage = pagination.pageIndex >= pageCount - 1;
    const ints = pageRows.map((r) => r.original._ip);
    const gapsOnPage = computeGaps(ints.filter((v) => v !== null), parsed, { leading: isFirstPage, trailing: isLastPage });
    const gapByAfter = new Map(gapsOnPage.map((g) => [g.afterIpInt, g]));
    if (isFirstPage) {
      const lead = gapByAfter.get(parsed.firstHost - 1);
      if (lead) displayItems.push({ kind: 'gap', key: `gap-${lead.start}`, gap: lead });
    }
    pageRows.forEach((row) => {
      displayItems.push({ kind: 'row', row });
      const g = row.original._ip !== null ? gapByAfter.get(row.original._ip) : null;
      if (g) displayItems.push({ kind: 'gap', key: `gap-${g.start}`, gap: g });
    });
  } else {
    pageRows.forEach((row) => displayItems.push({ kind: 'row', row }));
  }

  function occupyFirst(gap) {
    const free = [];
    for (let a = gap.start; a <= gap.end && free.length < 8; a += 1) free.push(ipIntToStr(a));
    openAddModal({ address: `${free[0]}/${maskStr}`, prefix: singlePrefix.prefix, free });
  }

  // --- сводка справа от вкладок
  const summaryLine = (() => {
    const n = scopeRows.length;
    const addrWord = pluralize(n, 'адрес', 'адреса', 'адресов');
    if (site === 'all') {
      const sites = new Set(scopeRows.map((r) => r.site?.slug).filter(Boolean)).size;
      const p = parsedPrefixes.length;
      return `${fmtNum(n)} ${addrWord} · ${fmtNum(sites)} ${pluralize(sites, 'площадка', 'площадки', 'площадок')} · ${fmtNum(p)} ${pluralize(p, 'подсеть', 'подсети', 'подсетей')}`;
    }
    if (site === NO_ROLE) return `${fmtNum(n)} ${addrWord} без площадки`;
    const p = scopePrefixes.length;
    return `${fmtNum(n)} ${addrWord} на площадке · ${fmtNum(p)} ${pluralize(p, 'подсеть', 'подсети', 'подсетей')}`;
  })();

  // --- чипы активных фильтров
  const chips = [];
  if (filters.prefix.length) {
    const single = filters.prefix.length === 1 ? parsedPrefixes.find((p) => p.prefix === filters.prefix[0]) : null;
    chips.push({
      key: 'prefix', label: 'Подсеть', mono: true,
      value: filters.prefix.length === 1 ? filters.prefix[0] : `${filters.prefix.length} ${pluralize(filters.prefix.length, 'подсеть', 'подсети', 'подсетей')}`,
      note: single?.vlan?.vid != null ? `VLAN ${single.vlan.vid}` : '',
    });
  }
  // Статус показываем чипом и при одном значении, хотя он уже подсвечен
  // вкладкой: в панели должны быть видны все активные фильтры, а не только
  // те, которых нет в других местах экрана.
  if (filters.status.length) chips.push({ key: 'status', label: 'Статус', value: filters.status.map((v) => labelOf(v)).join(', ') });
  if (filters.role.length) chips.push({ key: 'role', label: 'Роль', mono: true, value: filters.role.map((v) => (v === NO_ROLE ? 'без роли' : v)).join(', ') });
  if (filters.tags.length) chips.push({ key: 'tags', label: 'Тэги', value: filters.tags.join(', ') });

  const totalFiltered = filteredRows.length;
  const pageStart = totalFiltered ? pagination.pageIndex * pagination.pageSize + 1 : 0;
  const pageEnd = Math.min(totalFiltered, (pagination.pageIndex + 1) * pagination.pageSize);

  // ------------------------------------------------------------- render
  return (
    <div className="ipt" data-density={density}>
      {error && (
        <div className="ipt-error" role="alert">
          <span>Не удалось получить список IP-адресов: {error}. Повтор через 8 секунд…</span>
          <button type="button" className="ipt-btn-ghost ipt-btn-sm" onClick={loadData}>Повторить сейчас</button>
        </div>
      )}

      {/* Полоса статусов + заполненность */}
      <div className="ipt-tabs">
        <div className="ipt-tablist" role="group" aria-label="Фильтр по статусу">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              className={`ipt-tab${activeTab === t.key ? ' is-on' : ''}`}
              aria-pressed={activeTab === t.key}
              disabled={t.disabled}
              title={t.disabled ? 'Свободные адреса считаются при одной выбранной подсети' : undefined}
              onClick={() => pickTab(t.key)}
            >
              {t.dot && <StatusDot value={t.dot} className="ipt-dot-plain" />}
              {t.free && <span className="ipt-dot ipt-dot-free" aria-hidden="true" />}
              <span>{t.label}</span>
              <span className="ipt-tab-n ipt-mono">{!loaded ? '–' : typeof t.count === 'number' ? fmtNum(t.count) : t.count}</span>
            </button>
          ))}
        </div>
        {occupancy ? (
          <div className="ipt-occ">
            <span className="ipt-muted">Занято <b className="ipt-mono">{fmtNum(occupancy.used)}</b> из <span className="ipt-mono">{fmtNum(occupancy.total)}</span></span>
            <div
              className="ipt-bar"
              role="img"
              aria-label={`Заполненность подсети ${occupancy.percent}%: ${STATUS_ORDER.filter((v) => occupancy.byStatus[v]).map((v) => `${labelOf(v).toLowerCase()} ${occupancy.byStatus[v]}`).join(', ')}, свободно ${occupancy.free}`}
            >
              {STATUS_ORDER.filter((v) => occupancy.byStatus[v]).map((v) => (
                <span key={v} title={`${labelOf(v)} — ${occupancy.byStatus[v]}`} style={{ flexGrow: occupancy.byStatus[v], background: STATUS_COLOR_VAR[v] }} />
              ))}
              {occupancy.free > 0 && <span title={`Свободно — ${occupancy.free}`} style={{ flexGrow: occupancy.free, background: 'var(--soft)' }} />}
            </div>
            <span className="ipt-mono ipt-occ-pct">{occupancy.percent}%</span>
          </div>
        ) : (
          <span className="ipt-summary">{loaded ? summaryLine : ''}</span>
        )}
      </div>

      {/* Тулбар */}
      <div className="ipt-toolbar">
        <div className="ipt-toolbar-left">
          <label className="ipt-search">
            <IconSearch size={15} />
            <input
              type="search"
              aria-label="Поиск по таблице"
              placeholder="Найти адрес, имя или описание"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <button
            ref={filterBtnRef}
            type="button"
            className={`ipt-btn-filter${filterOpen ? ' is-on' : ''}`}
            aria-haspopup="dialog"
            aria-expanded={filterOpen}
            onClick={() => setFilterOpen((v) => !v)}
          >
            <IconPlus size={14} />
            Фильтр
          </button>
          {chips.map((c) => (
            <span key={c.key} className="ipt-chip">
              <span className="ipt-muted">{c.label}</span>
              <span className={c.mono ? 'ipt-mono ipt-chip-val' : 'ipt-chip-val'}>{c.value}</span>
              {c.note && <span className="ipt-muted">{c.note}</span>}
              <button type="button" className="ipt-chip-x" aria-label={`Сбросить фильтр «${c.label}»`} onClick={() => setFilterField(c.key, [])}>
                <IconX size={12} />
              </button>
            </span>
          ))}
        </div>
        <button
          ref={viewBtnRef}
          type="button"
          className={`ipt-btn-ghost${viewOpen ? ' is-on' : ''}`}
          aria-haspopup="dialog"
          aria-expanded={viewOpen}
          onClick={() => setViewOpen((v) => !v)}
        >
          <IconSliders size={15} />
          Вид
        </button>
      </div>

      <FilterMenu
        anchorRef={filterBtnRef}
        open={filterOpen}
        onClose={() => setFilterOpen(false)}
        fields={filterFields}
        selected={filters}
        onChange={setFilterField}
      />
      <ViewMenu
        anchorRef={viewBtnRef}
        open={viewOpen}
        onClose={() => setViewOpen(false)}
        density={density}
        onDensity={setDensity}
        columns={viewColumns}
        onToggleColumn={(id) => setColumnVisibility((v) => ({ ...v, [id]: !v[id] }))}
        onReorder={(order) => setColumnOrder(normalizeOrder(showSite ? order : [...order, 'site']))}
        showGaps={showGaps}
        onShowGaps={setShowGaps}
        gapsAvailable={!!occupancy}
        connect={connectPrefs}
        onConnect={(patch) => setConnectPrefs((p) => ({ ...p, ...patch }))}
        onReset={resetView}
      />

      {/* Таблица */}
      <div className="ipt-grid">
        <div className="ipt-head" style={{ gridTemplateColumns: gridTemplate }}>
          {table.getFlatHeaders().map((header) => {
            const col = header.column;
            const def = COLUMN_DEFS.find((c) => c.id === col.id);
            const label = col.id === 'select' || col.id === 'actions'
              ? null
              : (def?.short || def?.label || (typeof col.columnDef.header === 'string' ? col.columnDef.header : col.id));
            const sorted = col.getIsSorted();
            const info = window.COLUMN_INFO?.[col.id];
            if (col.id === 'select') {
              return <div key={header.id} className="ipt-hcell ipt-c-select">{flexRender(col.columnDef.header, header.getContext())}</div>;
            }
            if (col.id === 'actions') return <div key={header.id} className="ipt-hcell ipt-c-actions" />;
            return (
              <button
                key={header.id}
                type="button"
                className={`ipt-hcell ipt-hbtn${sorted ? ' is-sorted' : ''}`}
                title={info || def?.label || label}
                aria-sort={sorted ? (sorted === 'desc' ? 'descending' : 'ascending') : 'none'}
                onClick={col.getCanSort() ? col.getToggleSortingHandler() : undefined}
              >
                <span className="ipt-ellipsis">{label}</span>
                {sorted === 'asc' && <IconArrowUp size={11} />}
                {sorted === 'desc' && <IconArrowDown size={11} />}
              </button>
            );
          })}
        </div>

        <div className="ipt-body">
          {displayItems.map((item) => {
            if (item.kind === 'gap') {
              const g = item.gap;
              return (
                <div key={item.key} className="ipt-gap" role="row">
                  <span className="ipt-muted">
                    <b className="ipt-mono">{fmtNum(g.count)}</b> {pluralize(g.count, 'свободный адрес', 'свободных адреса', 'свободных адресов')}
                  </span>
                  <span className="ipt-mono ipt-gap-range">
                    {g.count === 1 ? ipIntToStr(g.start) : `${ipIntToStr(g.start)} – ${ipIntToStr(g.end)}`}
                  </span>
                  <button type="button" className="ipt-gap-take" onClick={() => occupyFirst(g)}>
                    Занять первый
                    <IconArrowRight size={13} />
                  </button>
                </div>
              );
            }
            const { row } = item;
            const id = String(row.original.id);
            const selected = row.getIsSelected();
            const menuOpen = menuRowId === id;
            return (
              <div
                key={id}
                role="row"
                className={`ipt-row${selected ? ' is-selected' : ''}${menuOpen ? ' is-menu' : ''}`}
                style={{ gridTemplateColumns: gridTemplate }}
                data-status={row.original.status?.value}
                onMouseEnter={() => { hoveredRowRef.current = row.original; }}
                onMouseLeave={() => { if (hoveredRowRef.current === row.original) hoveredRowRef.current = null; }}
              >
                {row.getVisibleCells().map((cell) => (
                  <div key={cell.id} className={`ipt-cell ipt-c-${cell.column.id}`} role="cell">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </div>
                ))}
              </div>
            );
          })}

          {!displayItems.length && !loaded && (
            <div className="shell-loading"><span className="shell-spinner" />Загрузка адресов…</div>
          )}
          {!displayItems.length && loaded && (
            <div className="ipt-empty">
              {freeOnly
                  ? 'Свободных адресов нет'
                  : rows.length
                    ? 'Ничего не найдено'
                    : 'Адресов пока нет'}
            </div>
          )}
        </div>
      </div>

      {/* Подвал */}
      <div className="ipt-foot">
        <span className="ipt-muted">
          {freeOnly
            ? <><b className="ipt-mono">{fmtNum(occupancy?.gaps.length || 0)}</b> {pluralize(occupancy?.gaps.length || 0, 'диапазон', 'диапазона', 'диапазонов')}</>
            : <><b className="ipt-mono">{pageStart}–{pageEnd}</b> из <b className="ipt-mono">{fmtNum(totalFiltered)}</b></>}
          {loading && loaded && <span className="ipt-faint"> · обновление…</span>}
        </span>
        {!freeOnly && pageCount > 1 && (
          <div className="ipt-pager">
            <button type="button" className="ipt-pbtn" aria-label="Назад" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>
              <IconChevronLeft size={15} />
            </button>
            {pageItems(pagination.pageIndex + 1, pageCount).map((p, i) => (typeof p === 'number' ? (
              <button
                key={p}
                type="button"
                className={`ipt-pbtn ipt-pnum ipt-mono${p === pagination.pageIndex + 1 ? ' is-on' : ''}`}
                aria-current={p === pagination.pageIndex + 1 ? 'page' : undefined}
                onClick={() => table.setPageIndex(p - 1)}
              >
                {p}
              </button>
            ) : (
              <span key={`e${i}`} className="ipt-pellip ipt-mono">…</span>
            )))}
            <button type="button" className="ipt-pbtn" aria-label="Вперёд" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>
              <IconChevronRight size={15} />
            </button>
          </div>
        )}
      </div>

      {/* Плавающая панель выделения */}
      {selectedIds.length > 0 && createPortal(
        <div className="ipt-float" role="toolbar" aria-label="Действия с выбранными адресами">
          <span className="ipt-float-count">
            {bulkBusy ? <>{bulkBusy.label} <span className="ipt-mono">{bulkBusy.done}/{bulkBusy.total}</span></> : <>Выбрано <span className="ipt-mono">{selectedIds.length}</span></>}
          </span>
          <span className="ipt-float-sep" />
          <button type="button" className="ipt-fbtn" disabled={!!bulkBusy} onClick={() => runBulk('check')}><IconActivity size={15} />Доступность</button>
          <button type="button" className="ipt-fbtn" disabled={!!bulkBusy} onClick={() => runBulk('scan-ports')}><IconScan size={15} />Порты</button>
          <button type="button" className="ipt-fbtn" disabled={!!bulkBusy} onClick={() => runBulk('resolve-dns')}><IconGlobe size={15} />DNS</button>
          <button ref={bulkStatusRef} type="button" className={`ipt-fbtn${bulkStatusOpen ? ' is-on' : ''}`} disabled={!!bulkBusy} aria-haspopup="menu" aria-expanded={bulkStatusOpen} onClick={() => setBulkStatusOpen((v) => !v)}><IconTag size={15} />Статус</button>
          <button type="button" className="ipt-fbtn is-danger" disabled={!!bulkBusy} onClick={() => runBulk('delete')}><IconTrash size={15} />Удалить</button>
          <span className="ipt-float-sep ipt-float-sep-tight" />
          <button type="button" className="ipt-fbtn ipt-fbtn-x" aria-label="Снять выделение" title="Снять выделение" onClick={() => setRowSelection({})}><IconX size={15} /></button>
          <StatusPicker
            anchorRef={bulkStatusRef}
            open={bulkStatusOpen}
            onClose={() => setBulkStatusOpen(false)}
            current={null}
            choices={statusChoices}
            onPick={(value, label) => runBulkStatus(value, label)}
          />
        </div>,
        document.body,
      )}
    </div>
  );
}
