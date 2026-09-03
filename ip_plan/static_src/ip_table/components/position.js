// Общая логика позиционирования плавающих панелей (position:fixed,
// портированных в document.body) рядом с якорем — переиспользуется
// os-menu.jsx и kebab-menu.jsx. Перенесено из positionFixedPanel()/
// positionSubmenu() в app.js (старая DataTables-версия): и там, и там —
// прижать к границам вьюпорта, не выпуская панель за экран.
export function positionNear(anchorRect, panelEl, { flyout } = {}) {
  const width = panelEl.offsetWidth;
  const height = panelEl.offsetHeight;
  let left;
  let top;
  if (flyout) {
    // Подменю — выезжает справа от родительского пункта, при нехватке
    // места переворачивается влево.
    left = anchorRect.right + 2;
    if (left + width > window.innerWidth - 8) left = anchorRect.left - width - 2;
    top = Math.min(anchorRect.top, window.innerHeight - 8 - height);
  } else {
    left = Math.min(anchorRect.left, window.innerWidth - 8 - width);
    top = Math.min(anchorRect.bottom + 4, window.innerHeight - 8 - height);
  }
  left = Math.max(left, 8);
  top = Math.max(top, 8);
  panelEl.style.left = `${left}px`;
  panelEl.style.top = `${top}px`;
}
