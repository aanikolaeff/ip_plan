import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Собирает несколько входов (по одному на React-island) в
// ip_plan/static/ip_plan/dist/ — оттуда Django collectstatic подхватывает
// файлы как обычную статику, без django-vite (см.
// docs/plans/2026-09-03-ip-table-react-design.md). Django не рендерит
// index.html Vite — appType:'custom' отключает его SPA fallback (иначе
// дев-сервер ждал бы index.html, которого нет).
//
// rollupOptions.input — объект {имя: путь}, а не одна строка: manifest.json
// при этом всё равно ключуется по пути к исходнику (проверено на живой
// сборке), так что vite_asset(entry) в шаблонах ничего не знает про это имя
// и не меняется — оно нужно только Rollup'у для именования чанков.
export default defineConfig(({ command }) => ({
  root: __dirname,
  appType: 'custom',
  plugins: [react()],
  build: {
    outDir: 'ip_plan/static/ip_plan/dist',
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        ip_table: 'ip_plan/static_src/ip_table/main.jsx',
        regulator_table: 'ip_plan/static_src/regulator_table/main.jsx',
      },
    },
  },
  server: {
    // Порт дев-сервера Vite (HMR) — не путать с 8090 (сам Django/gunicorn).
    port: 5173,
    strictPort: true,
    // Разрешает Django-шаблону (другой origin — localhost:8090) грузить
    // модули с dev-сервера по <script type="module" src="http://localhost:5173/...">.
    cors: true,
  },
}));
