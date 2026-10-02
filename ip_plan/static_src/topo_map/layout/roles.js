// Раскладка «По ролям»: полосы по band (Граница → Серверы), внутри полосы
// порядок по барицентру соседей (несколько проходов сверху вниз и снизу
// вверх, как в слоистой раскладке Сугиямы), без сторонних библиотек.
//
// Единица раскладки — устройство, свёрнутая группа (плитка того же размера)
// или развёрнутая группа (блок-сетка из членов). Слишком длинная полоса
// переносится на несколько рядов, чтобы «Показать всё» оставалось читаемым.

import {
  DEV_W, DEV_H, GAP_X, ROW_GAP, BAND_PAD_TOP, BAND_PAD_BOTTOM, MAX_ROW_UNITS,
  GROUP_HEAD, GROUP_PAD, MEMBER_GAP_X, MEMBER_GAP_Y,
} from '../constants.js';
import { natCmp } from '../format.js';

const PASSES = 3;

export function blockGrid(count) {
  const cols = Math.max(1, Math.min(count, Math.ceil(Math.sqrt(count * 1.8))));
  const rows = Math.ceil(count / cols);
  return {
    cols,
    rows,
    w: GROUP_PAD * 2 + cols * DEV_W + (cols - 1) * MEMBER_GAP_X,
    h: GROUP_HEAD + GROUP_PAD + rows * DEV_H + (rows - 1) * MEMBER_GAP_Y,
  };
}

// Позиции членов в сетке относительно левого верхнего угла блока.
export function gridOffsets(index, cols) {
  const c = index % cols;
  const r = Math.floor(index / cols);
  return [GROUP_PAD + c * (DEV_W + MEMBER_GAP_X), GROUP_HEAD + r * (DEV_H + MEMBER_GAP_Y)];
}

const avg = (arr) => arr.reduce((s, v) => s + v, 0) / arr.length;

/**
 * @param model  модель (state/model.js)
 * @param vis    computeVisibility(...)
 * @param opts   { multiSite: bool } — при «Все площадки» сначала площадка, потом барицентр
 * @returns { pos: {id: [x,y]}, bands: [{index,label,top,height}], bbox }
 */
export function computeRolesLayout(model, vis, opts = {}) {
  const units = [];
  vis.devices.forEach((n) => units.push({ id: n.id, band: n.band, w: DEV_W, h: DEV_H, name: n.name, site: n.site, type: 'device' }));
  vis.collapsed.forEach((g) => {
    const site = model.nodesById.get(g.members[0])?.site;
    units.push({ id: g.id, band: g.band, w: DEV_W, h: DEV_H, name: g.title, site, type: 'group' });
  });
  vis.expanded.forEach((g) => {
    const members = vis.groupMembers.get(g.id);
    const grid = blockGrid(members.length);
    const site = model.nodesById.get(g.members[0])?.site;
    units.push({ id: g.id, band: g.band, w: grid.w, h: grid.h, name: g.title, site, type: 'block', grid, members });
  });
  const unitById = new Map(units.map((u) => [u.id, u]));

  // Соседство единиц (с повторами: несколько связей тянут сильнее).
  const adj = new Map(units.map((u) => [u.id, []]));
  model.links.forEach((l) => {
    if (l._self) return;
    const a = vis.unitOf(l.a.node);
    const b = vis.unitOf(l.b.node);
    if (!a || !b || a === b || !adj.has(a) || !adj.has(b)) return;
    adj.get(a).push(b);
    adj.get(b).push(a);
  });

  const siteOrder = new Map();
  if (opts.multiSite) {
    [...new Set(units.map((u) => u.site || ''))].sort(natCmp).forEach((s, i) => siteOrder.set(s, i));
  }
  const siteRank = (u) => (opts.multiSite ? siteOrder.get(u.site || '') ?? 0 : 0);

  const nBands = 5;
  const byBand = Array.from({ length: nBands }, () => []);
  units.forEach((u) => byBand[u.band].push(u));
  byBand.forEach((list) => list.sort((p, q) => siteRank(p) - siteRank(q) || natCmp(p.name, q.name)));

  const xOf = new Map();
  const assign = (list) => {
    let x = 0;
    list.forEach((u) => { xOf.set(u.id, x + u.w / 2); x += u.w + GAP_X; });
    const shift = (x - GAP_X) / 2;
    list.forEach((u) => xOf.set(u.id, xOf.get(u.id) - shift));
  };
  byBand.forEach(assign);

  const sweep = (b, useBand) => {
    const list = byBand[b];
    if (list.length < 2) return;
    const bc = new Map();
    list.forEach((u) => {
      const ref = adj.get(u.id).filter((v) => useBand(unitById.get(v).band));
      bc.set(u.id, ref.length ? avg(ref.map((v) => xOf.get(v))) : xOf.get(u.id));
    });
    list.sort((p, q) => siteRank(p) - siteRank(q) || bc.get(p.id) - bc.get(q.id) || natCmp(p.name, q.name));
    assign(list);
  };
  for (let it = 0; it < PASSES; it += 1) {
    for (let b = 1; b < nBands; b += 1) sweep(b, (ob) => ob < b);
    for (let b = nBands - 2; b >= 0; b -= 1) sweep(b, (ob) => ob > b);
  }
  // Финальный проход сверху вниз — полосы ниже выравниваются под верхние.
  for (let b = 1; b < nBands; b += 1) sweep(b, (ob) => ob !== b);

  // Перенос длинных полос на ряды.
  const maxRowW = MAX_ROW_UNITS * (DEV_W + GAP_X) - GAP_X;
  const pos = {};
  const bands = [];
  let cursorY = 0;
  let minX = Infinity;
  let maxX = -Infinity;
  for (let b = 0; b < nBands; b += 1) {
    const list = byBand[b];
    if (!list.length) continue;
    const total = list.reduce((s, u) => s + u.w + GAP_X, 0) - GAP_X;
    const nRows = Math.max(1, Math.ceil(total / maxRowW));
    const target = total / nRows;
    const rows = [];
    let row = [];
    let rowW = 0;
    list.forEach((u) => {
      if (row.length && rowW + GAP_X + u.w > target + DEV_W / 2 && rows.length < nRows - 1) {
        rows.push(row);
        row = [];
        rowW = 0;
      }
      rowW += (row.length ? GAP_X : 0) + u.w;
      row.push(u);
    });
    if (row.length) rows.push(row);

    const top = cursorY;
    let y = top + BAND_PAD_TOP;
    rows.forEach((r, ri) => {
      const w = r.reduce((s, u) => s + u.w + GAP_X, 0) - GAP_X;
      let x = -w / 2;
      const h = Math.max(...r.map((u) => u.h));
      r.forEach((u) => {
        pos[u.id] = [x, y];
        minX = Math.min(minX, x);
        maxX = Math.max(maxX, x + u.w);
        x += u.w + GAP_X;
      });
      y += h + (ri < rows.length - 1 ? ROW_GAP : 0);
    });
    const height = y - top + BAND_PAD_BOTTOM;
    bands.push({ index: b, label: model.bands[b] || `Полоса ${b + 1}`, top, height });
    cursorY = top + height;
  }

  // Члены развёрнутых групп — сеткой внутри блока; порядок — по x соседей
  // снаружи блока, чтобы связи меньше перекрещивались.
  units.filter((u) => u.type === 'block').forEach((u) => {
    const [bx, by] = pos[u.id];
    const outerX = (m) => {
      const xs = [];
      (model.linksByNode.get(m) || []).forEach((lid) => {
        const l = model.linksById.get(lid);
        const other = l.a.node === m ? l.b.node : l.a.node;
        const ou = vis.unitOf(other);
        if (ou && ou !== u.id && pos[ou]) xs.push(pos[ou][0]);
      });
      return xs.length ? avg(xs) : Infinity;
    };
    const ordered = [...u.members].sort((p, q) => outerX(p) - outerX(q)
      || natCmp(model.nodesById.get(p).name, model.nodesById.get(q).name));
    ordered.forEach((m, i) => {
      const [ox, oy] = gridOffsets(i, u.grid.cols);
      pos[m] = [bx + ox, by + oy];
    });
  });

  if (!Number.isFinite(minX)) { minX = 0; maxX = 0; }
  return { pos, bands, bbox: { minX, maxX, minY: 0, maxY: cursorY }, blocks: units.filter((u) => u.type === 'block') };
}
