// Корень острова «Карта сети»: store, загрузка данных и раскладки,
// вычисление позиций и графа текущего слоя, сохранение раскладки.

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import {
  MapContext, reducer, initialState, ACTIONS, writeFilters, writeMode,
} from './state/store.js';
import { buildModel } from './state/model.js';
import { fetchMapData, fetchLayout, putLayout, deleteLayout } from './api.js';
import { computeVisibility } from './layout/visibility.js';
import { computeRolesLayout } from './layout/roles.js';
import { effectivePositions, physicalPositions, groupFrame, logicalSize } from './layout/positions.js';
import { buildLogicalEdges, buildLogicalGraph } from './graph/logical.js';
import { buildCards, buildPhysicalGraph } from './graph/physical.js';
import { DEV_W } from './constants.js';
import { formatWhen, plural, W } from './format.js';
import Toolbar from './components/Toolbar.jsx';
import MapCanvas from './components/MapCanvas.jsx';
import RightPanel from './panel/RightPanel.jsx';
import { exportPng } from './export.js';
import { IconRefresh } from './components/Icons.jsx';

const SAVE_DEBOUNCE_MS = 600;

function useColorMode() {
  const get = () => (document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');
  const [mode, setMode] = useState(get);
  useEffect(() => {
    const obs = new MutationObserver(() => setMode(get()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);
  return mode;
}

// «Связи обновлены …» — в шапку раздела рядом с заголовком (как в макете).
function useHeadSlot() {
  const [slot, setSlot] = useState(null);
  useEffect(() => {
    const host = document.querySelector('.shell-head-left');
    if (!host) return undefined;
    const el = document.createElement('span');
    el.className = 'tm-head-note';
    host.appendChild(el);
    setSlot(el);
    return () => el.remove();
  }, []);
  return slot;
}

function MapInner({ boot }) {
  const [state, dispatch] = useReducer(reducer, boot, initialState);
  const rf = useReactFlow();
  const colorMode = useColorMode();
  const headSlot = useHeadSlot();
  const rootRef = useRef(null);
  const fitRef = useRef(0);
  const [exporting, setExporting] = useState(false);
  const model = state.model;

  // ---------- сохранение раскладки (debounce) ----------
  const pending = useRef(null);
  const flush = useCallback(async (keepalive = false) => {
    const p = pending.current;
    if (!p) return;
    pending.current = null;
    if (!keepalive) dispatch({ type: ACTIONS.SAVE_STATUS, status: 'saving' });
    try {
      await putLayout(boot.layoutUrl, p.site, p.payload, keepalive);
      if (!keepalive) dispatch({ type: ACTIONS.SAVE_STATUS, status: 'saved' });
    } catch (e) {
      if (!keepalive) dispatch({ type: ACTIONS.SAVE_STATUS, status: 'error' });
    }
  }, [boot.layoutUrl]);

  useEffect(() => {
    if (!state.saveSeq || state.status !== 'ready') return undefined;
    pending.current = {
      site: state.site,
      payload: { positions: state.saved, expanded: state.expanded, locked: state.locked },
    };
    const t = setTimeout(() => flush(), SAVE_DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.saveSeq]);

  useEffect(() => {
    const onHide = () => flush(true);
    window.addEventListener('pagehide', onHide);
    return () => window.removeEventListener('pagehide', onHide);
  }, [flush]);

  useEffect(() => { writeFilters(state); }, [state.hiddenKinds, state.hiddenSources]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (state.status === 'ready') writeMode(state.site, state.mode); }, [state.mode, state.status, state.site]);

  // ---------- загрузка ----------
  const reqSeq = useRef(0);
  const load = useCallback(async (site) => {
    await flush();
    const seq = reqSeq.current + 1;
    reqSeq.current = seq;
    dispatch({ type: ACTIONS.LOAD_START, site });
    try {
      const url = new URL(window.location.href);
      url.searchParams.set('site', site);
      window.history.replaceState(window.history.state, '', url);
    } catch (e) { /* — */ }
    try {
      const [raw, layout] = await Promise.all([
        fetchMapData(boot.dataUrl, site),
        fetchLayout(boot.layoutUrl, site),
      ]);
      if (seq !== reqSeq.current) return;
      dispatch({ type: ACTIONS.LOAD_OK, model: buildModel(raw), layout });
    } catch (e) {
      if (seq !== reqSeq.current) return;
      dispatch({ type: ACTIONS.LOAD_ERROR, error: e.message || String(e) });
    }
  }, [boot.dataUrl, boot.layoutUrl, flush]);

  useEffect(() => { load(state.site); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- Esc снимает выбор ----------
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      const tag = (e.target?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
      dispatch({ type: ACTIONS.SELECT_CLEAR });
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // ---------- производные: видимость → раскладка → граф ----------
  const expandedSet = useMemo(() => new Set(state.expanded), [state.expanded]);
  const hiddenKinds = useMemo(() => new Set(state.hiddenKinds), [state.hiddenKinds]);
  const hiddenSources = useMemo(() => new Set(state.hiddenSources), [state.hiddenSources]);
  const vis = useMemo(() => (model ? computeVisibility(model, expandedSet, hiddenKinds) : null), [model, expandedSet, hiddenKinds]);
  const roles = useMemo(() => (vis ? computeRolesLayout(model, vis, { multiSite: model.site.slug === '*' }) : null), [model, vis]);
  const pos = useMemo(() => (roles ? effectivePositions(model, vis, roles, state.mode, state.saved) : null),
    [model, vis, roles, state.mode, state.saved]);
  const cards = useMemo(() => (model ? buildCards(model) : null), [model]);
  const physical = state.layer === 'physical';
  const physPos = useMemo(() => {
    if (!physical || !pos) return null;
    return physicalPositions(model, vis, pos, (id) => {
      const c = cards.get(id);
      return c ? [c.w, c.h] : [228, 70];
    });
  }, [physical, model, vis, pos, cards]);
  const built = useMemo(() => (vis ? buildLogicalEdges(model, vis, hiddenSources) : null), [model, vis, hiddenSources]);

  const graph = useMemo(() => {
    if (!model || !pos) return { nodes: [], edges: [] };
    if (physical) return buildPhysicalGraph({ model, vis, pos: physPos, cards, state, query: state.query });
    return buildLogicalGraph({ model, vis, pos, roles, mode: state.mode, state, query: state.query, built });
    // state целиком не нужен — только перечисленное ниже
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, vis, pos, physPos, cards, roles, built, physical, state.mode, state.selection, state.query, state.hiddenSources, state.site]);

  // ---------- действия ----------
  const capture = useCallback(() => {
    const out = {};
    vis.devices.forEach((n) => { if (pos[n.id]) out[n.id] = pos[n.id]; });
    vis.collapsed.forEach((g) => { if (pos[g.id]) out[g.id] = pos[g.id]; });
    vis.expanded.forEach((g) => {
      const f = groupFrame(vis.groupMembers.get(g.id), pos, logicalSize);
      if (f) out[g.id] = [f.x + f.w / 2 - DEV_W / 2, f.y];
    });
    return out;
  }, [vis, pos]);

  const onMode = useCallback((mode) => {
    dispatch({ type: ACTIONS.MODE_SET, mode, capture: mode === 'free' ? capture() : undefined });
  }, [capture]);

  const onCommit = useCallback((liveNodes) => {
    // клик с дрожанием мыши — не перетаскивание: без сдвига ничего не сохраняем
    const moved = liveNodes.some((n) => (n.type === 'device' || n.type === 'hostgroup')
      && pos?.[n.id] && (Math.abs(pos[n.id][0] - n.position.x) > 0.5 || Math.abs(pos[n.id][1] - n.position.y) > 0.5));
    if (!moved) return;
    const positions = {};
    liveNodes.forEach((n) => {
      const p = [Math.round(n.position.x * 10) / 10, Math.round(n.position.y * 10) / 10];
      if (n.type === 'device' || n.type === 'hostgroup') positions[n.id] = p;
      else if (n.type === 'frame') positions[n.data.groupId] = [Math.round(n.position.x + (n.width || 0) / 2 - DEV_W / 2), Math.round(n.position.y)];
    });
    dispatch({ type: ACTIONS.POSITIONS_COMMIT, positions });
  }, [pos]);

  const onReset = useCallback(async () => {
    pending.current = null;
    try {
      await deleteLayout(boot.layoutUrl, state.site);
      dispatch({ type: ACTIONS.LAYOUT_RESET });
    } catch (e) {
      dispatch({ type: ACTIONS.SAVE_STATUS, status: 'error' });
    }
  }, [boot.layoutUrl, state.site]);

  const onExport = useCallback(async () => {
    setExporting(true);
    try {
      await exportPng(rf, rootRef.current, model?.site?.slug || state.site);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error('Экспорт карты не удался', e);
      window.alert('Не удалось сохранить картинку карты.');
    } finally {
      setExporting(false);
    }
  }, [rf, model, state.site]);

  // Какие узлы полотна показать при центрировании на цели.
  const resolveFocus = useCallback((target) => {
    if (!model || !vis) return [];
    if (target.type === 'node') {
      const rep = vis.repOf(target.id);
      return rep ? [rep] : [];
    }
    if (target.type === 'link') {
      const l = model.linksById.get(target.id);
      if (!l) return [];
      return [...new Set([vis.repOf(l.a.node), vis.repOf(l.b.node)].filter(Boolean))];
    }
    return [];
  }, [model, vis]);

  const ctx = useMemo(() => ({ state, dispatch, model }), [state, model]);

  const updated = model
    ? (model.collectedAt ? `Связи обновлены ${formatWhen(model.collectedAt)}` : 'Топология ещё не строилась')
    : '';
  let counts = null;
  if (model) {
    const s = model.summary;
    const nDev = s.devices ?? model.nodes.length;
    const n2 = physical ? (s.cables ?? model.cables.length) : (s.links ?? model.links.length);
    counts = (
      <span className="tm-counts">
        <b>{nDev}</b> {plural(nDev, W.device)} · <b>{n2}</b> {plural(n2, physical ? W.cable : W.link)}
      </span>
    );
  }

  return (
    <MapContext.Provider value={ctx}>
      <div className="tm" ref={rootRef}>
        {headSlot && model && createPortal(
          <>
            {counts}
            <span className="tm-head-sep" />
            <span title={model.partialRun ? 'Последний прогон был частичным' : undefined}>{updated}{model.partialRun ? ' · частично' : ''}</span>
          </>,
          headSlot,
        )}
        <Toolbar
          onMode={onMode}
          onSite={(site) => load(site)}
          onExport={onExport}
          onReset={onReset}
          exporting={exporting}
        />
        <div className="tm-body">
          <div className="tm-canvas">
            {state.status === 'ready' && model && (
              <MapCanvas key={`${state.layer}`} graph={graph} onCommit={onCommit} resolveFocus={resolveFocus} colorMode={colorMode} fitRef={fitRef} />
            )}
            {state.status === 'loading' && (
              <div className="shell-loading tm-overlay"><span className="shell-spinner" aria-hidden="true" />Загрузка карты…</div>
            )}
            {state.status === 'error' && (
              <div className="tm-overlay tm-error" role="alert">
                <b>Карту не удалось загрузить</b>
                <span>{state.error}</span>
                <button type="button" className="tm-btn is-primary" onClick={() => load(state.site)}>
                  <IconRefresh />Повторить
                </button>
              </div>
            )}
            {state.status === 'ready' && model && model.nodes.length === 0 && (
              <div className="tm-overlay tm-empty">На этой площадке нет устройств со связями.</div>
            )}
          </div>
          {state.status === 'ready' && model && <RightPanel logicalEdges={built?.edges} />}
        </div>
        {!headSlot && model && <span className="tm-updated-fallback">{counts} · {updated}</span>}
      </div>
    </MapContext.Provider>
  );
}

export default function MapApp({ boot }) {
  return (
    <ReactFlowProvider>
      <MapInner boot={boot} />
    </ReactFlowProvider>
  );
}
