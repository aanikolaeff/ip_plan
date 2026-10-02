import { createColumnHelper } from '@tanstack/react-table';
import { COLUMN_DEFS, STATUS_ORDER } from './constants.js';
import { CopyButton, EditableText, Empty, StatusCell } from './components/cells.jsx';
import { OsMenuCell } from './components/os-menu.jsx';
import { RowMenu } from './components/kebab-menu.jsx';
import { ipToInt, maskOf, stripMask } from './ip.js';

const h = createColumnHelper();
const defMeta = (id) => COLUMN_DEFS.find((c) => c.id === id) || {};
const text = (v) => (v ? v : '');
const byText = (fn) => (a, b) => text(fn(a.original)).localeCompare(text(fn(b.original)), 'ru');

function MonoCell({ value, muted }) {
  if (!value) return <Empty />;
  return <span className={`ipt-txt ipt-mono${muted ? ' ipt-muted' : ''}`} title={value}>{value}</span>;
}

// table.options.meta: { updateRow, openEdit, statusChoices, menuRowId,
// setMenuRowId, onRowAction }.
export const columns = [
  h.display({
    id: 'select',
    enableHiding: false,
    enableSorting: false,
    header: ({ table }) => (
      <input
        type="checkbox"
        aria-label="Выбрать все на странице"
        checked={table.getIsAllPageRowsSelected()}
        ref={(el) => { if (el) el.indeterminate = table.getIsSomePageRowsSelected() && !table.getIsAllPageRowsSelected(); }}
        onChange={table.getToggleAllPageRowsSelectedHandler()}
      />
    ),
    cell: ({ row }) => (
      <input
        type="checkbox"
        aria-label={`Выбрать ${stripMask(row.original.address)}`}
        checked={row.getIsSelected()}
        onChange={row.getToggleSelectedHandler()}
        onClick={(e) => e.stopPropagation()}
      />
    ),
  }),

  h.accessor('address', {
    id: 'address',
    header: 'Адрес',
    enableHiding: false,
    meta: { label: 'Адрес' },
    // Строковая сортировка дала бы "10.0.1.10" < "10.0.1.2" — нужен
    // числовой порядок, на нём же держится арифметика свободных диапазонов.
    sortingFn: (a, b) => (ipToInt(stripMask(a.original.address)) ?? 0) - (ipToInt(stripMask(b.original.address)) ?? 0),
    cell: ({ row, table }) => {
      const full = row.original.address;
      const addr = stripMask(full);
      const mask = maskOf(full);
      const role = row.original.role;
      return (
        <span className="ipt-addr">
          <button
            type="button"
            className="ipt-addr-link ipt-mono"
            title={`${full} — открыть карточку`}
            onClick={(e) => { e.stopPropagation(); table.options.meta?.openEdit(row.original.id); }}
          >
            {addr}
          </button>
          {mask && <span className="ipt-addr-mask ipt-mono" aria-hidden="true">/{mask}</span>}
          {role && <span className="ipt-role ipt-mono" title={`Роль: ${role.name}`}>{role.slug || role.name}</span>}
          <CopyButton value={addr} />
        </span>
      );
    },
  }),

  h.accessor((r) => r.status?.value || '', {
    id: 'status',
    header: 'Статус',
    meta: defMeta('status'),
    sortingFn: (a, b) => STATUS_ORDER.indexOf(a.original.status?.value) - STATUS_ORDER.indexOf(b.original.status?.value),
    cell: ({ row, table }) => (
      <StatusCell
        id={row.original.id}
        status={row.original.status}
        choices={table.options.meta?.statusChoices}
        onSaved={table.options.meta?.updateRow}
      />
    ),
  }),

  h.accessor((r) => r.site?.name || '', {
    id: 'site',
    header: 'Площадка',
    meta: defMeta('site'),
    sortingFn: byText((r) => r.site?.name),
    cell: ({ row }) => {
      const site = row.original.site;
      if (!site) return <span className="ipt-faint" title="Адрес не привязан ни к устройству, ни к подсети с площадкой">Без площадки</span>;
      return <span className="ipt-txt" title={`Площадка: ${site.name}`}>{site.name}</span>;
    },
  }),

  h.accessor('dns_name', {
    id: 'dns_name',
    header: 'DNS-имя',
    meta: defMeta('dns_name'),
    cell: ({ getValue, row, table }) => (
      <span className="ipt-cell-copy">
        <EditableText id={row.original.id} field="dns_name" value={getValue()} mono onSaved={table.options.meta?.updateRow} />
        <CopyButton value={getValue()} />
      </span>
    ),
  }),

  h.accessor('effective_description', {
    id: 'description',
    header: 'Описание',
    meta: defMeta('description'),
    cell: ({ getValue, row, table }) => (
      <span className="ipt-cell-copy">
        <EditableText
          id={row.original.id}
          field="description"
          value={row.original.description}
          display={getValue()}
          editable={!!row.original.description_editable}
          title={row.original.description_hint || getValue() || undefined}
          muted
          onSaved={table.options.meta?.updateRow}
        />
        <CopyButton value={getValue()} />
      </span>
    ),
  }),

  h.accessor((r) => (r.tags || []).join(', '), {
    id: 'tags',
    header: 'Тэги',
    meta: defMeta('tags'),
    cell: ({ row }) => {
      const tags = row.original.tags || [];
      if (!tags.length) return <Empty />;
      return (
        <span className="ipt-tags" title={tags.join(', ')}>
          {tags.map((t) => <span key={t} className="ipt-tag">{t}</span>)}
        </span>
      );
    },
  }),

  h.accessor('os_installed', {
    id: 'os_installed',
    header: 'ОС',
    meta: defMeta('os_installed'),
    // Своё меню со справочником (window.OS_CATALOG_OPTIONS), открывается
    // одним кликом — см. components/os-menu.jsx.
    cell: ({ getValue, row, table }) => <OsMenuCell id={row.original.id} value={getValue()} table={table} />,
  }),

  h.accessor('open_ports', {
    id: 'open_ports',
    header: 'Порты',
    meta: defMeta('open_ports'),
    cell: ({ getValue }) => <MonoCell value={getValue()} muted />,
  }),

  h.accessor((r) => r.role?.name || '', {
    id: 'role',
    header: 'Роль',
    meta: defMeta('role'),
    cell: ({ row }) => {
      const role = row.original.role;
      if (!role) return <Empty />;
      return <span className="ipt-role ipt-mono" title={role.name}>{role.slug || role.name}</span>;
    },
  }),

  h.accessor('purpose', {
    id: 'purpose',
    header: 'Назначение',
    meta: defMeta('purpose'),
    cell: ({ getValue, row, table }) => (
      <EditableText id={row.original.id} field="purpose" value={getValue()} onSaved={table.options.meta?.updateRow} />
    ),
  }),

  h.accessor('assignment', {
    id: 'assignment',
    header: 'Принадлежность',
    meta: defMeta('assignment'),
    cell: ({ getValue }) => (getValue() ? <span className="ipt-txt ipt-muted" title={getValue()}>{getValue()}</span> : <Empty />),
  }),

  h.accessor('dns_records', {
    id: 'dns_records',
    header: 'DNS-записи',
    meta: defMeta('dns_records'),
    cell: ({ getValue }) => <MonoCell value={getValue()} />,
  }),

  h.accessor('comments', {
    id: 'comments',
    header: 'Комментарии',
    meta: defMeta('comments'),
    cell: ({ getValue, row, table }) => (
      <EditableText id={row.original.id} field="comments" value={getValue()} muted onSaved={table.options.meta?.updateRow} />
    ),
  }),

  h.accessor('guest_os', {
    id: 'guest_os',
    header: 'Guest OS',
    meta: defMeta('guest_os'),
    cell: ({ getValue }) => <MonoCell value={getValue()} />,
  }),

  h.accessor('os_guess', {
    id: 'os_guess',
    header: 'OS Guess',
    meta: defMeta('os_guess'),
    cell: ({ getValue }) => <MonoCell value={getValue()} />,
  }),

  h.accessor('cpe', {
    id: 'cpe',
    header: 'CPE',
    meta: defMeta('cpe'),
    cell: ({ getValue }) => <MonoCell value={getValue()} />,
  }),

  h.display({
    id: 'actions',
    enableHiding: false,
    enableSorting: false,
    header: () => null,
    cell: ({ row, table }) => {
      const meta = table.options.meta || {};
      const id = String(row.original.id);
      return (
        <RowMenu
          address={stripMask(row.original.address)}
          isVm={row.original.role?.slug === 'vm'}
          open={meta.menuRowId === id}
          onToggle={(open) => meta.setMenuRowId?.(open ? id : null)}
          onAction={(action) => meta.onRowAction?.(action, row.original)}
        />
      );
    },
  }),
];
