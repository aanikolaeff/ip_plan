import { useEffect, useMemo, useRef, useState } from 'react';
import {
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { buildColumns } from './columns.jsx';
import { fetchRegulatorEntries } from './api.js';
import { loadJSON, loadSession, saveJSON, saveSession } from './storage.js';

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250, 1000];

const KIND_OPTIONS = [
  ['ip', 'IP'],
  ['fqdn', 'FQDN'],
  ['url', 'URL'],
  ['hash', 'IOC'],
];

// React/TanStack island — «та же основа, что основной IP план» (см.
// ip_table/TableRoot.jsx): один fetch целиком + вся фильтрация/сортировка/
// пагинация клиентские, без похода на сервер за каждой страницей/фильтром.
// Упрощено относительно ip_table: нет инлайн-редактирования, гэп-строк,
// OS-меню, кебаб-меню, закрепления/скрытия колонок и bulk-выбора — их тут
// просто нет по смыслу задачи.
//
// Один и тот же компонент для двух мест: вкладка «Записи» общего списка
// (documentId не задан — сквозной обзор по всем письмам) и таблица записей
// на детальной странице ОДНОГО письма (documentId задан — см. main.jsx,
// data-document-id на #regulator-entries-root). Разница — только в
// параметре fetch'а (?document=) и составе колонок/действий (buildColumns).
export default function TableRoot({ documentId }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [search, setSearch] = useState('');
  const [kindFilter, setKindFilter] = useState('');

  const [columnSizing, setColumnSizing] = useState(() => loadJSON('columnSizing', {}));
  // По умолчанию — свежие сверху (совпадает с Meta.ordering модели
  // RegulatorEntry, "-created_at"), не персистится — просто дефолт до
  // первого клика по заголовку.
  const [sorting, setSorting] = useState([{ id: 'created_at', desc: true }]);
  const [pageSize, setPageSize] = useState(() => loadSession('pageSize', 100));

  const columns = useMemo(
    () => buildColumns({ showDocument: !documentId, showRevoke: !!documentId }),
    [documentId],
  );

  useEffect(() => { saveJSON('columnSizing', columnSizing); }, [columnSizing]);
  useEffect(() => { saveSession('pageSize', pageSize); }, [pageSize]);

  // См. тот же гвард в ip_table/TableRoot.jsx — без него автоповтор при
  // ошибке копил бы параллельные fetch поверх друг друга.
  const loadingRef = useRef(false);

  async function loadData() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchRegulatorEntries(documentId);
      setRows(data.entries || []);
    } catch (e) {
      setError(e.message || 'Не удалось загрузить список записей');
    } finally {
      setLoading(false);
      loadingRef.current = false;
    }
  }

  useEffect(() => { loadData(); }, [documentId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!error) return undefined;
    const timer = setInterval(loadData, 8000);
    return () => clearInterval(timer);
  }, [error]); // eslint-disable-line react-hooks/exhaustive-deps

  // "Ввести данные"/"Загрузить файлы" на детальной странице письма — обычные
  // HTMX-формы, не React (см. regulator_document_detail.html) — при успехе
  // сервер добавляет "regulator-entries-changed" в HX-Trigger своего ответа
  // (тот же механизм, что show-toast/close-modal, см. base.html), а не
  // возвращает готовый HTML строки, как раньше: таблица теперь тут, у неё
  // нет DOM-узла, куда вставлять <tr> — проще (и надёжнее рассинхрона)
  // просто перезапросить список целиком.
  useEffect(() => {
    if (!documentId) return undefined;
    window.addEventListener('regulator-entries-changed', loadData);
    return () => window.removeEventListener('regulator-entries-changed', loadData);
  }, [documentId]); // eslint-disable-line react-hooks/exhaustive-deps

  function updateEntry(id, patch) {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  function removeEntry(id) {
    setRows((prev) => prev.filter((r) => r.id !== id));
  }

  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (kindFilter && r.kind !== kindFilter) return false;
      if (!q) return true;
      return [r.value, r.document?.reference_no].some((v) => v && String(v).toLowerCase().includes(q));
    });
  }, [rows, search, kindFilter]);

  const table = useReactTable({
    data: filteredRows,
    columns,
    columnResizeMode: 'onChange',
    getRowId: (row) => String(row.id),
    state: { columnSizing, sorting },
    onColumnSizingChange: setColumnSizing,
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
    meta: { updateEntry, removeEntry },
  });

  useEffect(() => { table.setPageSize(pageSize); }, [pageSize]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) {
    return (
      <div className="alert-modal-error" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem' }}>
        <span>Не удалось получить список записей: {error}. Повтор через 8 секунд…</span>
        <button type="button" className="btn-modal-secondary" onClick={loadData}>Повторить сейчас</button>
      </div>
    );
  }

  return (
    <>
      <div className="page-toolbar">
        <div className="toolbar-left">
          <div className="toolbar-input-wrap">
            <input
              className="toolbar-input" type="text"
              placeholder={documentId ? '🔍 Поиск по значению...' : '🔍 Поиск по значению/письму...'}
              value={search} onChange={(e) => setSearch(e.target.value)}
            />
            {search && (
              <button type="button" className="toolbar-clear-btn" onClick={() => setSearch('')} title="Очистить поиск">✕</button>
            )}
          </div>
          <select className="toolbar-select" value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}>
            <option value="">Тип: все</option>
            {KIND_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
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
        <div className="ip-table-scroll">
          <table id="ip-table" className="table w-100" style={{ width: table.getTotalSize() }}>
            <thead>
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header) => (
                    <th key={header.id} style={{ width: header.getSize(), position: 'sticky', top: 0, zIndex: 4 }}>
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
                <tr key={row.id}>
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {loading && <div className="panel-note"><span>Загрузка…</span></div>}
        {!loading && !filteredRows.length && <div className="panel-note"><span>Записей не найдено</span></div>}
      </div>

      <div className="pagination-bar">
        <button type="button" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>‹</button>
        <span>Страница {table.getState().pagination.pageIndex + 1} из {table.getPageCount() || 1}</span>
        <button type="button" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>›</button>
      </div>
    </>
  );
}
