// Simple travel pictograms in the style of transit signage. Drawn inline so they work offline.
export function Pictogram({ mode, size = 56 }: { mode: string; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 48 48", "aria-hidden": true, fill: "currentColor" } as const;
  if (mode === "bus") {
    return (
      <svg {...common}>
        <rect x="9" y="6" width="30" height="31" rx="5" />
        <rect x="13" y="11" width="22" height="11" rx="1.5" fill="var(--band-ink)" />
        <circle cx="16" cy="30" r="2.5" fill="var(--band-ink)" />
        <circle cx="32" cy="30" r="2.5" fill="var(--band-ink)" />
        <rect x="12" y="36" width="5" height="6" rx="1.5" />
        <rect x="31" y="36" width="5" height="6" rx="1.5" />
      </svg>
    );
  }
  if (mode === "train") {
    return (
      <svg {...common}>
        <rect x="11" y="5" width="26" height="31" rx="7" />
        <rect x="15" y="10" width="18" height="10" rx="1.5" fill="var(--band-ink)" />
        <circle cx="18" cy="28" r="2.5" fill="var(--band-ink)" />
        <circle cx="30" cy="28" r="2.5" fill="var(--band-ink)" />
        <path d="M15 43l5-7h2l-4 7zM33 43l-5-7h-2l4 7z" />
      </svg>
    );
  }
  // walk
  return (
    <svg {...common}>
      <circle cx="26" cy="7" r="4.5" />
      <path d="M23 14c2-1 5-1 6.5.6l4.8 6.2 5.6 1.8-1.2 3.6-6.8-2.3-2.4-3-1.6 7.4 5 5.8 1.5 10.4-4 .6-1.3-9-5.2-5.4-3.2 14.8H16.8l4.4-21.6-3.6 1.6-2.6 6-3.6-1.6 3.4-7.6z" />
    </svg>
  );
}
