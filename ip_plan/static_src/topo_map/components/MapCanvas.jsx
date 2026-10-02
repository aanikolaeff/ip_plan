// Полотно React Flow: узлы/рёбра текущего слоя, перетаскивание с
// сохранением, выбор, центрирование, кнопки масштаба, миникарта, легенда.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ReactFlow, Background, BackgroundVariant, MiniMap, Panel, applyNodeChanges, useReactFlow, ConnectionMode } from '@xyflow/react';
import { useMap, ACTIONS } from '../state/store.js';
import DeviceNode from '../nodes/DeviceNode.jsx';
import GroupNode from '../nodes/GroupNode.jsx';
import FrameNode from '../nodes/FrameNode.jsx';
import BandNode from '../nodes/BandNode.jsx';
import { MemoCardNode, MemoPhysGroupNode } from '../nodes/CardNode.jsx';
import LinkEdge from '../edges/LinkEdge.jsx';
import CableEdge from '../edges/CableEdge.jsx';
import Legend from './Legend.jsx';
import { groupFrame, logicalSize } from '../layout/positions.js';
import {
  IconPlus, IconMinus, IconFit, IconLock, IconUnlock, IconLegend,
} from './Icons.jsx';

const nodeTypes = {
  device: DeviceNode,
  hostgroup: GroupNode,
  frame: FrameNode,
  band: BandNode,
  card: MemoCardNode,
  physgroup: MemoPhysGroupNode,
};
const edgeTypes = { link: LinkEdge, cable: CableEdge };

// Поля «Показать всё»: сверху — кнопка «Легенда», снизу — масштаб и миникарта.
export const FIT_PADDING = { top: '56px', right: '28px', bottom: '84px', left: '28px' };

// Физический слой высокий (карточки с портами): вместо «всего в точку» —
// читаемый масштаб, ширина по центру, верх графа у верхнего края.
function fitTop(rf, nodes) {
  if (!nodes.length) return;
  const b = rf.getNodesBounds(nodes);
  const el = document.querySelector('.tm-flow');
  const w = el?.clientWidth || 800;
  const h = el?.clientHeight || 600;
  const fit = Math.min((w - 56) / b.width, (h - 140) / b.height);
  const zoom = Math.max(0.6, Math.min(1.1, fit));
  const y = fit >= 0.6 ? (h - b.height * zoom) / 2 - b.y * zoom : 56 - b.y * zoom;
  // не помещается по ширине — прижать к левому краю (как к верху по высоте),
  // иначе центрирование обрезает граф с обеих сторон
  const x = b.width * zoom > w - 56 ? 28 - b.x * zoom : w / 2 - (b.x + b.width / 2) * zoom;
  rf.setViewport({ x, y, zoom }, { duration: 250 });
}

const FAM_COLOR = { net: '#4f4fc8', sec: '#c2410c', cmp: '#0f766e', oth: '#8c8c96' };

function miniColor(model) {
  return (n) => {
    if (n.type === 'band' || n.type === 'frame') return 'transparent';
    if (n.type === 'hostgroup' || n.type === 'physgroup') return FAM_COLOR[model.groupsById.get(n.data.groupId)?.family] || FAM_COLOR.oth;
    return FAM_COLOR[model.nodesById.get(n.data.nodeId)?.family] || FAM_COLOR.oth;
  };
}

function SaveNote() {
  const { state } = useMap();
  if (state.saveStatus === 'saving') return <span className="tm-save">Сохраняю расположение…</span>;
  if (state.saveStatus === 'saved') return <span className="tm-save is-ok">Расположение сохранено</span>;
  if (state.saveStatus === 'error') return <span className="tm-save is-err">Не удалось сохранить расположение</span>;
  return null;
}

export default function MapCanvas({ graph, onCommit, resolveFocus, colorMode, fitRef }) {
  const { state, dispatch, model } = useMap();
  const rf = useReactFlow();
  const [nodes, setNodes] = useState(graph.nodes);
  const nodesRef = useRef(graph.nodes);
  const physical = state.layer === 'physical';

  useEffect(() => {
    nodesRef.current = graph.nodes;
    setNodes(graph.nodes);
  }, [graph.nodes]);

  const onNodesChange = useCallback((changes) => {
    setNodes((nds) => {
      let next = applyNodeChanges(changes, nds);
      const moves = changes.filter((c) => c.type === 'position' && c.position);
      if (moves.length) {
        const frameMoves = moves.filter((c) => c.id.startsWith('frame:'));
        if (frameMoves.length) {
          // тащим рамку — едут члены группы
          const prev = new Map(nds.map((n) => [n.id, n]));
          const shift = new Map();
          frameMoves.forEach((c) => {
            const p = prev.get(c.id);
            if (!p) return;
            const d = [c.position.x - p.position.x, c.position.y - p.position.y];
            (model.groupsById.get(c.id.slice(6))?.members || []).forEach((m) => shift.set(m, d));
          });
          next = next.map((n) => {
            const d = shift.get(n.id);
            return d ? { ...n, position: { x: n.position.x + d[0], y: n.position.y + d[1] } } : n;
          });
        } else if (next.some((n) => n.type === 'frame')) {
          // тащим члена — рамка подстраивается
          const byId = new Map(next.map((n) => [n.id, n]));
          next = next.map((n) => {
            if (n.type !== 'frame') return n;
            const members = (model.groupsById.get(n.data.groupId)?.members || []).filter((m) => byId.has(m));
            const pos = {};
            members.forEach((m) => { const p = byId.get(m).position; pos[m] = [p.x, p.y]; });
            const f = groupFrame(members, pos, logicalSize);
            return f ? { ...n, position: { x: f.x, y: f.y }, width: f.w, height: f.h } : n;
          });
        }
      }
      nodesRef.current = next;
      return next;
    });
  }, [model]);

  const onNodeDragStop = useCallback(() => onCommit(nodesRef.current), [onCommit]);

  const onNodeClick = useCallback((e, node) => {
    switch (node.type) {
      case 'device':
      case 'card':
        dispatch({ type: ACTIONS.SELECT, selection: { type: 'node', id: node.id } });
        break;
      case 'hostgroup':
      case 'physgroup':
        dispatch({ type: ACTIONS.GROUP_TOGGLE, id: node.id });
        break;
      case 'band':
      case 'frame':
        dispatch({ type: ACTIONS.SELECT_CLEAR });
        break;
      default:
    }
  }, [dispatch]);

  const onEdgeClick = useCallback((e, edge) => {
    if (edge.type === 'cable') dispatch({ type: ACTIONS.SELECT, selection: { type: 'cable', id: edge.data.cableId } });
    else if (edge.data.kind === 'bundle') dispatch({ type: ACTIONS.SELECT, selection: { type: 'bundle', id: edge.id } });
    else dispatch({ type: ACTIONS.SELECT, selection: { type: 'link', id: edge.data.linkIds[0] } });
  }, [dispatch]);

  const onPaneClick = useCallback(() => dispatch({ type: ACTIONS.SELECT_CLEAR }), [dispatch]);

  // «Показать всё» после загрузки / смены слоя / раскладки
  // Ждём, пока React Flow измерит узлы устройств (до этого fitView их не
  // видит и вид не вписывается), но не дольше ~0,6 с: служебные узлы
  // (полосы, рамки) могут не измеряться никогда, поэтому useNodesInitialized
  // здесь не годится.
  useEffect(() => {
    if (!state.fitSeq || fitRef.current === state.fitSeq) return undefined;
    let timer;
    let tries = 0;
    const run = () => {
      // Скрытая вкладка (карту открыли в фоне): браузер не измеряет узлы и
      // не крутит анимацию — вписываем, когда пользователь на неё перейдёт.
      if (document.hidden) {
        document.addEventListener('visibilitychange', onVisible);
        return;
      }
      const devs = rf.getNodes().filter((n) => !n.hidden && n.type !== 'band' && n.type !== 'frame');
      const ready = devs.length > 0 && devs.every((n) => n.measured?.width);
      if (!ready && tries++ < 30) { timer = setTimeout(run, 20); return; }
      fitRef.current = state.fitSeq;
      if (physical) fitTop(rf, rf.getNodes());
      else rf.fitView({ padding: FIT_PADDING, duration: 250, maxZoom: 1.2 });
    };
    function onVisible() {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', onVisible);
      tries = 0;
      timer = setTimeout(run, 50);
    }
    timer = setTimeout(run, 0);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, [state.fitSeq, rf]);

  // центрирование на найденном / выбранном из сводки
  useEffect(() => {
    const target = state.focus;
    if (!target) return undefined;
    let tries = 0;
    let raf;
    const run = () => {
      const ids = resolveFocus(target);
      const internals = ids.map((id) => rf.getInternalNode(id)).filter(Boolean);
      if (!internals.length || internals.length < ids.length) {
        tries += 1;
        if (tries < 30) raf = requestAnimationFrame(run);
        return;
      }
      if (internals.length === 1) {
        const n = internals[0];
        const w = n.measured?.width ?? n.width ?? 140;
        const h = n.measured?.height ?? n.height ?? 86;
        const p = n.internals.positionAbsolute;
        rf.setCenter(p.x + w / 2, p.y + Math.min(h, 300) / 2, { zoom: Math.max(rf.getZoom(), 1), duration: 500 });
      } else {
        const b = rf.getNodesBounds(internals.map((n) => n.id));
        const el = document.querySelector('.tm-flow');
        const w = el?.clientWidth || 800;
        const h = el?.clientHeight || 600;
        const zoom = Math.max(0.45, Math.min(1.1, w / (b.width * 1.5 + 80), h / (b.height * 1.5 + 80)));
        rf.setCenter(b.x + b.width / 2, b.y + Math.min(b.height, 600) / 2, { zoom, duration: 500 });
      }
    };
    raf = requestAnimationFrame(run);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.focus?.seq]);

  const draggable = !state.locked && !physical;
  let hint;
  if (physical) hint = 'Расположение — как на логическом слое · остальное — на миникарте';
  else if (state.locked) hint = 'Расположение закреплено';
  else if (state.mode === 'free') hint = 'Перетащите устройство — положение сохранится';
  else hint = 'Раскладка по ролям устройств · перетащите — станет «Свободно»';

  return (
    <ReactFlow
      className="tm-flow"
      nodes={nodes}
      edges={graph.edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeDragStop={onNodeDragStop}
      onNodeClick={onNodeClick}
      onEdgeClick={onEdgeClick}
      onPaneClick={onPaneClick}
      nodesDraggable={draggable}
      nodeDragThreshold={4}
      nodesConnectable={false}
      elementsSelectable={false}
      edgesFocusable={false}
      connectionMode={ConnectionMode.Loose}
      minZoom={0.08}
      maxZoom={2.5}
      zoomOnDoubleClick={false}
      colorMode={colorMode}
      attributionPosition="top-right"
      onlyRenderVisibleElements={false}
      deleteKeyCode={null}
      selectionKeyCode={null}
      multiSelectionKeyCode={null}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
      <Panel position="top-left" className="tm-panel-tl">
        <button
          type="button"
          className={`tm-float-btn${state.legendOpen ? ' is-on' : ''}`}
          aria-expanded={state.legendOpen}
          onClick={() => dispatch({ type: ACTIONS.LEGEND_TOGGLE })}
        >
          <IconLegend />Легенда
        </button>
        {state.legendOpen && <Legend />}
      </Panel>
      <Panel position="bottom-left" className="tm-panel-bl">
        <div className="tm-controls" role="group" aria-label="Масштаб">
          <button type="button" aria-label="Приблизить" title="Приблизить" onClick={() => rf.zoomIn({ duration: 200 })}><IconPlus /></button>
          <button type="button" aria-label="Отдалить" title="Отдалить" onClick={() => rf.zoomOut({ duration: 200 })}><IconMinus /></button>
          <button type="button" aria-label="Показать всё" title="Показать всё" onClick={() => rf.fitView({ padding: FIT_PADDING, duration: 300, maxZoom: 1.2 })}
            onDoubleClick={() => physical && fitTop(rf, rf.getNodes())}><IconFit /></button>
          <button
            type="button"
            aria-label="Закрепить расположение"
            aria-pressed={state.locked}
            title={state.locked ? 'Открепить расположение' : 'Закрепить расположение'}
            className={state.locked ? 'is-on' : ''}
            onClick={() => dispatch({ type: ACTIONS.LOCK_TOGGLE })}
          >
            {state.locked ? <IconLock /> : <IconUnlock />}
          </button>
        </div>
        <span className="tm-hint">{hint} <SaveNote /></span>
      </Panel>
      <MiniMap
        position="bottom-right"
        pannable
        zoomable
        ariaLabel="Миникарта"
        nodeColor={miniColor(model)}
        nodeStrokeWidth={0}
        nodeBorderRadius={2}
        className="tm-minimap"
        style={{ width: 160, height: 104 }}
      />
    </ReactFlow>
  );
}
