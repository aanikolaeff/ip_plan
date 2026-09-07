// См. showToast в ip_table/components/cells.jsx — тот же CustomEvent,
// слушатель общий (base.html, toast-stack), дублируется тут в одну
// функцию, чтобы не тянуть весь cells.jsx ip_table ради одного экспорта.
export function showToast(message, kind) {
  window.dispatchEvent(new CustomEvent('show-toast', { detail: { message, kind } }));
}
