import { createColumnHelper } from '@tanstack/react-table';
import { CopyButton, EditableCell, RoleBadge, StatusBadge } from './components/cells.jsx';

const columnHelper = createColumnHelper();

// Дефолтные размеры — перенесены как есть из app.js
// (FIXED_COLUMN_WIDTHS/STRETCHY_MIN_WIDTHS), только теперь это лишь
// СТАРТОВОЕ значение: TanStack позволяет их реально тащить мышью, и
// storage.js персистит результат в localStorage (см. TableRoot.jsx).
export const columns = [
  columnHelper.display({
    id: 'select',
    size: 48,
    enableResizing: false,
    enableHiding: false,
    enableSorting: false,
    header: ({ table }) => (
      <input
        type="checkbox"
        checked={table.getIsAllPageRowsSelected()}
        ref={(el) => { if (el) el.indeterminate = table.getIsSomePageRowsSelected() && !table.getIsAllPageRowsSelected(); }}
        onChange={table.getToggleAllPageRowsSelectedHandler()}
        title="Выбрать все на странице"
      />
    ),
    cell: ({ row }) => (
      <input
        type="checkbox"
        checked={row.getIsSelected()}
        onChange={row.getToggleSelectedHandler()}
      />
    ),
  }),

  columnHelper.accessor('address', {
    header: 'Адрес (IP)',
    size: 220,
    minSize: 140,
    enableHiding: false,
    cell: ({ getValue, row, table }) => {
      const address = getValue();
      const addressOnly = address.split('/')[0];
      return (
        <span className="cell-with-copy">
          <span
            className="ip-link ip-mono"
            onClick={() => table.options.meta?.openEditModal(row.original.id)}
          >
            {address}
          </span>
          <CopyButton value={addressOnly} />
        </span>
      );
    },
  }),

  columnHelper.accessor((row) => row.status, {
    id: 'status',
    header: 'Статус',
    size: 120,
    minSize: 90,
    // Значение колонки — объект {value,label}, дефолтный sortingFn ('auto')
    // не умеет сравнивать объекты (клик по заголовку проставлял бы ▲/▼,
    // но порядок строк не менялся) — сортируем по человекочитаемому label.
    sortingFn: (rowA, rowB) => (rowA.original.status?.label || '').localeCompare(rowB.original.status?.label || ''),
    cell: ({ getValue, row, table }) => (
      <EditableCell
        id={row.original.id}
        field="status"
        value={getValue()?.value}
        display={<StatusBadge status={getValue()} />}
        editable
        type="select"
        table={table}
      />
    ),
  }),

  columnHelper.accessor('dns_name', {
    header: 'DNS-имя',
    size: 280,
    minSize: 200,
    cell: ({ getValue, row, table }) => (
      <span className="cell-with-copy">
        <EditableCell id={row.original.id} field="dns_name" value={getValue()} editable table={table} />
        <CopyButton value={getValue()} />
      </span>
    ),
  }),

  columnHelper.accessor('dns_records', {
    header: 'DNS записи',
    size: 150,
    minSize: 100,
    cell: ({ getValue }) => <span className="cell-value" title={getValue()}>{getValue() || '—'}</span>,
  }),

  columnHelper.accessor('assignment', {
    header: 'Принадлежность',
    size: 200,
    minSize: 140,
    cell: ({ getValue }) => (
      <span className="cell-muted" title={getValue()}>{getValue() || '—'}</span>
    ),
  }),

  columnHelper.accessor((row) => row.role, {
    id: 'role',
    header: 'Роль',
    size: 100,
    minSize: 80,
    // См. комментарий у status выше — тот же случай, объект вместо примитива.
    sortingFn: (rowA, rowB) => (rowA.original.role?.name || '').localeCompare(rowB.original.role?.name || ''),
    cell: ({ getValue }) => <RoleBadge role={getValue()} />,
  }),

  columnHelper.accessor('effective_description', {
    id: 'description',
    header: 'Описание',
    size: 280,
    minSize: 200,
    cell: ({ getValue, row, table }) => (
      <span className="cell-with-copy" title={row.original.description_hint}>
        <EditableCell
          id={row.original.id}
          field="description"
          value={row.original.description}
          display={getValue()}
          editable={row.original.description_editable}
          maxLen={40}
          table={table}
        />
        <CopyButton value={getValue()} />
      </span>
    ),
  }),

  columnHelper.accessor('tags', {
    header: 'Тэги',
    size: 86,
    minSize: 70,
    enableSorting: false,
    cell: ({ getValue }) => {
      const tags = getValue() || [];
      if (!tags.length) return <span className="cell-muted">—</span>;
      return tags.map((tag) => <span key={tag} className="tag-chip">{tag}</span>);
    },
  }),

  columnHelper.accessor('cpe', {
    header: 'CPE',
    size: 121,
    minSize: 90,
    cell: ({ getValue }) => <span className="cell-value" title={getValue()}>{getValue() || '—'}</span>,
  }),

  columnHelper.accessor('open_ports', {
    header: 'Open Ports',
    size: 156,
    minSize: 100,
    cell: ({ getValue }) => <span className="cell-value" title={getValue()}>{getValue() || '—'}</span>,
  }),

  columnHelper.accessor('guest_os', {
    header: 'Guest OS',
    size: 140,
    minSize: 100,
    cell: ({ getValue }) => <span className="cell-value" title={getValue()}>{getValue() || '—'}</span>,
  }),

  columnHelper.accessor('os_guess', {
    header: 'OS Guess',
    size: 506,
    minSize: 200,
    cell: ({ getValue }) => <span className="cell-value" title={getValue()}>{getValue() || '—'}</span>,
  }),

  columnHelper.accessor('os_installed', {
    header: 'Операционная система',
    size: 280,
    minSize: 200,
    cell: ({ getValue, row, table }) => (
      <EditableCell id={row.original.id} field="os_installed" value={getValue()} editable table={table} />
    ),
  }),

  columnHelper.accessor('purpose', {
    header: 'Назначение',
    size: 300,
    minSize: 200,
    cell: ({ getValue, row, table }) => (
      <EditableCell id={row.original.id} field="purpose" value={getValue()} editable maxLen={24} table={table} />
    ),
  }),

  columnHelper.accessor('comments', {
    header: 'Комментарии',
    size: 300,
    minSize: 200,
    cell: ({ getValue, row, table }) => (
      <EditableCell id={row.original.id} field="comments" value={getValue()} editable maxLen={40} table={table} />
    ),
  }),
];

// Скрыты по умолчанию — те же индексы/поля, что DEFAULT_HIDDEN_COLUMNS в
// app.js (dns_records, tags, cpe, guest_os, os_guess).
export const DEFAULT_HIDDEN_COLUMNS = {
  dns_records: false,
  tags: false,
  cpe: false,
  guest_os: false,
  os_guess: false,
};

export const DEFAULT_PINNING = { left: ['select', 'address'] };
