// Инлайн-иконки макета (lucide-подобные контуры). Размер и толщина —
// через props, цвет — currentColor.

function Icon({ size = 15, stroke = 1.6, children, ...rest }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconSearch = (p) => <Icon {...p}><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></Icon>;
export const IconPlus = (p) => <Icon stroke={1.75} {...p}><path d="M5 12h14" /><path d="M12 5v14" /></Icon>;
export const IconX = (p) => <Icon stroke={2} {...p}><path d="M18 6 6 18" /><path d="m6 6 12 12" /></Icon>;
export const IconSliders = (p) => (
  <Icon {...p}>
    <line x1="21" x2="14" y1="4" y2="4" /><line x1="10" x2="3" y1="4" y2="4" />
    <line x1="21" x2="12" y1="12" y2="12" /><line x1="8" x2="3" y1="12" y2="12" />
    <line x1="21" x2="16" y1="20" y2="20" /><line x1="12" x2="3" y1="20" y2="20" />
    <line x1="14" x2="14" y1="2" y2="6" /><line x1="8" x2="8" y1="10" y2="14" /><line x1="16" x2="16" y1="18" y2="22" />
  </Icon>
);
export const IconCopy = (p) => <Icon {...p}><rect width="14" height="14" x="8" y="8" rx="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /></Icon>;
export const IconPencil = (p) => <Icon {...p}><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" /></Icon>;
export const IconActivity = (p) => <Icon {...p}><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></Icon>;
export const IconScan = (p) => (
  <Icon {...p}>
    <path d="M3 7V5a2 2 0 0 1 2-2h2" /><path d="M17 3h2a2 2 0 0 1 2 2v2" /><path d="M21 17v2a2 2 0 0 1-2 2h-2" /><path d="M7 21H5a2 2 0 0 1-2-2v-2" />
    <circle cx="12" cy="12" r="3" /><path d="m16 16-1.9-1.9" />
  </Icon>
);
export const IconGlobe = (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" /><path d="M2 12h20" /></Icon>;
export const IconTag = (p) => <Icon {...p}><path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z" /><circle cx="7.5" cy="7.5" r=".5" fill="currentColor" /></Icon>;
export const IconTrash = (p) => <Icon {...p}><path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" /></Icon>;
export const IconChevronLeft = (p) => <Icon {...p}><path d="m15 18-6-6 6-6" /></Icon>;
export const IconChevronRight = (p) => <Icon {...p}><path d="m9 18 6-6-6-6" /></Icon>;
export const IconArrowRight = (p) => <Icon stroke={2} {...p}><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></Icon>;
export const IconArrowUp = (p) => <Icon stroke={2.2} {...p}><path d="m5 12 7-7 7 7" /><path d="M12 19V5" /></Icon>;
export const IconArrowDown = (p) => <Icon stroke={2.2} {...p}><path d="m19 12-7 7-7-7" /><path d="M12 5v14" /></Icon>;
export const IconMore = (p) => <Icon stroke={2} {...p}><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /><circle cx="5" cy="12" r="1" /></Icon>;
export const IconGrip = (p) => (
  <Icon stroke={2} {...p}>
    <circle cx="9" cy="6" r="1" /><circle cx="9" cy="12" r="1" /><circle cx="9" cy="18" r="1" />
    <circle cx="15" cy="6" r="1" /><circle cx="15" cy="12" r="1" /><circle cx="15" cy="18" r="1" />
  </Icon>
);
export const IconLock = (p) => <Icon {...p}><rect width="18" height="11" x="3" y="11" rx="2" ry="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></Icon>;
export const IconReset = (p) => <Icon stroke={1.75} {...p}><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /></Icon>;
export const IconNetwork = (p) => (
  <Icon {...p}>
    <rect x="16" y="16" width="6" height="6" rx="1" /><rect x="2" y="16" width="6" height="6" rx="1" /><rect x="9" y="2" width="6" height="6" rx="1" />
    <path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3" /><path d="M12 12V8" />
  </Icon>
);
export const IconStatus = (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="3" fill="currentColor" /></Icon>;
export const IconRole = (p) => <Icon {...p}><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z" /><path d="m9 12 2 2 4-4" /></Icon>;
export const IconServer = (p) => <Icon {...p}><rect width="20" height="8" x="2" y="2" rx="2" /><rect width="20" height="8" x="2" y="14" rx="2" /><path d="M6 6h.01" /><path d="M6 18h.01" /></Icon>;
export const IconCheck = (p) => <Icon stroke={2.2} {...p}><path d="M20 6 9 17l-5-5" /></Icon>;
export const IconInfo = (p) => <Icon {...p}><circle cx="12" cy="12" r="10" /><path d="M12 16v-4" /><path d="M12 8h.01" /></Icon>;

// Подключение к хосту (кебаб строки → «Подключиться»)
export const IconTerminal = (p) => <Icon {...p}><polyline points="4 17 10 11 4 5" /><line x1="12" x2="20" y1="19" y2="19" /></Icon>;
export const IconMonitor = (p) => <Icon {...p}><rect width="20" height="14" x="2" y="3" rx="2" /><line x1="8" x2="16" y1="21" y2="21" /><line x1="12" x2="12" y1="17" y2="21" /></Icon>;
export const IconWindow = (p) => <Icon {...p}><rect x="2" y="4" width="20" height="16" rx="2" /><path d="M10 4v4" /><path d="M2 8h20" /><path d="M6 4v4" /></Icon>;
export const IconConsole = (p) => <Icon {...p}><rect width="20" height="15" x="2" y="7" rx="2" ry="2" /><polyline points="17 2 12 7 7 2" /></Icon>;
