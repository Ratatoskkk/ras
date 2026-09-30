import { html, raw } from './util.js';

const paths = {
  dashboard: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M10 9v12"/>',
  queue: '<path d="M5 6h14M5 12h14M5 18h14"/><circle cx="3" cy="6" r=".5" fill="currentColor"/><circle cx="3" cy="12" r=".5" fill="currentColor"/><circle cx="3" cy="18" r=".5" fill="currentColor"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18M8 14h2M14 14h2M8 18h2"/>',
  library: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 15h6"/>',
  activity: '<path d="M3 12h4l3-6 4 12 3-6h4"/>',
  settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="2" fill="var(--bg-sunken)"/><circle cx="15" cy="17" r="2" fill="var(--bg-sunken)"/>',
  chevronLeft: '<path d="m15 5-7 7 7 7"/>',
  chevronRight: '<path d="m9 5 7 7-7 7"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14-5L4 8M4 4v4h4M4 13a8 8 0 0 0 14 5l2-2M20 20v-4h-4"/>',
  close: '<path d="M5 5 19 19M19 5 5 19"/>',
};

export function icon(name) {
  return html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true">${raw(paths[name] || '')}</svg>`;
}
