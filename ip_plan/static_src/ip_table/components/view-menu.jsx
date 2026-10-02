import { useState } from 'react';
import { DENSITIES } from '../constants.js';
import { IconGrip, IconLock, IconReset } from './icons.jsx';
import { Popover } from './popover.jsx';

function Switch({ checked, onChange, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={`ipt-switch${checked ? ' is-on' : ''}`}
      onClick={onChange}
    >
      <span className="ipt-switch-knob" />
    </button>
  );
}

/** Меню «Вид»: плотность, видимость и порядок колонок (перетаскивание),
 * свободные диапазоны, сброс. columns — список переключаемых колонок уже
 * в текущем порядке: [{id, label, scan, visible}]. */
export function ViewMenu({
  anchorRef, open, onClose,
  density, onDensity,
  columns, onToggleColumn, onReorder,
  showGaps, onShowGaps, gapsAvailable,
  connect, onConnect,
  onReset,
}) {
  const [dragId, setDragId] = useState(null);
  const [overId, setOverId] = useState(null);

  const visible = columns.filter((c) => c.visible);
  const hidden = columns.filter((c) => !c.visible);
  const total = columns.length + 1; // + «Адрес»

  function drop(targetId) {
    if (!dragId || dragId === targetId) { setDragId(null); setOverId(null); return; }
    const order = columns.map((c) => c.id);
    const from = order.indexOf(dragId);
    const to = order.indexOf(targetId);
    if (from < 0 || to < 0) return;
    order.splice(to, 0, order.splice(from, 1)[0]);
    onReorder(order);
    setDragId(null);
    setOverId(null);
  }

  const renderCol = (c, draggable) => (
    <div
      key={c.id}
      className={`ipt-vm-row${overId === c.id && dragId !== c.id ? ' is-over' : ''}${dragId === c.id ? ' is-drag' : ''}`}
      draggable={draggable}
      onDragStart={draggable ? (e) => { setDragId(c.id); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', c.id); } catch (err) { /* IE */ } } : undefined}
      onDragOver={draggable ? (e) => { e.preventDefault(); if (overId !== c.id) setOverId(c.id); } : undefined}
      onDragLeave={draggable ? () => { if (overId === c.id) setOverId(null); } : undefined}
      onDrop={draggable ? (e) => { e.preventDefault(); drop(c.id); } : undefined}
      onDragEnd={() => { setDragId(null); setOverId(null); }}
    >
      <IconGrip size={14} className={`ipt-vm-grip${draggable ? '' : ' is-idle'}`} />
      <span className={`ipt-vm-label${c.visible ? '' : ' ipt-muted'}`}>{c.label}</span>
      {c.scan && <span className="ipt-vm-scan">скан</span>}
      <Switch checked={c.visible} onChange={() => onToggleColumn(c.id)} label={`Показывать колонку «${c.label}»`} />
    </div>
  );

  return (
    <Popover anchorRef={anchorRef} open={open} onClose={onClose} align="end" className="ipt-pop-view" role="dialog" ariaLabel="Настройки вида таблицы">
      <div className="ipt-vm-sec">
        <span className="ipt-sec-title">Плотность строк</span>
        <div className="ipt-seg" role="radiogroup" aria-label="Плотность строк">
          {DENSITIES.map((d) => (
            <button
              key={d.value}
              type="button"
              role="radio"
              aria-checked={density === d.value}
              className={`ipt-seg-btn${density === d.value ? ' is-on' : ''}`}
              onClick={() => onDensity(d.value)}
            >
              <span className="ipt-seg-lines" style={{ gap: d.gap }}><i /><i /><i /></span>
              {d.label}
            </button>
          ))}
        </div>
      </div>

      <div className="ipt-sep-h" />

      <div className="ipt-vm-colhead">
        <span className="ipt-sec-title">Колонки</span>
        <span className="ipt-vm-colnote"><b>{visible.length + 1}</b> из {total} · перетащите, чтобы изменить порядок</span>
      </div>
      <div className="ipt-vm-cols">
        <div className="ipt-vm-row is-locked">
          <IconLock size={14} className="ipt-vm-grip is-idle" />
          <span className="ipt-vm-label ipt-vm-always">Адрес</span>
          <span className="ipt-vm-scan">всегда</span>
        </div>
        {visible.map((c) => renderCol(c, true))}
        {hidden.length > 0 && (
          <div className="ipt-vm-divider">Скрытые<span /></div>
        )}
        {hidden.map((c) => renderCol(c, true))}
      </div>

      <div className="ipt-sep-h" />

      <div className="ipt-vm-sec ipt-vm-sec-tight">
        <div className="ipt-vm-row is-static" title={gapsAvailable ? undefined : 'Диапазоны определены только при одной выбранной подсети'}>
          <span className="ipt-vm-label">Показывать свободные диапазоны</span>
          <Switch checked={showGaps} onChange={() => onShowGaps(!showGaps)} label="Показывать свободные диапазоны" />
        </div>
      </div>

      <div className="ipt-sep-h" />

      {/* Подключение к хостам (кебаб строки → «Подключиться»): имена
          пользователей подставляются в ssh:// и в RDP; пароли не храним. */}
      <div className="ipt-vm-sec">
        <span className="ipt-sec-title">Подключение к хостам</span>
        <div className="ipt-vm-fields">
          <label className="ipt-vm-field">
            <span>Пользователь SSH</span>
            <input className="ipt-vm-input ipt-mono" value={connect?.sshUser || ''} placeholder="root" autoComplete="off" spellCheck={false} onChange={(e) => onConnect?.({ sshUser: e.target.value.trim() })} />
          </label>
          <label className="ipt-vm-field">
            <span>Пользователь RDP</span>
            <input className="ipt-vm-input ipt-mono" value={connect?.rdpUser || ''} placeholder="DOMAIN\user" autoComplete="off" spellCheck={false} onChange={(e) => onConnect?.({ rdpUser: e.target.value.trim() })} />
          </label>
        </div>
        <span className="ipt-vm-hint">SSH и VNC открываются системным клиентом по ссылке, RDP — через Windows App на macOS или файлом .rdp, консоль ВМ — файлом .vv для remote-viewer либо ссылкой VMRC.</span>
      </div>

      <div className="ipt-pop-foot">
        <span>Запоминается в этом браузере</span>
        <button type="button" className="ipt-btn-ghost ipt-btn-sm" onClick={onReset}>
          <IconReset size={13} />
          Сбросить
        </button>
      </div>
    </Popover>
  );
}
