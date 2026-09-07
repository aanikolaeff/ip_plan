import { createColumnHelper } from '@tanstack/react-table';
import { RowActions } from './components/row-actions.jsx';

const columnHelper = createColumnHelper();

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('ru-RU');
}

// Те же классы, что у основной IP-таблицы (.tag-chip/.ip-mono/.cell-muted/
// .status-chip и т.д.) — глобально или #ip-table-скоуплены в table.css/
// style.css, переиспользуются как есть (см. design doc про "на той же
// основе"). Своих новых классов для ячеек здесь нет.
export const columns = [
  columnHelper.accessor('kind_display', {
    id: 'kind',
    header: 'Тип',
    size: 90,
    minSize: 70,
    cell: ({ getValue }) => <span className="tag-chip">{getValue()}</span>,
  }),

  columnHelper.accessor('value', {
    header: 'Значение',
    size: 320,
    minSize: 180,
    cell: ({ getValue }) => <span className="cell-value ip-mono" title={getValue()}>{getValue()}</span>,
  }),

  columnHelper.accessor((row) => row.document, {
    id: 'document',
    header: 'Письмо',
    size: 220,
    minSize: 160,
    sortingFn: (rowA, rowB) => (
      (rowA.original.document?.reference_no || '').localeCompare(rowB.original.document?.reference_no || '')
    ),
    cell: ({ getValue }) => {
      const doc = getValue();
      if (!doc) return <span className="cell-muted">—</span>;
      return (
        <span>
          <a href={doc.url}>№{doc.reference_no}</a>{' '}
          <span className="cell-muted">от {formatDate(doc.document_date)}</span>
        </span>
      );
    },
  }),

  columnHelper.accessor('status', {
    header: 'Статус',
    size: 150,
    minSize: 110,
    sortingFn: (rowA, rowB) => (
      (rowA.original.status_display || '').localeCompare(rowB.original.status_display || '')
    ),
    cell: ({ row }) => {
      const entry = row.original;
      const revoked = entry.status === 'revoked';
      return (
        <span className="status-chip" data-status={entry.status} title={revoked ? entry.revoked_reason || undefined : undefined}>
          <span className="status-dot" aria-hidden="true" />
          {revoked ? `снята${entry.revoked_at ? ' ' + formatDate(entry.revoked_at) : ''}` : 'активна'}
        </span>
      );
    },
  }),

  columnHelper.accessor('created_at', {
    header: 'Добавлена',
    size: 120,
    minSize: 100,
    cell: ({ getValue }) => <span className="cell-muted">{formatDate(getValue())}</span>,
  }),

  columnHelper.display({
    id: 'actions',
    header: '',
    size: 170,
    minSize: 150,
    enableSorting: false,
    enableResizing: false,
    cell: ({ row, table }) => <RowActions entry={row.original} table={table} />,
  }),
];
