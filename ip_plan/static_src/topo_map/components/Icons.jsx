// 16 иконок оборудования из макета (Ico.tpl.html): сетка 24 px,
// stroke=currentColor, толщина — sw. Плюс мелкие иконки интерфейса.

const KIND_PATHS = {
  router: (
    <>
      <circle cx="12" cy="12" r="9.25" /><path d="M12 5v4" /><path d="m10.25 7.5 1.75 1.75 1.75-1.75" />
      <path d="M12 19v-4" /><path d="m10.25 16.5 1.75-1.75 1.75 1.75" /><path d="M9 12H5" />
      <path d="M6.5 10.25 4.75 12l1.75 1.75" /><path d="M15 12h4" /><path d="M17.5 10.25 19.25 12l-1.75 1.75" />
    </>
  ),
  core: (
    <>
      <rect x="2.5" y="5.5" width="19" height="13" rx="2.5" /><path d="M6.5 10h10.5" /><path d="m15 8 2 2-2 2" />
      <path d="M17.5 14H7" /><path d="m9 12-2 2 2 2" />
    </>
  ),
  access: (
    <>
      <rect x="2" y="7" width="20" height="10" rx="2" /><path d="M5.5 11.5h2v1.5h-2z" /><path d="M9.5 11.5h2v1.5h-2z" />
      <path d="M13.5 11.5h2v1.5h-2z" /><path d="M18.5 12h.01" />
    </>
  ),
  firewall: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9.33h18" /><path d="M3 14.67h18" />
      <path d="M9 4v5.33" /><path d="M15 4v5.33" /><path d="M6 9.33v5.34" /><path d="M12 9.33v5.34" />
      <path d="M18 9.33v5.34" /><path d="M9 14.67V20" /><path d="M15 14.67V20" />
    </>
  ),
  crypto: (
    <>
      <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
      <circle cx="12" cy="10.5" r="2" /><path d="M12 12.5V16" />
    </>
  ),
  tls: (
    <>
      <rect x="4" y="10.5" width="16" height="10.5" rx="2" /><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
      <path d="M9 15.75h6" /><path d="m13 13.75 2 2-2 2" />
    </>
  ),
  hypervisor: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="2.5" /><rect x="6.75" y="6.75" width="4" height="4" rx="1" />
      <rect x="13.25" y="6.75" width="4" height="4" rx="1" /><rect x="6.75" y="13.25" width="4" height="4" rx="1" />
      <rect x="13.25" y="13.25" width="4" height="4" rx="1" strokeDasharray="2 1.6" />
    </>
  ),
  server: (
    <>
      <rect x="3" y="3.5" width="18" height="7" rx="2" /><rect x="3" y="13.5" width="18" height="7" rx="2" />
      <path d="M7 7h.01" /><path d="M7 17h.01" /><path d="M11 7h6" /><path d="M11 17h6" />
    </>
  ),
  vm: (
    <>
      <rect x="3.5" y="3.5" width="17" height="17" rx="3" strokeDasharray="3.2 2.4" /><rect x="8" y="8" width="8" height="8" rx="1.5" />
    </>
  ),
  storage: (
    <>
      <ellipse cx="12" cy="5" rx="8" ry="2.75" /><path d="M4 5v14c0 1.52 3.58 2.75 8 2.75s8-1.23 8-2.75V5" />
      <path d="M4 12c0 1.52 3.58 2.75 8 2.75s8-1.23 8-2.75" />
    </>
  ),
  chassis: (
    <>
      <rect x="4" y="2.5" width="16" height="19" rx="2" /><path d="M8 6.5v11" /><path d="M12 6.5v11" /><path d="M16 6.5v11" />
    </>
  ),
  ap: (
    <>
      <path d="M4 16.5c0 1.66 3.58 3 8 3s8-1.34 8-3" /><path d="M4 16.5h16" /><path d="M8.5 10.5a5 5 0 0 1 7 0" />
      <path d="M6 7.5a8.5 8.5 0 0 1 12 0" /><path d="M12 13.25h.01" />
    </>
  ),
  workstation: (
    <>
      <rect x="2.5" y="3.5" width="19" height="13" rx="2" /><path d="M8 20.5h8" /><path d="M12 16.5v4" />
    </>
  ),
  cloud: <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" />,
  power: (
    <>
      <rect x="6" y="3" width="12" height="18" rx="2" /><path d="m12.75 7-2.5 4.5h3.5l-2.5 4.5" />
    </>
  ),
  unknown: (
    <>
      <rect x="3.5" y="3.5" width="17" height="17" rx="3" strokeDasharray="3.2 2.4" />
      <path d="M9.75 9.75a2.25 2.25 0 1 1 3.2 2.04c-.57.27-.95.8-.95 1.43v.53" /><path d="M12 16.5h.01" />
    </>
  ),
};

export function KindIcon({ kind, size = 20, sw = 1.6 }) {
  const body = KIND_PATHS[kind] || KIND_PATHS.unknown;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={sw}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {body}
    </svg>
  );
}

const ui = (children, { size = 15, sw = 1.75 } = {}) => function UiIcon(props) {
  const s = props.size || size;
  return (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={props.sw || sw}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" style={props.style} className={props.className}>
      {children}
    </svg>
  );
};

export const IconSearch = ui(<><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></>, { sw: 1.6 });
export const IconPhys = ui(<><path d="M7 3v5" /><path d="M17 3v5" /><rect x="4" y="8" width="16" height="6" rx="1.5" /><path d="M12 14v7" /></>, { size: 14 });
export const IconLogic = ui(<><path d="M4 9h16" /><path d="M4 15h16" /></>, { size: 14 });
export const IconFilter = ui(<><path d="M3 6h18" /><path d="M7 12h10" /><path d="M10 18h4" /></>, { size: 14 });
export const IconDownload = ui(<><path d="M12 15V3" /><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="m7 10 5 5 5-5" /></>, { sw: 1.6 });
export const IconPlus = ui(<><path d="M5 12h14" /><path d="M12 5v14" /></>);
export const IconMinus = ui(<path d="M5 12h14" />);
export const IconFit = ui(<><path d="M8 3H5a2 2 0 0 0-2 2v3" /><path d="M21 8V5a2 2 0 0 0-2-2h-3" /><path d="M3 16v3a2 2 0 0 0 2 2h3" /><path d="M16 21h3a2 2 0 0 0 2-2v-3" /></>);
export const IconLock = ui(<><rect width="16" height="10" x="4" y="11" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>);
export const IconUnlock = ui(<><rect width="16" height="10" x="4" y="11" rx="2" /><path d="M8 11V7a4 4 0 0 1 7.9-1" /></>);
export const IconClose = ui(<><path d="M18 6 6 18" /><path d="m6 6 12 12" /></>);
export const IconArrow = ui(<><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></>, { size: 14 });
export const IconExternal = ui(<><path d="M7 17 17 7" /><path d="M7 7h10v10" /></>, { size: 12, sw: 2 });
export const IconChevron = ui(<path d="m6 9 6 6 6-6" />, { size: 14 });
export const IconChevronRight = ui(<path d="m9 6 6 6-6 6" />, { size: 14, sw: 2 });
export const IconPin = ui(<><path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0" /><circle cx="12" cy="10" r="3" /></>, { size: 14, sw: 1.7 });
export const IconView = ui(<><path d="M20 7h-9" /><path d="M14 17H5" /><circle cx="17" cy="17" r="3" /><circle cx="7" cy="7" r="3" /></>, { sw: 1.7 });
export const IconLegend = ui(<><path d="M3 5h4" /><path d="M3 12h4" /><path d="M3 19h4" /><path d="M11 5h10" /><path d="M11 12h10" /><path d="M11 19h10" /></>, { size: 14 });
export const IconCollapse = ui(<><path d="m7 20 5-5 5 5" /><path d="m7 4 5 5 5-5" /></>, { size: 13, sw: 2 });
export const IconWarn = ui(<><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" /><path d="M12 9v4" /><path d="M12 17h.01" /></>, { size: 14, sw: 1.8 });
export const IconCheck = ui(<path d="M20 6 9 17l-5-5" />, { size: 12, sw: 2.5 });
export const IconRefresh = ui(<><path d="M3 12a9 9 0 0 1 15-6.7L21 8" /><path d="M21 3v5h-5" /><path d="M21 12a9 9 0 0 1-15 6.7L3 16" /><path d="M8 16H3v5" /></>, { size: 14 });
