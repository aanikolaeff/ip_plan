import { useEffect, useMemo, useRef, useState } from 'react';
import {
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { columns, DEFAULT_HIDDEN_COLUMNS, DEFAULT_PINNING } from './columns.jsx';
import { deleteIp, fetchIpList } from './api.js';
import { loadJSON, loadSession, saveJSON, saveSession } from './storage.js';
import { showToast } from './components/cells.jsx';
import { ipIntToStr, ipToInt } from './ip.js';

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250, 1000];

// Перенесено дословно из app.js (pluralizeAddresses) — русское склонение
// "адрес/адреса/адресов" по числу.
function pluralizeAddresses(n) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return 'адресов';
  if (mod10 === 1) return 'адрес';
  if (mod10 >= 2 && mod10 <= 4) return 'адреса';
  return 'адресов';
}

function openAddIpModal(prefillAddress) {
  window.Alpine?.store('modal') && (window.Alpine.store('modal').dirty = false);
  window.htmx?.ajax('GET', `${window.URL_PREFIX || ''}/ip/add/?address=${encodeURIComponent(prefillAddress)}`, {
    target: '#modal-content',
    indicator: '#modal-spinner',
  });
  window.dispatchEvent(new CustomEvent('open-modal'));
}

export default function TableRoot() {
  const [rows, setRows] = useState([]);
  const [prefixes, setPrefixes] = useState([]);
  const [statusChoices, setStatusChoices] = useState([]);
  const [roleChoices, setRoleChoices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [prefixFilter, setPrefixFilter] = useState('');

  const [columnSizing, setColumnSizing] = useState(() => loadJSON('columnSizing', {}));
  const [columnVisibility, setColumnVisibility] = useState(() => loadJSON('columnVisibility', DEFAULT_HIDDEN_COLUMNS));
  const [columnOrder, setColumnOrder] = useState(() => loadJSON('columnOrder', []));
  const [columnPinning] = useState(DEFAULT_PINNING); // закреплённые колонки не переключаются пользователем (пока)
  // По умолчанию — по адресу (как в старой DataTables-версии, order:
  // [[1,'asc']]) — не персистится (в отличие от sizing/visibility/order),
  // это просто дефолт до первого клика по заголовку. Важно и для гэп-строк
  // свободных диапазонов ниже: их арифметика предполагает, что видимые на
  // странице строки идут по возрастанию адреса.
  const [sorting, setSorting] = useState([{ id: 'address', desc: false }]);
  const [rowSelection, setRowSelection] = useState({});
  const [pageSize, setPageSize] = useState(() => loadSession('pageSize', 100));

  useEffect(() => { saveJSON('columnSizing', columnSizing); }, [columnSizing]);
  useEffect(() => { saveJSON('columnVisibility', columnVisibility); }, [columnVisibility]);
  useEffect(() => { saveJSON('columnOrder', columnOrder); }, [columnOrder]);
  useEffect(() => { saveSession('pageSize', pageSize); }, [pageSize]);

  // Гвард от параллельных загрузок — без него setInterval (см. ниже) на
  // 8с, срабатывающий поверх ещё не завершившегося предыдущего fetch (сам
  // /ip/list.json может идти дольше 8с — в т.ч. именно во время скана
  // портов из кебаб-меню, см. ниже), копил бы параллельные запросы к
  // /ip/list.json один поверх другого — упирались бы в лимит браузера на
  // число одновременных соединений к одному origin (обычно 6) и вешали
  // вкладку целиком, а не только эту таблицу. Ровно это и произошло при
  // живой проверке: через ~40с параллельных попыток рендерер завис.
  const loadingRef = useRef(false);

  async function loadData() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchIpList();
      setRows(data.results);
      setPrefixes(data.prefixes || []);
      setStatusChoices(data.status_choices || []);
      setRoleChoices(data.role_choices || []);
    } catch (e) {
      setError(e.message || 'Не удалось загрузить список IP-адресов');
    } finally {
      setLoading(false);
      loadingRef.current = false;
      document.getElementById('table-loading-overlay')?.remove();
    }
  }

  useEffect(() => { loadData(); }, []);

  // Автоповтор при ошибке — на практике самая частая причина здесь: пока
  // выполняется "Сканировать порты" (кебаб-меню, nmap с raw-сокетами),
  // сам этот контейнер на локальном Docker Desktop на время скана (~15–25с,
  // ровно до его завершения) теряет связность с NetBox — не только
  // конкретный запрос скана, вообще любой запрос с этого же контейнера,
  // включая /ip/list.json; восстанавливается сама, сразу как скан
  // закончится. Без автоповтора пользователь упирался бы в баннер ошибки
  // и ничего не видел до ручной перезагрузки страницы.
  // setInterval, а не setTimeout внутри useEffect(..., [error]) — если
  // повторная попытка падает с ТЕМ ЖЕ текстом ошибки, error как значение
  // стейта не меняется (строки сравниваются по значению), и эффект просто
  // не перезапустился бы; interval тикает сам, не завися от того, меняется
  // ли error между попытками. Сами тики при этом безопасны за счёт
  // loadingRef выше — тик во время ещё идущей попытки просто no-op.
  useEffect(() => {
    if (!error) return undefined;
    const timer = setInterval(loadData, 8000);
    return () => clearInterval(timer);
  }, [error]); // eslint-disable-line react-hooks/exhaustive-deps

  function updateRow(id, patch) {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  // Одиночное удаление через кебаб-меню (в отличие от bulkDelete ниже,
  // которая берёт ids из rowSelection) — на всякий случай тоже снимает
  // строку с выбора, если её успели отметить чекбоксом до удаления.
  function removeRow(id) {
    setRows((prev) => prev.filter((r) => String(r.id) !== String(id)));
    setRowSelection((prev) => {
      if (!(String(id) in prev)) return prev;
      const next = { ...prev };
      delete next[String(id)];
      return next;
    });
  }

  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (statusFilter && r.status?.value !== statusFilter) return false;
      if (roleFilter) {
        if (roleFilter === '__none__' ? r.role : r.role?.slug !== roleFilter) return false;
      }
      if (prefixFilter) {
        const [net, maskStr] = prefixFilter.split('/');
        const mask = Number(maskStr);
        const addrInt = ipToInt(r.address.split('/')[0]);
        const netInt = ipToInt(net);
        if (addrInt === null || netInt === null) return false;
        const shift = 32 - mask;
        if ((addrInt >>> shift) !== (netInt >>> shift)) return false;
      }
      if (!q) return true;
      return [
        r.address, r.dns_name, r.dns_records, r.assignment, r.description,
        r.effective_description, r.cpe, r.open_ports, r.guest_os, r.os_guess,
        r.os_installed, r.purpose, r.comments, ...(r.tags || []),
      ].some((v) => v && String(v).toLowerCase().includes(q));
    });
  }, [rows, search, statusFilter, roleFilter, prefixFilter]);

  const table = useReactTable({
    data: filteredRows,
    columns,
    columnResizeMode: 'onChange',
    enableMultiSort: true,
    getRowId: (row) => String(row.id),
    state: { columnSizing, columnVisibility, columnOrder, columnPinning, sorting, rowSelection },
    onColumnSizingChange: setColumnSizing,
    onColumnVisibilityChange: setColumnVisibility,
    onColumnOrderChange: setColumnOrder,
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
    meta: {
      updateRow,
      removeRow,
      openEditModal(id) {
        window.Alpine?.store('modal') && (window.Alpine.store('modal').dirty = false);
        window.htmx?.ajax('GET', `${window.URL_PREFIX || ''}/ip/${id}/edit/`, {
          target: '#modal-content',
          indicator: '#modal-spinner',
        });
        window.dispatchEvent(new CustomEvent('open-modal'));
      },
    },
  });

  useEffect(() => { table.setPageSize(pageSize); }, [pageSize]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedIds = Object.keys(rowSelection);

  async function bulkDelete() {
    if (!selectedIds.length) return;
    if (!window.confirm(`Удалить ${selectedIds.length} записей? Это действие нельзя отменить.`)) return;
    const results = await Promise.allSettled(selectedIds.map((id) => deleteIp(id).then((r) => ({ id, ok: r.ok }))));
    const succeeded = new Set();
    const failed = [];
    results.forEach((res, i) => {
      const id = selectedIds[i];
      if (res.status === 'fulfilled' && res.value.ok) succeeded.add(id);
      else failed.push(id);
    });
    setRows((prev) => prev.filter((r) => !succeeded.has(String(r.id))));
    setRowSelection({});
    if (failed.length) showToast(`Не удалось удалить: ${failed.join(', ')}`, 'error');
    else showToast(`Удалено ${succeeded.size} записей`, 'success');
  }

  const stats = useMemo(() => {
    const total = rows.length;
    const active = rows.filter((r) => r.status?.value === 'active').length;
    return { total, active, inactive: total - active };
  }, [rows]);

  useEffect(() => {
    // Счётчики в шапке (#stat-total и т.д.) остаются Django-рендеренными
    // DOM-элементами вне React-root (см. design doc) — обновляем их
    // напрямую, не дублируя разметку/CSS hero-блока в React.
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
    set('stat-total', stats.total);
    set('stat-active', stats.active);
    set('stat-inactive', stats.inactive);
  }, [stats]);

  if (error) {
    return (
      <div className="alert-modal-error" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem' }}>
        <span>Не удалось получить список IP-адресов/подсетей: {error}. Повтор через 8 секунд…</span>
        <button type="button" className="btn-modal-secondary" onClick={loadData}>Повторить сейчас</button>
      </div>
    );
  }

  // --- Свободные диапазоны IP внутри выбранной подсети ("гэп-строки") ---
  // Перенесено из app.js (renderFreeRanges/insertGapRow): чисто визуальные
  // строки, не часть данных таблицы — считаются ТОЛЬКО по строкам,
  // реально показанным на текущей странице (после всех фильтров и
  // сортировки), поэтому гэп, уходящий за границу страницы, виден только
  // с той стороны, что приходится на первую/последнюю страницу выборки.
  // Требует возрастающего порядка по адресу — см. дефолт sorting выше и
  // числовой sortingFn у колонки address в columns.jsx.
  const pageRows = table.getRowModel().rows;
  const displayRows = [];
  if (prefixFilter) {
    const [netStr, maskStr] = prefixFilter.split('/');
    const mask = Number(maskStr);
    const netInt = ipToInt(netStr);
    const size = Number.isFinite(mask) && mask >= 0 && mask <= 32 ? 2 ** (32 - mask) : null;
    // /31, /32 — network/broadcast не выделяются, считать "свободное" нет смысла.
    if (netInt !== null && size !== null && size > 2) {
      const firstHost = netInt + 1;
      const lastHost = netInt + size - 2;
      const isFirstPage = table.getState().pagination.pageIndex === 0;
      const isLastPage = table.getState().pagination.pageIndex >= table.getPageCount() - 1;
      const pushGap = (afterIpInt, count) => {
        if (count > 0) displayRows.push({ kind: 'gap', key: `gap-${afterIpInt}`, afterIpInt, count });
      };
      let prevIpInt = null;
      pageRows.forEach((row, idx) => {
        const ipInt = ipToInt(row.original.address.split('/')[0]);
        if (ipInt === null) { displayRows.push({ kind: 'row', row }); return; }
        if (idx === 0 && isFirstPage) pushGap(firstHost - 1, ipInt - firstHost);
        else if (prevIpInt !== null) pushGap(prevIpInt, ipInt - prevIpInt - 1);
        displayRows.push({ kind: 'row', row });
        prevIpInt = ipInt;
      });
      if (isLastPage && prevIpInt !== null) pushGap(prevIpInt, lastHost - prevIpInt);
    }
  }
  if (!displayRows.length) pageRows.forEach((row) => displayRows.push({ kind: 'row', row }));

  // draggedColId — drag-переупорядочивание заголовков (нативный HTML5 DnD,
  // TanStack сам drag не делает — только держит columnOrder как state).
  let draggedColId = null;

  return (
    <>
      <div className="page-toolbar">
        <div className="toolbar-left">
          <div className="toolbar-input-wrap">
            <svg className="toolbar-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7"></circle>
              <path d="m21 21-4.3-4.3"></path>
            </svg>
            <input
              className="toolbar-input" type="text" placeholder="Поиск..."
              value={search} onChange={(e) => setSearch(e.target.value)}
            />
            {search && (
              <button type="button" className="toolbar-clear-btn" onClick={() => setSearch('')} title="Очистить поиск">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>
              </button>
            )}
          </div>
          <select className="toolbar-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">Статус: все</option>
            {statusChoices.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select className="toolbar-select" value={prefixFilter} onChange={(e) => setPrefixFilter(e.target.value)}>
            <option value="">Подсеть: все</option>
            {prefixes.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <select className="toolbar-select" value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
            <option value="">Роль: все</option>
            <option value="__none__">Нет роли</option>
            {roleChoices.map(([slug, name]) => <option key={slug} value={slug}>{name}</option>)}
          </select>

          <div className="columns-dropdown-wrap">
            <ColumnsDropdown table={table} />
          </div>

          {/* Раньше жила иконкой в постоянной шапке (.app-nav) рядом со
              "Справкой" — но экспортирует всегда именно эту таблицу
              целиком (все IP, независимо от текущих фильтров/видимых
              столбцов), поэтому логичнее в тулбаре самой таблицы, а не в
              шапке, общей для ACL/Блокировок, где кнопка не имела смысла. */}
          <a href={`${window.URL_PREFIX || ''}/export/`} className="btn-modal-secondary btn-icon-label" title="Выгрузить все IP-адреса в .xlsx">
            <svg className="btn-icon-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 3v12"></path>
              <path d="M7 10l5 5 5-5"></path>
              <path d="M4 19h16"></path>
            </svg>
            Экспорт в Excel
          </a>

          {selectedIds.length > 0 && (
            <div className="bulk-delete-group show">
              <span>{selectedIds.length} выбрано</span>
              <button type="button" className="btn-danger" onClick={bulkDelete}>Удалить ({selectedIds.length})</button>
            </div>
          )}
        </div>
        <div className="toolbar-right">
          <div className="records-counter">
            Показано <span>{table.getRowModel().rows.length}</span> из <span>{filteredRows.length}</span> записей
          </div>
          <select className="toolbar-select" value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
            {PAGE_SIZE_OPTIONS.map((n) => <option key={n} value={n}>{n} на странице</option>)}
          </select>
        </div>
      </div>

      <div className="table-wrapper">
        {/* Свой скролл-контейнер вокруг самой <table> — .table-wrapper
            специально держит overflow:visible (общий класс, его же
            использует DataTables на ACL-страницах, см. комментарий в
            style.css про sticky-шапку). Без этого контейнера широкая
            таблица растягивала бы весь документ, и горизонтальный скролл
            двигал бы тулбар/hero вместе с ней — ровно баг, который чиним. */}
        <div className="ip-table-scroll">
        <table id="ip-table" className="table w-100" style={{ width: table.getTotalSize() }}>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    style={{
                      width: header.getSize(),
                      // sticky всегда (не только у закреплённых колонок) —
                      // держит шапку по вертикали при скролле .ip-table-scroll
                      // (см. max-height/overflow-y там же, table.css). У
                      // закреплённых колонок (select/address) добавляется ещё
                      // и left — угловая ячейка залипает по обеим осям сразу,
                      // это нормально для position:sticky.
                      position: 'sticky',
                      top: 0,
                      left: header.column.getIsPinned() ? header.column.getStart('left') : undefined,
                      zIndex: header.column.getIsPinned() ? 5 : 4,
                    }}
                    className={header.column.getIsPinned() ? 'col-pinned' : undefined}
                    draggable={header.column.id !== 'select' && header.column.getIsPinned() !== 'left'}
                    onDragStart={() => { draggedColId = header.column.id; }}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => {
                      if (!draggedColId || draggedColId === header.column.id) return;
                      const order = table.getAllLeafColumns().map((c) => c.id);
                      const from = order.indexOf(draggedColId);
                      const to = order.indexOf(header.column.id);
                      order.splice(to, 0, order.splice(from, 1)[0]);
                      setColumnOrder(order);
                    }}
                  >
                    <span
                      className="th-label"
                      onClick={header.column.getCanSort() ? header.column.getToggleSortingHandler() : undefined}
                      style={{ cursor: header.column.getCanSort() ? 'pointer' : 'default' }}
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {header.column.getCanSort() && (
                        <span className={`sort-indicator${header.column.getIsSorted() ? ' active' : ''}`} data-dir={header.column.getIsSorted() || undefined}>
                          {header.column.getIsSorted() === 'desc' ? '▼' : '▲'}
                        </span>
                      )}
                    </span>
                    {header.column.getCanResize() && (
                      <div
                        onMouseDown={header.getResizeHandler()}
                        onTouchStart={header.getResizeHandler()}
                        onDoubleClick={() => header.column.resetSize()}
                        className={`column-resizer${header.column.getIsResizing() ? ' resizing' : ''}`}
                        title="Тащить — изменить ширину, двойной клик — сбросить"
                      />
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {displayRows.map((item) => {
              if (item.kind === 'gap') {
                const maskStr = prefixFilter.split('/')[1];
                return (
                  <tr key={item.key} className="free-range-row">
                    <td colSpan={table.getVisibleLeafColumns().length}>
                      <div className="free-range-content">
                        <span className="free-range-label">
                          ● Свободно: {item.count} {pluralizeAddresses(item.count)}
                        </span>
                        <button
                          type="button"
                          className="free-range-add-btn"
                          title="Добавить IP в этом диапазоне"
                          onClick={() => openAddIpModal(`${ipIntToStr(item.afterIpInt + 1)}${maskStr ? '/' + maskStr : ''}`)}
                        >
                          +
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              }
              const row = item.row;
              return (
                <tr
                  key={row.id}
                  className={row.getIsSelected() ? 'row-selected' : undefined}
                  data-status={row.original.status?.value}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      className={cell.column.getIsPinned() ? 'col-pinned' : undefined}
                      style={cell.column.getIsPinned() ? {
                        position: 'sticky', left: cell.column.getStart('left'), zIndex: 1,
                      } : undefined}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {loading && <div className="panel-note"><span>Загрузка…</span></div>}
        {!loading && !filteredRows.length && <div className="panel-note"><span>Ничего не найдено</span></div>}
      </div>

      <div className="pagination-bar">
        <button type="button" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>‹</button>
        <span>Страница {table.getState().pagination.pageIndex + 1} из {table.getPageCount() || 1}</span>
        <button type="button" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>›</button>
      </div>
    </>
  );
}

function ColumnsDropdown({ table }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="btn-modal-secondary btn-icon-label" onClick={() => setOpen((v) => !v)}>
        {/* Не шестерёнка "Настроек" (base.html) — это переключение видимых
            колонок таблицы, не приложения; своя, отдельная по смыслу
            иконка. */}
        <svg className="btn-icon-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="2"></rect>
          <line x1="9" y1="4" x2="9" y2="20"></line>
          <line x1="15" y1="4" x2="15" y2="20"></line>
        </svg>
        Столбцы
      </button>
      {open && (
        <div className="columns-dropdown show">
          <div className="columns-dropdown-title">Видимые столбцы</div>
          <div className="columns-checklist">
            {table.getAllLeafColumns().filter((c) => c.getCanHide()).map((column) => (
              <label key={column.id} className="column-check">
                <input
                  type="checkbox"
                  checked={column.getIsVisible()}
                  onChange={column.getToggleVisibilityHandler()}
                />
                {typeof column.columnDef.header === 'string' ? column.columnDef.header : column.id}
              </label>
            ))}
          </div>
          <div className="columns-dropdown-footer">
            <button type="button" className="columns-reset-btn" onClick={() => table.resetColumnVisibility()}>
              Сбросить по умолчанию
            </button>
          </div>
        </div>
      )}
    </>
  );
}
