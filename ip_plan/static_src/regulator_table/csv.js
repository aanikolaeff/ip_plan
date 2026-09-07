// Экспорт видимых (уже отфильтрованных) записей в CSV — чисто клиентский,
// без похода на сервер: данные и так уже в памяти (см. TableRoot.jsx,
// filteredRows). ';' как разделитель, а не ',' — так CSV с кириллицей и
// текстом, содержащим запятые (например, причина снятия), открывается в
// Excel одной колонкой без ручного импорта через мастер текста (Excel в
// русской локали и так использует ',' как десятичный разделитель, ';' —
// его собственный CSV-разделитель по умолчанию). BOM в начале файла — та
// же причина: без него Excel показывает кириллицу как "кракозябры",
// определяя кодировку по умолчанию как не-UTF-8.

function csvField(value) {
  const str = value === null || value === undefined ? '' : String(value);
  if (/[";\n]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('ru-RU');
}

function statusText(entry) {
  if (entry.status !== 'revoked') return 'активна';
  const parts = ['снята'];
  if (entry.revoked_at) parts.push(formatDate(entry.revoked_at));
  if (entry.revoked_reason) parts.push(`(${entry.revoked_reason})`);
  return parts.join(' ');
}

export function entriesToCsv(entries) {
  const header = ['Тип', 'Значение', 'Статус', 'Добавлена'];
  const lines = [header.map(csvField).join(';')];
  for (const entry of entries) {
    lines.push([
      entry.kind_display,
      entry.value,
      statusText(entry),
      formatDate(entry.created_at),
    ].map(csvField).join(';'));
  }
  return `\uFEFF${lines.join('\r\n')}`;
}

// Скачивание через временную <a download> — тот же приём, что и везде для
// клиентских файловых экспортов без похода на сервер: создаём blob-URL,
// кликаем по невидимой ссылке, сразу освобождаем URL.
export function downloadCsv(filename, csvContent) {
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
