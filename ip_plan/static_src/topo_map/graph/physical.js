// Физический слой: карточки устройств со списком портов, у которых есть
// кабель (члены агрегата — под заголовком агрегата), и кабели порт-в-порт.
// Члены агрегата без кабеля (links[].lag_gaps) показываются жёлтой строкой
// «нет кабеля». Свёрнутая группа — карточка без портов, её кабели приходят
// в общий разъём карточки.

import {
  CARD_W, CARD_HEAD, CARD_PAD, PORT_ROW, LAG_ROW,
} from '../constants.js';
import { natCmp } from '../format.js';
import { groupFrame } from '../layout/positions.js';
import { matchesQuery } from './logical.js';

const GROUP_CARD_H = CARD_HEAD + 34;

const portKey = (end) => String(end.interface_id ?? end.interface);

/** Строки портов каждой карточки + размеры — нужны раскладке до построения рёбер. */
export function buildCards(model) {
  const cards = new Map();
  model.nodes.forEach((n) => {
    const ifaces = new Map();
    const add = (key, row) => {
      if (!ifaces.has(key)) ifaces.set(key, { ...row, cables: [] });
      return ifaces.get(key);
    };
    (model.cablesByNode.get(n.id) || []).forEach((cid) => {
      const c = model.cablesById.get(cid);
      [['a', 'b'], ['b', 'a']].forEach(([me, peer]) => {
        const end = c[me];
        if (end.node !== n.id) return;
        if (me === 'b' && c.a.node === c.b.node && portKey(c.a) === portKey(c.b)) return;
        const row = add(portKey(end), { key: portKey(end), name: end.interface || '—', lag: end.lag || null, type: end.type });
        const other = c[peer];
        row.cables.push({ id: c.id, peerNode: other.node, peerName: model.nodesById.get(other.node)?.name || '?', peerPort: other.interface || '' });
      });
    });
    // члены агрегатов без кабеля
    (model.linksByNode.get(n.id) || []).forEach((lid) => {
      const l = model.linksById.get(lid);
      l.lag_gaps.forEach((g) => {
        if (g.node !== n.id) return;
        (g.without_cable || []).forEach((port) => {
          const key = `gap:${g.lag}:${port}`;
          if (![...ifaces.values()].some((r) => r.name === port)) add(key, { key, name: port, lag: g.lag, missing: true });
        });
      });
    });

    const lagMap = new Map();
    const singles = [];
    ifaces.forEach((r) => {
      if (r.lag) {
        if (!lagMap.has(r.lag)) lagMap.set(r.lag, []);
        lagMap.get(r.lag).push(r);
      } else singles.push(r);
    });
    const blocks = [];
    singles.forEach((r) => blocks.push({ sortKey: r.name, rows: [r] }));
    lagMap.forEach((rows, lag) => {
      rows.sort((a, b) => natCmp(a.name, b.name));
      blocks.push({ sortKey: rows[0].name, lag, rows });
    });
    blocks.sort((a, b) => natCmp(a.sortKey, b.sortKey));

    const rows = [];
    blocks.forEach((b) => {
      if (b.lag) {
        const cableRows = b.rows.filter((r) => !r.missing);
        const missing = b.rows.length - cableRows.length;
        const srcs = new Set();
        cableRows.forEach((r) => r.cables.forEach((c) => srcs.add(model.cablesById.get(c.id)?.source)));
        const note = [...srcs].filter(Boolean).map((k) => model.sourcesByKey.get(k)?.label || k).join(', ');
        rows.push({ type: 'lag', key: `lag:${b.lag}`, name: b.lag, note, partial: missing ? `${cableRows.length} из ${b.rows.length}` : null });
        b.rows.forEach((r) => rows.push({ ...r, type: 'port', inLag: true }));
      } else {
        rows.push({ ...b.rows[0], type: 'port', inLag: false });
      }
    });
    let h = CARD_HEAD + CARD_PAD * 2;
    rows.forEach((r) => { h += r.type === 'lag' ? LAG_ROW : PORT_ROW; });
    if (!rows.length) h += PORT_ROW;
    cards.set(n.id, { rows, w: CARD_W, h });
  });
  model.groups.forEach((g) => cards.set(g.id, { rows: [], w: CARD_W, h: GROUP_CARD_H }));
  return cards;
}

export function buildPhysicalGraph({ model, vis, pos, cards, state, query }) {
  const { selection } = state;
  const hidden = new Set(state.hiddenSources);
  const q = (query || '').trim().toLowerCase();
  const searching = q.length >= 2;
  const sizeOf = (id) => {
    const c = cards.get(id);
    return c ? [c.w, c.h] : [CARD_W, CARD_HEAD + 30];
  };
  const center = (id) => {
    const p = pos[id] || [0, 0];
    const [w, h] = sizeOf(id);
    return [p[0] + w / 2, p[1] + h / 2];
  };

  // какие кабели подсвечены
  const litCables = new Set();
  let anySel = false;
  if (selection?.type === 'cable') { litCables.add(selection.id); anySel = true; }
  if (selection?.type === 'link') {
    (model.linksById.get(selection.id)?.cables || []).forEach((id) => litCables.add(id));
    anySel = true;
  }
  if (selection?.type === 'bundle') {
    // b:<a>|<b> — все кабели между двумя представителями
    anySel = true;
  }
  if (selection?.type === 'node') {
    (model.cablesByNode.get(selection.id) || []).forEach((id) => litCables.add(id));
    anySel = true;
  }

  const usedSide = new Map(); // nodeId -> Map(portKey -> 'L'|'R')
  const mark = (node, key, side) => {
    if (!usedSide.has(node)) usedSide.set(node, new Map());
    usedSide.get(node).set(key, side);
  };
  const litNodes = new Set();
  const litPorts = new Set(); // `${node}|${key}`
  const loops = new Map();
  const edges = [];

  model.cables.forEach((c) => {
    if (hidden.has(c.source)) return;
    const ra = vis.repOf(c.a.node);
    const rb = vis.repOf(c.b.node);
    if (!ra || !rb) return;
    const aGroup = model.groupsById.has(ra);
    const bGroup = model.groupsById.has(rb);
    if (ra === rb && aGroup) return;
    if (selection?.type === 'bundle') {
      const [s, t] = selection.id.slice(2).split('|');
      if ((ra === s && rb === t) || (ra === t && rb === s)) litCables.add(c.id);
    }
    const [ax] = center(ra);
    const [bx] = center(rb);
    let sa; let sb;
    if (ra === rb || Math.abs(ax - bx) < 60) { sa = 'R'; sb = 'R'; } else if (bx > ax) { sa = 'R'; sb = 'L'; } else { sa = 'L'; sb = 'R'; }
    const ka = aGroup ? 'g' : portKey(c.a);
    const kb = bGroup ? 'g' : portKey(c.b);
    if (!aGroup) mark(ra, ka, sa);
    if (!bGroup) mark(rb, kb, sb);
    const lit = litCables.has(c.id);
    if (lit) {
      litNodes.add(ra); litNodes.add(rb);
      litPorts.add(`${ra}|${ka}`); litPorts.add(`${rb}|${kb}`);
    }
    let bulge = 0;
    if (sa === sb) {
      const pk = ra <= rb ? `${ra}|${rb}` : `${rb}|${ra}`;
      bulge = (loops.get(pk) || 0) + 1;
      loops.set(pk, bulge);
    }
    edges.push({
      id: `c${c.id}`,
      source: ra,
      target: rb,
      sourceHandle: `${ka}:${sa}`,
      targetHandle: `${kb}:${sb}`,
      type: 'cable',
      data: {
        cableId: c.id,
        source: c.source,
        state: c.state || 'confirmed',
        owner: c.owner || 'ours',
        lit,
        dim: anySel && !lit,
        bulge,
      },
    });
  });

  edges.sort((a, b) => Number(a.data.lit) - Number(b.data.lit));
  const nodes = [];
  vis.expanded.forEach((g) => {
    const members = vis.groupMembers.get(g.id);
    const f = groupFrame(members, pos, sizeOf);
    if (!f) return;
    nodes.push({
      id: `frame:${g.id}`,
      type: 'frame',
      position: { x: f.x, y: f.y },
      width: f.w,
      height: f.h,
      selectable: false,
      focusable: false,
      draggable: false,
      zIndex: -1,
      data: { groupId: g.id, count: members.length, dim: anySel && !members.some((m) => litNodes.has(m)) },
    });
  });
  vis.collapsed.forEach((g) => {
    const p = pos[g.id] || [0, 0];
    const members = vis.groupMembers.get(g.id);
    nodes.push({
      id: g.id,
      type: 'physgroup',
      position: { x: p[0], y: p[1] },
      width: CARD_W,
      height: GROUP_CARD_H,
      zIndex: 1,
      data: {
        groupId: g.id,
        count: members.length,
        dim: anySel && !litNodes.has(g.id),
        lit: litNodes.has(g.id),
        match: searching && members.some((m) => matchesQuery(model.nodesById.get(m), q)),
      },
    });
  });
  vis.devices.forEach((n) => {
    const p = pos[n.id] || [0, 0];
    const card = cards.get(n.id);
    const sides = usedSide.get(n.id);
    nodes.push({
      id: n.id,
      type: 'card',
      ariaLabel: n.name,
      position: { x: p[0], y: p[1] },
      width: card.w,
      height: card.h,
      zIndex: 1,
      data: {
        nodeId: n.id,
        rows: card.rows,
        sides: sides ? Object.fromEntries(sides) : {},
        litPorts: card.rows.filter((r) => litPorts.has(`${n.id}|${r.key}`)).map((r) => r.key),
        selected: selection?.type === 'node' && selection.id === n.id,
        lit: litNodes.has(n.id),
        dim: anySel && !litNodes.has(n.id) && !(selection?.type === 'node' && selection.id === n.id),
        match: searching && matchesQuery(n, q),
        otherSite: model.site.slug !== '*' && n.site && n.site !== model.site.slug ? (n.site_name || n.site) : null,
      },
    });
  });
  return { nodes, edges };
}
