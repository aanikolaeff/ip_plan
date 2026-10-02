import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@xyflow/react/dist/style.css';
import MapApp from './MapApp.jsx';
import './topo.css';

function readBoot() {
  const el = document.getElementById('topo-map-boot');
  let boot = {};
  try {
    boot = el ? JSON.parse(el.textContent || '{}') : {};
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error('topo-map-boot: неверный JSON', e);
  }
  const prefix = window.URL_PREFIX || '';
  return {
    site: boot.site || '*',
    sites: Array.isArray(boot.sites) ? boot.sites : [],
    dataUrl: boot.dataUrl || `${prefix}/map/data.json`,
    layoutUrl: boot.layoutUrl || `${prefix}/map/layout.json`,
    user: boot.user || '',
  };
}

const container = document.getElementById('topo-map-root');
if (container) {
  createRoot(container).render(
    <StrictMode>
      <MapApp boot={readBoot()} />
    </StrictMode>,
  );
} else {
  // eslint-disable-next-line no-console
  console.error('#topo-map-root не найден — карта сети не смонтирована');
}
