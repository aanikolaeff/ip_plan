// Панель инструментов: слой, поиск, расположение, роли, площадка, вид,
// экспорт картинки. Счётчики и время прогона — в шапке раздела (MapApp).

import { useMap, ACTIONS } from '../state/store.js';
import { IconPhys, IconLogic, IconDownload, IconPin } from './Icons.jsx';
import SearchBox from './SearchBox.jsx';
import RolesMenu from './RolesMenu.jsx';
import ViewMenu from './ViewMenu.jsx';

const LAYERS = [
  ['physical', 'Физический', 'Кабели: интерфейс — интерфейс', IconPhys],
  ['logical', 'Логический', 'Связи устройств: агрегат или бонд — одна линия', IconLogic],
];
const MODES = [['roles', 'По ролям'], ['free', 'Свободно']];

export default function Toolbar({ onMode, onSite, onExport, onReset, exporting }) {
  const { model, state, dispatch } = useMap();
  const sites = state.boot.sites || [];

  return (
    <div className="tm-toolbar">
      <div className="tm-seg is-dark" role="group" aria-label="Слой карты">
        {LAYERS.map(([key, label, hint, Ico]) => (
          <button
            key={key}
            type="button"
            aria-pressed={state.layer === key}
            title={hint}
            className={state.layer === key ? 'is-on' : ''}
            onClick={() => dispatch({ type: ACTIONS.LAYER_SET, layer: key })}
          >
            <Ico />{label}
          </button>
        ))}
      </div>
      <span className="tm-vsep" />
      <SearchBox />
      <div className="tm-seg" role="group" aria-label="Расположение">
        {MODES.map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={state.mode === key}
            className={state.mode === key ? 'is-on' : ''}
            disabled={!model}
            title={key === 'roles' ? 'Полосы по ролям, порядок — по соседям' : 'Своё расположение: перетаскивайте устройства'}
            onClick={() => onMode(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <RolesMenu />
      <label className="tm-site" title="Площадка карты">
        <IconPin style={{ color: 'var(--accent)' }} />
        <select aria-label="Площадка карты" value={state.site} onChange={(e) => onSite(e.target.value)}>
          {sites.map((x) => <option key={x.slug} value={x.slug}>{x.name}</option>)}
          <option value="*">Все площадки</option>
        </select>
      </label>
      <ViewMenu onReset={onReset} />
      <span className="tm-grow" />
      <button
        type="button"
        className="tm-tbtn is-icon"
        aria-label="Скачать как картинку"
        title="Скачать как картинку (PNG)"
        disabled={!model || exporting}
        onClick={onExport}
      >
        <IconDownload />
      </button>
    </div>
  );
}
