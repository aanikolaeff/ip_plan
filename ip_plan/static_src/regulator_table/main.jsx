import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import TableRoot from './TableRoot.jsx';
// Тот же файл, что и у ip_table — не собственный table.css: id="ip-table"
// на <table> ниже намеренно совпадает с основной IP-таблицей, чтобы
// унаследовать все #ip-table-скоуплены правила (шапка/вертикальное
// выравнивание/зебра и т.д.) "на той же основе" без дублирования CSS. Обе
// таблицы никогда не видны одновременно (разные страницы), так что общий id
// не конфликтует.
import '../ip_table/table.css';

const container = document.getElementById('regulator-entries-root');
if (container) {
  createRoot(container).render(
    <StrictMode>
      <TableRoot />
    </StrictMode>,
  );
} else {
  // eslint-disable-next-line no-console
  console.error('#regulator-entries-root не найден — React-таблица записей не смонтирована');
}
