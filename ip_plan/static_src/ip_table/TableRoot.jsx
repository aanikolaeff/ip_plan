import { useEffect, useMemo, useState } from 'react';
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

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250, 1000];

function ipToInt(address) {
  const parts = (address || '').split('.').map(Number);
  if (parts.length !== 4 || parts.some((p) => Number.isNaN(p))) return null;
  return parts.reduce((acc, p) => acc * 256 + p, 0);
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
  const [sorting, setSorting] = useState([]);
  const [rowSelection, setRowSelection] = useState({});
  const [pageSize, setPageSize] = useState(() => loadSession('pageSize', 100));

  useEffect(() => { saveJSON('columnSizing', columnSizing); }, [columnSizing]);
  useEffect(() => { saveJSON('columnVisibility', columnVisibility); }, [columnVisibility]);
  useEffect(() => { saveJSON('columnOrder', columnOrder); }, [columnOrder]);
  useEffect(() => { saveSession('pageSize', pageSize); }, [pageSize]);

  async function loadData() {
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
      document.getElementById('table-loading-overlay')?.remove();
    }
  }

  useEffect(() => { loadData(); }, []);

  function updateRow(id, patch) {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
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
    return <div className="alert-modal-error">Не удалось получить список IP-адресов/подсетей: {error}</div>;
  }

  // draggedColId — drag-переупорядочивание заголовков (нативный HTML5 DnD,
  // TanStack сам drag не делает — только держит columnOrder как state).
  let draggedColId = null;

  return (
    <>
      <div className="page-toolbar">
        <div className="toolbar-left">
          <div className="toolbar-input-wrap">
            <input
              className="toolbar-input" type="text" placeholder="🔍 Поиск..."
              value={search} onChange={(e) => setSearch(e.target.value)}
            />
            {search && (
              <button type="button" className="toolbar-clear-btn" onClick={() => setSearch('')} title="Очистить поиск">✕</button>
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
        <table id="ip-table" className="table w-100" style={{ width: table.getTotalSize() }}>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    style={{
                      width: header.getSize(),
                      position: header.column.getIsPinned() ? 'sticky' : 'relative',
                      left: header.column.getIsPinned() ? header.column.getStart('left') : undefined,
                      zIndex: header.column.getIsPinned() ? 3 : undefined,
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
            {table.getRowModel().rows.map((row) => (
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
            ))}
          </tbody>
        </table>
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
      <button type="button" className="btn-modal-secondary" onClick={() => setOpen((v) => !v)}>⚙ Столбцы</button>
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
