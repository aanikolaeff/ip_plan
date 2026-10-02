// Единый store карты: useReducer + контекст. Все действия пользователя —
// выбор, слой, раскладка, группы, фильтры — проходят через один dispatch,
// поэтому будущие действия с кабелями (создать/удалить) добавляются сюда же
// новыми типами, без переделки компонентов. См. ACTIONS ниже.

import { createContext, useContext } from 'react';

export const ACTIONS = {
  LOAD_START: 'load/start',
  LOAD_OK: 'load/ok',
  LOAD_ERROR: 'load/error',
  LAYER_SET: 'layer/set',
  MODE_SET: 'mode/set',
  POSITIONS_COMMIT: 'positions/commit',
  LOCK_TOGGLE: 'lock/toggle',
  LAYOUT_RESET: 'layout/reset',
  GROUP_TOGGLE: 'group/toggle',
  GROUPS_SET: 'groups/set',
  SELECT: 'select',
  SELECT_CLEAR: 'select/clear',
  FOCUS: 'focus',
  SOURCE_TOGGLE: 'source/toggle',
  KINDS_SET: 'kinds/set',
  LEGEND_TOGGLE: 'legend/toggle',
  SAVE_STATUS: 'save/status',
  QUERY_SET: 'query/set',
  // На вырост (ручные связи): CABLE_CREATED / CABLE_DELETED получат ответ API
  // и заменят model через upsertCable/removeCable из state/model.js.
};

const FILTERS_KEY = 'topo_map_filters';

function readFilters() {
  try {
    const v = JSON.parse(localStorage.getItem(FILTERS_KEY) || '{}');
    return {
      hiddenSources: Array.isArray(v.hiddenSources) ? v.hiddenSources : [],
      hiddenKinds: Array.isArray(v.hiddenKinds) ? v.hiddenKinds : [],
    };
  } catch (e) {
    return { hiddenSources: [], hiddenKinds: [] };
  }
}
export function writeFilters(state) {
  try {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ hiddenSources: state.hiddenSources, hiddenKinds: state.hiddenKinds }));
  } catch (e) { /* приватный режим — не страшно */ }
}

const modeKey = (site) => `topo_map_mode:${site}`;
export function readMode(site) {
  try { return localStorage.getItem(modeKey(site)); } catch (e) { return null; }
}
export function writeMode(site, mode) {
  try { localStorage.setItem(modeKey(site), mode); } catch (e) { /* — */ }
}

export function initialState(boot) {
  return {
    boot,
    site: boot.site || '*',
    status: 'loading',
    error: null,
    model: null,
    layer: 'logical',
    mode: 'roles',
    saved: {},
    expanded: [],
    locked: false,
    selection: null,
    legendOpen: false,
    focus: null,
    query: '',
    fitSeq: 0,
    saveSeq: 0,
    saveStatus: 'idle',
    ...readFilters(),
  };
}

const sameSel = (a, b) => !!a && !!b && a.type === b.type && a.id === b.id;

export function reducer(state, action) {
  switch (action.type) {
    case ACTIONS.LOAD_START:
      return { ...state, site: action.site ?? state.site, status: 'loading', error: null, selection: null, focus: null };
    case ACTIONS.LOAD_OK: {
      const layout = action.layout || {};
      const positions = layout.positions && typeof layout.positions === 'object' ? layout.positions : {};
      const hasSaved = Object.keys(positions).length > 0;
      const stored = readMode(state.site);
      const mode = stored === 'free' || stored === 'roles' ? stored : (hasSaved ? 'free' : 'roles');
      const groupIds = new Set(action.model.groups.map((g) => g.id));
      return {
        ...state,
        status: 'ready',
        model: action.model,
        saved: positions,
        expanded: (Array.isArray(layout.expanded) ? layout.expanded : []).filter((id) => groupIds.has(id)),
        locked: !!layout.locked,
        mode,
        selection: null,
        fitSeq: state.fitSeq + 1,
        saveStatus: 'idle',
      };
    }
    case ACTIONS.LOAD_ERROR:
      return { ...state, status: 'error', error: action.error };
    case ACTIONS.LAYER_SET:
      if (action.layer === state.layer) return state;
      return {
        ...state,
        layer: action.layer,
        selection: action.selection !== undefined ? action.selection : state.selection,
        focus: action.focus ? { ...action.focus, seq: (state.focus?.seq || 0) + 1 } : null,
        fitSeq: action.focus ? state.fitSeq : state.fitSeq + 1,
      };
    case ACTIONS.MODE_SET: {
      if (action.mode === state.mode) return state;
      let { saved, saveSeq } = state;
      if (action.mode === 'free' && Object.keys(saved).length === 0 && action.capture) {
        saved = action.capture;
        saveSeq += 1;
      }
      return { ...state, mode: action.mode, saved, saveSeq, fitSeq: state.fitSeq + 1 };
    }
    case ACTIONS.POSITIONS_COMMIT:
      return {
        ...state,
        saved: state.mode === 'free' ? { ...state.saved, ...action.positions } : action.positions,
        mode: 'free',
        saveSeq: state.saveSeq + 1,
      };
    case ACTIONS.LOCK_TOGGLE:
      return { ...state, locked: !state.locked, saveSeq: state.saveSeq + 1 };
    case ACTIONS.LAYOUT_RESET:
      return { ...state, saved: {}, expanded: [], locked: false, mode: 'roles', fitSeq: state.fitSeq + 1, saveStatus: 'idle' };
    case ACTIONS.GROUP_TOGGLE: {
      const has = state.expanded.includes(action.id);
      const expanded = has ? state.expanded.filter((x) => x !== action.id) : [...state.expanded, action.id];
      let { selection } = state;
      // свернули группу, внутри которой был выбран узел — выбор снимаем
      if (has && selection?.type === 'node' && state.model?.groupOf.get(selection.id) === action.id) selection = null;
      return { ...state, expanded, selection, saveSeq: state.saveSeq + 1 };
    }
    case ACTIONS.GROUPS_SET:
      return { ...state, expanded: action.ids, selection: null, saveSeq: state.saveSeq + 1, fitSeq: state.fitSeq + 1 };
    case ACTIONS.SELECT:
      if (!action.selection || (sameSel(state.selection, action.selection) && !action.keep)) {
        return { ...state, selection: null };
      }
      return { ...state, selection: action.selection };
    case ACTIONS.SELECT_CLEAR:
      return state.selection ? { ...state, selection: null } : state;
    case ACTIONS.FOCUS: {
      // Поиск/переход: раскрыть свёрнутую группу, выбрать и центрировать.
      let { expanded, saveSeq } = state;
      const target = action.target; // {type:'node'|'link', id}
      if (target.type === 'node') {
        const g = state.model?.groupOf.get(target.id);
        if (g && !expanded.includes(g)) {
          expanded = [...expanded, g];
          saveSeq += 1;
        }
      }
      return {
        ...state,
        expanded,
        saveSeq,
        selection: target,
        focus: { ...target, seq: (state.focus?.seq || 0) + 1 },
      };
    }
    case ACTIONS.SOURCE_TOGGLE: {
      const has = state.hiddenSources.includes(action.key);
      return { ...state, hiddenSources: has ? state.hiddenSources.filter((k) => k !== action.key) : [...state.hiddenSources, action.key] };
    }
    case ACTIONS.KINDS_SET:
      return { ...state, hiddenKinds: action.hidden };
    case ACTIONS.LEGEND_TOGGLE:
      return { ...state, legendOpen: action.open ?? !state.legendOpen };
    case ACTIONS.QUERY_SET:
      return { ...state, query: action.query };
    case ACTIONS.SAVE_STATUS:
      return { ...state, saveStatus: action.status };
    default:
      return state;
  }
}

export const MapContext = createContext(null);
export const useMap = () => useContext(MapContext);
