import { useRef } from 'react';
import {
  IconActivity, IconConsole, IconCopy, IconGlobe, IconMonitor, IconMore, IconPencil, IconScan, IconTerminal, IconTrash, IconWindow,
} from './icons.jsx';
import { Popover } from './popover.jsx';

// Шлюз удалённого доступа (Guacamole) настроен и доступен пользователю —
// выставляет base.html.
const GATEWAY = !!window.IP_PLAN_GATEWAY;
// В «Настройках» задан служебный ключ — вход без запроса логина и пароля.
const GATEWAY_KEY = !!window.IP_PLAN_GATEWAY_KEY;

/** Пункты меню строки. Подзаголовок «Подключиться» — локальные клиенты
 * (см. connect.js); у этих пунктов в колонке клавиши — порт по умолчанию
 * или тип файла, горячих клавиш у них нет. «Консоль ВМ» — только у адресов,
 * назначенных виртуальной машине (роль vm): файл .vv / ссылка vmrc://.
 * При включённом шлюзе выше стоит раздел «В браузере» (действия web-*:
 * сессия в новой вкладке), а локальные клиенты подписаны как таковые. */
export function rowMenuItems({ isVm }) {
  return [
    { action: 'edit', label: 'Изменить', key: 'E', Icon: IconPencil },
    { action: 'copy', label: 'Копировать адрес', key: '⌘C', Icon: IconCopy },
    { divider: true },
    ...(GATEWAY ? [
      { head: 'В браузере' },
      { action: 'web-ssh', label: 'SSH', key: '22', Icon: IconTerminal },
      ...(GATEWAY_KEY ? [{ action: 'web-ssh-key', label: 'SSH по ключу', key: '22', Icon: IconTerminal }] : []),
      { action: 'web-vnc', label: 'VNC', key: '5900', Icon: IconMonitor },
      { action: 'web-rdp', label: 'RDP', key: '3389', Icon: IconWindow },
      ...(isVm ? [{ action: 'web-console', label: 'Консоль ВМ', key: 'VNC', Icon: IconConsole }] : []),
    ] : []),
    { head: GATEWAY ? 'Локальный клиент' : 'Подключиться' },
    { action: 'ssh', label: 'SSH', key: '22', Icon: IconTerminal },
    { action: 'vnc', label: 'VNC', key: '5900', Icon: IconMonitor },
    { action: 'rdp', label: 'RDP', key: '3389', Icon: IconWindow },
    ...(isVm ? [{ action: 'console', label: 'Консоль ВМ', key: 'SPICE', Icon: IconConsole }] : []),
    { divider: true },
    { action: 'check', label: 'Проверить доступность', key: 'P', Icon: IconActivity },
    { action: 'scan-ports', label: 'Сканировать порты', key: 'S', Icon: IconScan },
    { action: 'resolve-dns', label: 'Резолв DNS', key: 'D', Icon: IconGlobe },
    { divider: true },
    { action: 'delete', label: 'Удалить', key: '⌫', Icon: IconTrash, danger: true },
  ];
}

/** Кебаб строки (⋯): кнопка видна при наведении, открытая — с фоном.
 * Какая строка держит открытое меню — знает TableRoot (нужно для горячих
 * клавиш), поэтому open/onToggle приходят снаружи. */
export function RowMenu({ address, isVm = false, open, onToggle, onAction }) {
  const btnRef = useRef(null);
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={`ipt-kebab ipt-reveal${open ? ' is-on' : ''}`}
        aria-label={`Действия для ${address}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => { e.stopPropagation(); onToggle(!open); }}
      >
        <IconMore size={16} />
      </button>
      <Popover anchorRef={btnRef} open={open} onClose={() => onToggle(false)} align="end" className="ipt-pop-menu" ariaLabel={`Действия для ${address}`}>
        {rowMenuItems({ isVm }).map((item, i) => {
          if (item.divider) return <div key={`d${i}`} className="ipt-sep" />;
          if (item.head) return <div key={`h${i}`} className="ipt-mi-head">{item.head}</div>;
          return (
            <button
              key={item.action}
              type="button"
              role="menuitem"
              className={`ipt-mi${item.danger ? ' is-danger' : ''}`}
              onClick={() => { onToggle(false); onAction(item.action); }}
            >
              <item.Icon size={15} className="ipt-mi-icon" />
              <span className="ipt-mi-label">{item.label}</span>
              <span className="ipt-mi-key ipt-mono">{item.key}</span>
            </button>
          );
        })}
      </Popover>
    </>
  );
}
