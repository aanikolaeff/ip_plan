import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import TableRoot from './TableRoot.jsx';
import './table.css';

const container = document.getElementById('ip-table-root');
if (container) {
  createRoot(container).render(
    <StrictMode>
      <TableRoot />
    </StrictMode>,
  );
} else {
  // eslint-disable-next-line no-console
  console.error('#ip-table-root не найден — React-таблица не смонтирована');
}
