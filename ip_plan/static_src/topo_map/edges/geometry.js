// Геометрия рёбер логического слоя (плавающие рёбра: концы считаются от
// положения узлов, а не от разъёмов). Правила из макета: узлы одной полосы —
// прямая между плитками; разные полосы — кривая от низа верхнего узла к
// верху плитки нижнего. Параллельные связи одной пары — веером.

import { TILE } from '../constants.js';

function box(node) {
  const p = node.internals.positionAbsolute;
  const w = node.measured?.width ?? node.width ?? 140;
  const h = node.measured?.height ?? node.height ?? 86;
  const cx = p.x + w / 2;
  return { x: p.x, y: p.y, w, h, cx, tileTop: p.y, tileCy: p.y + TILE / 2, bottom: p.y + h };
}

const bezierMid = (p0, p1, p2, p3) => [
  (p0[0] + 3 * p1[0] + 3 * p2[0] + p3[0]) / 8,
  (p0[1] + 3 * p1[1] + 3 * p2[1] + p3[1]) / 8,
];

export function linkGeometry(sNode, tNode, parallel, self, offS = 0, offT = 0) {
  const S = box(sNode);
  const T = box(tNode);
  const off = (parallel.i - (parallel.n - 1) / 2) * 12;

  if (self) {
    const x = S.cx + TILE / 2;
    const y = S.tileCy;
    const r = 52;
    return {
      path: `M ${x} ${y - 9} C ${x + r} ${y - 44}, ${x + r} ${y + 30}, ${x} ${y + 9}`,
      mid: [x + r * 0.78, y - 4],
      aFirst: true,
    };
  }

  if (Math.abs(S.tileCy - T.tileCy) < 40) {
    const aFirst = S.cx <= T.cx;
    const [L, R] = aFirst ? [S, T] : [T, S];
    const dx = R.cx - L.cx;
    if (dx < 260) {
      const y = L.tileCy + off;
      const sx = L.cx + TILE / 2 + 4;
      const ex = R.cx - TILE / 2 - 4;
      return { path: `M ${sx} ${y} L ${ex} ${y}`, mid: [(sx + ex) / 2, y], aFirst };
    }
    // далеко по одной полосе — дугой над плитками, чтобы не пересекать соседей
    const lift = Math.min(110, 34 + dx * 0.08) + Math.abs(off) * 2;
    const p0 = [L.cx + off, L.tileTop - 3];
    const p3 = [R.cx + off, R.tileTop - 3];
    const p1 = [p0[0] + 10, p0[1] - lift];
    const p2 = [p3[0] - 10, p3[1] - lift];
    return {
      path: `M ${p0[0]} ${p0[1]} C ${p1[0]} ${p1[1]}, ${p2[0]} ${p2[1]}, ${p3[0]} ${p3[1]}`,
      mid: bezierMid(p0, p1, p2, p3),
      aFirst,
    };
  }

  const aFirst = S.tileCy <= T.tileCy;
  const [U, D] = aFirst ? [S, T] : [T, S];
  const [offU, offD] = aFirst ? [offS, offT] : [offT, offS];
  const spread = offS !== 0 || offT !== 0;
  const p0 = [U.cx + (spread ? offU : off), U.bottom + 2];
  const p3 = [D.cx + (spread ? offD : off), D.tileTop - 4];
  const dy = Math.max(30, (p3[1] - p0[1]) / 2);
  const p1 = [p0[0], p0[1] + dy];
  const p2 = [p3[0], p3[1] - dy];
  return {
    path: `M ${p0[0]} ${p0[1]} C ${p1[0]} ${p1[1]}, ${p2[0]} ${p2[1]}, ${p3[0]} ${p3[1]}`,
    mid: bezierMid(p0, p1, p2, p3),
    aFirst,
  };
}

// Кабель физического слоя: от разъёма к разъёму, горизонтальные касательные;
// оба разъёма с одной стороны — петля наружу с нарастающим выносом.
export function cableGeometry(sx, sy, sPos, tx, ty, tPos, bulge) {
  const dirS = sPos === 'right' ? 1 : -1;
  const dirT = tPos === 'right' ? 1 : -1;
  let p1; let p2;
  if (sPos === tPos) {
    const bx = 34 + 14 * Math.max(0, bulge - 1) + Math.min(80, Math.abs(ty - sy) * 0.08);
    p1 = [sx + dirS * bx, sy];
    p2 = [tx + dirT * bx, ty];
  } else {
    const d = Math.max(50, Math.abs(tx - sx) / 2);
    p1 = [sx + dirS * d, sy];
    p2 = [tx + dirT * d, ty];
  }
  const p0 = [sx, sy];
  const p3 = [tx, ty];
  return {
    path: `M ${sx} ${sy} C ${p1[0]} ${p1[1]}, ${p2[0]} ${p2[1]}, ${tx} ${ty}`,
    mid: bezierMid(p0, p1, p2, p3),
  };
}
