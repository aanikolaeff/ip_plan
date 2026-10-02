// Меню вида: свернуть/развернуть все группы, сбросить раскладку.
import { useMap, ACTIONS } from '../state/store.js';
import { IconView } from './Icons.jsx';
import usePopover from './usePopover.js';

export default function ViewMenu({ onReset }) {
  const { model, state, dispatch } = useMap();
  const { open, setOpen, ref } = usePopover();
  const groups = model?.groups || [];
  const run = (fn) => () => { setOpen(false); fn(); };
  return (
    <div className="tm-popwrap" ref={ref}>
      <button type="button" className="tm-tbtn is-icon" aria-label="Вид" title="Вид" aria-haspopup="menu" aria-expanded={open} disabled={!model} onClick={() => setOpen(!open)}>
        <IconView />
      </button>
      {open && (
        <div className="tm-pop tm-menu" role="menu">
          <button type="button" role="menuitem" disabled={!groups.length || !state.expanded.length}
            onClick={run(() => dispatch({ type: ACTIONS.GROUPS_SET, ids: [] }))}>
            Свернуть все группы
          </button>
          <button type="button" role="menuitem" disabled={!groups.length || state.expanded.length === groups.length}
            onClick={run(() => dispatch({ type: ACTIONS.GROUPS_SET, ids: groups.map((g) => g.id) }))}>
            Развернуть все группы
          </button>
          <button type="button" role="menuitem" onClick={run(() => dispatch({ type: ACTIONS.LEGEND_TOGGLE }))}>
            {state.legendOpen ? 'Скрыть легенду' : 'Показать легенду'}
          </button>
          <div className="tm-menu-sep" />
          <button type="button" role="menuitem" className="is-danger" onClick={run(onReset)}>
            Сбросить раскладку
            <span className="tm-menu-hint">Своё расположение и развёрнутые группы удаляются, карта — снова «По ролям»</span>
          </button>
        </div>
      )}
    </div>
  );
}
