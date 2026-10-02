import { useEffect, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

/** Плавающая панель у якоря: портал в document.body, position:fixed,
 * прижимается к границам вьюпорта, при нехватке места снизу
 * переворачивается вверх. Закрывается по Escape и по клику вне панели
 * и якоря. Клики внутри ЛЮБОЙ другой панели (data-ipt-pop) считаются
 * «внутри» — так вложенные поповеры (например, выбор статуса из
 * плавающей панели выделения) не закрывают родителя.
 *
 * align: 'start' — левый край по левому краю якоря, 'end' — правый по
 * правому. Позиция пересчитывается при ресайзе и любом скролле. */
export function Popover({ anchorRef, open, onClose, align = 'start', offset = 4, className = '', role = 'menu', ariaLabel, children, style }) {
  const panelRef = useRef(null);

  useLayoutEffect(() => {
    if (!open) return undefined;
    const place = () => {
      const panel = panelRef.current;
      const anchor = anchorRef?.current;
      if (!panel || !anchor) return;
      const rect = anchor.getBoundingClientRect();
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      let left = align === 'end' ? rect.right - width : rect.left;
      left = Math.max(8, Math.min(left, window.innerWidth - 8 - width));
      let top = rect.bottom + offset;
      const spaceBelow = window.innerHeight - 8 - rect.bottom - offset;
      const spaceAbove = rect.top - offset - 8;
      if (height > spaceBelow && spaceAbove > spaceBelow) {
        top = Math.max(8, rect.top - offset - height);
      } else {
        top = Math.max(8, Math.min(top, window.innerHeight - 8 - height));
      }
      panel.style.left = `${left}px`;
      panel.style.top = `${top}px`;
    };
    place();
    window.addEventListener('resize', place);
    document.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      document.removeEventListener('scroll', place, true);
    };
  }, [open, anchorRef, align, offset, children]);

  useEffect(() => {
    if (!open) return undefined;
    function onMouseDown(e) {
      if (panelRef.current?.contains(e.target)) return;
      if (anchorRef?.current?.contains(e.target)) return;
      if (e.target.closest?.('[data-ipt-pop]')) return;
      onClose?.();
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); onClose?.(); }
    }
    // mousedown, не click — клик, которым открыли панель, успевает
    // завершиться раньше, чем навешивается этот обработчик.
    document.addEventListener('mousedown', onMouseDown, true);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('mousedown', onMouseDown, true);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open, onClose, anchorRef]);

  if (!open) return null;
  return createPortal(
    <div ref={panelRef} data-ipt-pop="" role={role} aria-label={ariaLabel} className={`ipt-pop ${className}`} style={style}>
      {children}
    </div>,
    document.body,
  );
}
