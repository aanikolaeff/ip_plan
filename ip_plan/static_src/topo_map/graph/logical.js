// Узлы и рёбра React Flow для логического слоя.
//
// Одна связь — одно ребро. Связи членов свёрнутой группы с внешним узлом
// собираются в одно ребро-связку (состояние — худшее из членов), связи
// внутри свёрнутой группы не рисуются. Петли (стек, кабель между портами
// одного устройства) — одна петля на устройство.

import { DEV_W, DEV_H, worstState } from '../constants.js';
import { endShort } from '../format.js';
import { groupFrame, logicalSize } from '../layout/positions.js';

export function matchesQuery(node, q) {
  if (!q) return false;
  return node.name.toLowerCase().includes(q) || (!!node.ip && node.ip.toLowerCase().startsWith(q));
}

function dominant(counts) {
  let best = null;
  Object.entries(counts).forEach(([k, v]) => { if (!best || v > counts[best]) best = k; });
  return best;
}

/** Рёбра логического слоя без подсветки — общая часть для полотна и панели. */
export function buildLogicalEdges(model, vis, hiddenSources) {
  const singles = [];
  const bundles = new Map();
  const edgeOfLink = new Map();
  model.links.forEach((l) => {
    if (hiddenSources.has(l.source)) return;
    const ra = vis.repOf(l.a.node);
    const rb = vis.repOf(l.b.node);
    if (!ra || !rb) return;
    if (ra === rb && !l._self) return; // внутри свёрнутой группы
    const aGroup = model.groupsById.has(ra);
    const bGroup = model.groupsById.has(rb);
    if (l._self || aGroup || bGroup) {
      const [s, t] = ra <= rb ? [ra, rb] : [rb, ra];
      const key = `b:${s}|${t}`;
      if (!bundles.has(key)) bundles.set(key, { id: key, source: s, target: t, links: [] });
      bundles.get(key).links.push(l);
      edgeOfLink.set(l.id, key);
      return;
    }
    singles.push({ id: l.id, source: ra, target: rb, links: [l] });
    edgeOfLink.set(l.id, l.id);
  });

  // Связка из одной связи (одиночная петля, единственная связь группы) — обычное ребро.
  const edges = [...singles];
  bundles.forEach((b) => {
    if (b.links.length === 1) {
      edges.push({ ...b, id: b.links[0].id, single: true });
      edgeOfLink.set(b.links[0].id, b.links[0].id);
    } else {
      edges.push(b);
    }
  });

  // Параллельные рёбра одной пары — веером.
  const pairs = new Map();
  edges.forEach((e) => {
    const key = e.source <= e.target ? `${e.source}|${e.target}` : `${e.target}|${e.source}`;
    if (!pairs.has(key)) pairs.set(key, []);
    pairs.get(key).push(e);
  });
  pairs.forEach((list) => list.forEach((e, i) => { e.parallel = { i, n: list.length }; }));

  return { edges, edgeOfLink };
}

function edgeView(e, model) {
  const links = e.links;
  const isBundle = !e.single && (links.length > 1 || e.id.startsWith('b:'));
  const l0 = links[0];
  const sourceCounts = {};
  links.forEach((l) => { sourceCounts[l.source] = (sourceCounts[l.source] || 0) + 1; });
  const owners = links.map((l) => l._owner);
  const partial = !isBundle ? l0._partial : (links.some((l) => l._partial) ? { text: '', hint: 'В связке есть неполный агрегат' } : null);
  return {
    kind: isBundle ? 'bundle' : 'link',
    linkIds: links.map((l) => l.id),
    source: isBundle ? dominant(sourceCounts) : l0.source,
    lag: !isBundle && l0._lag && !l0._self,
    members: !isBundle ? (l0.members_proven || l0._cables.length || 1) : 0,
    count: links.length,
    cableCount: links.reduce((s, l) => s + (l._cables.length || 1), 0),
    state: worstState(links.map((l) => (l.state === 'partial' ? 'confirmed' : l.state))),
    owner: owners.includes('manual') ? 'manual' : (owners.includes('edited') ? 'edited' : 'ours'),
    partial,
    self: e.source === e.target,
    parallel: e.parallel || { i: 0, n: 1 },
    ends: !isBundle ? { a: l0.a, b: l0.b } : null,
    endsNames: !isBundle ? {
      a: model.nodesById.get(l0.a.node)?.name,
      b: model.nodesById.get(l0.b.node)?.name,
    } : null,
  };
}

// Концы рёбер между полосами разводятся по ширине узла (снизу — под
// подписью, сверху — по плитке) в порядке x соседа: меньше «пучков» в одной
// точке и пересечений у самого узла.
function spreadEnds(edges, pos) {
  const slots = new Map();
  const add = (key, edge, end, otherX) => {
    if (!slots.has(key)) slots.set(key, []);
    slots.get(key).push({ edge, end, otherX });
  };
  edges.forEach((e) => {
    e.data.offS = 0;
    e.data.offT = 0;
    if (e.data.self) return;
    const ps = pos[e.source];
    const pt = pos[e.target];
    if (!ps || !pt || Math.abs(ps[1] - pt[1]) < 40) return;
    const sUpper = ps[1] < pt[1];
    add(`${e.source}|${sUpper ? 'b' : 't'}`, e, 'offS', pt[0]);
    add(`${e.target}|${sUpper ? 't' : 'b'}`, e, 'offT', ps[0]);
  });
  slots.forEach((list, key) => {
    if (list.length < 2) return;
    const bottom = key.endsWith('|b');
    const half = bottom ? 46 : 17;
    const step = Math.min(bottom ? 10 : 7, (half * 2) / (list.length - 1));
    list.sort((a, b) => a.otherX - b.otherX || (a.edge.id < b.edge.id ? -1 : 1));
    const start = -(step * (list.length - 1)) / 2;
    list.forEach((s, i) => { s.edge.data[s.end] = start + i * step; });
  });
}

export function buildLogicalGraph({ model, vis, pos, roles, mode, state, query, built }) {
  const { selection } = state;
  const { edges: rawEdges, edgeOfLink } = built;
  const q = (query || '').trim().toLowerCase();
  const searching = q.length >= 2;

  // ---- подсветка выбора ----
  const litEdges = new Set();
  const litNodes = new Set();
  let anySel = false;
  if (selection) {
    if (selection.type === 'node') {
      const rep = vis.repOf(selection.id);
      if (rep) {
        anySel = true;
        litNodes.add(rep);
        (model.linksByNode.get(selection.id) || []).forEach((lid) => {
          const eid = edgeOfLink.get(lid);
          if (eid) litEdges.add(eid);
        });
      }
    } else if (selection.type === 'link' || selection.type === 'cable') {
      const lid = selection.type === 'link' ? selection.id : model.cablesById.get(selection.id)?.link_id;
      const eid = edgeOfLink.get(lid);
      if (eid) { anySel = true; litEdges.add(eid); }
    } else if (selection.type === 'bundle') {
      anySel = true;
      litEdges.add(selection.id);
    }
    rawEdges.forEach((e) => {
      if (litEdges.has(e.id)) { litNodes.add(e.source); litNodes.add(e.target); }
    });
  }

  const selectedRep = selection?.type === 'node' ? vis.repOf(selection.id) : null;
  const multiSite = state.site === '*' || model.site.slug === '*';
  const nodes = [];

  // полосы ролей (только «По ролям») — фоном
  if (mode !== 'free' && roles.bands.length) {
    const { minX, maxX } = roles.bbox;
    roles.bands.forEach((b, i) => {
      nodes.push({
        id: `band:${b.index}`,
        type: 'band',
        position: { x: minX - 170, y: b.top },
        width: maxX - minX + 340,
        height: b.height,
        draggable: false,
        selectable: false,
        focusable: false,
        zIndex: -2,
        data: { label: b.label, first: i === 0 },
      });
    });
  }

  // рамки развёрнутых групп
  vis.expanded.forEach((g) => {
    const members = vis.groupMembers.get(g.id);
    const f = groupFrame(members, pos, logicalSize);
    if (!f) return;
    const memberMatch = searching && members.some((m) => matchesQuery(model.nodesById.get(m), q));
    nodes.push({
      id: `frame:${g.id}`,
      type: 'frame',
      position: { x: f.x, y: f.y },
      width: f.w,
      height: f.h,
      selectable: false,
      focusable: false,
      zIndex: -1,
      data: { groupId: g.id, count: members.length, dim: anySel && !members.some((m) => litNodes.has(m)), match: memberMatch },
    });
  });

  vis.collapsed.forEach((g) => {
    const p = pos[g.id] || [0, 0];
    const members = vis.groupMembers.get(g.id);
    nodes.push({
      id: g.id,
      type: 'hostgroup',
      ariaLabel: `Группа ${g.title}, ${members.length} — развернуть`,
      position: { x: p[0], y: p[1] },
      width: DEV_W,
      height: DEV_H,
      zIndex: 1,
      data: {
        groupId: g.id,
        count: members.length,
        selected: selectedRep === g.id,
        dim: anySel && !litNodes.has(g.id),
        match: searching && members.some((m) => matchesQuery(model.nodesById.get(m), q)),
      },
    });
  });

  vis.devices.forEach((n) => {
    const p = pos[n.id] || [0, 0];
    nodes.push({
      id: n.id,
      type: 'device',
      ariaLabel: n.ip ? `${n.name}, ${n.ip}` : n.name,
      position: { x: p[0], y: p[1] },
      width: DEV_W,
      height: DEV_H,
      zIndex: 1,
      data: {
        nodeId: n.id,
        selected: selection?.type === 'node' && selection.id === n.id,
        dim: anySel && !litNodes.has(n.id),
        match: searching && matchesQuery(n, q),
        otherSite: !multiSite && n.site && n.site !== model.site.slug ? (n.site_name || n.site) : null,
      },
    });
  });

  const edges = rawEdges.map((e) => {
    const view = edgeView(e, model);
    const lit = litEdges.has(e.id);
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      type: 'link',
      data: { ...view, lit, dim: anySel && !lit },
    };
  });
  spreadEnds(edges, pos);
  // подсвеченные — последними, чтобы лежали поверх остальных линий
  edges.sort((a, b) => Number(a.data.lit) - Number(b.data.lit));

  return { nodes, edges };
}
