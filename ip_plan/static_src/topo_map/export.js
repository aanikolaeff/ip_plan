// «Скачать как картинку»: PNG всего графа по рецепту документации React Flow
// (html-to-image + getNodesBounds). Масштаб 1:1, ×2 для чёткости, но не
// больше ~12 000 px по длинной стороне (предел canvas в браузерах).

import { toPng } from 'html-to-image';
import { todayStamp } from './format.js';

export async function exportPng(rf, container, site) {
  const nodes = rf.getNodes();
  if (!nodes.length || !container) return;
  const viewport = container.querySelector('.react-flow__viewport');
  if (!viewport) return;
  const b = rf.getNodesBounds(nodes);
  const pad = 48;
  const width = Math.ceil(b.width + pad * 2);
  const height = Math.ceil(b.height + pad * 2);
  const pixelRatio = Math.max(0.5, Math.min(2, 12000 / Math.max(width, height)));
  const bg = getComputedStyle(container).getPropertyValue('--softer').trim() || '#f8f8f7';
  const dataUrl = await toPng(viewport, {
    backgroundColor: bg,
    width,
    height,
    pixelRatio,
    style: {
      width: `${width}px`,
      height: `${height}px`,
      transform: `translate(${pad - b.x}px, ${pad - b.y}px) scale(1)`,
    },
  });
  const a = document.createElement('a');
  a.download = `karta-${site === '*' ? 'vse' : site}-${todayStamp()}.png`;
  a.href = dataUrl;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
