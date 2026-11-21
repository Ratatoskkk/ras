// Small helpers shared by every view. No dependencies, no build step.

/** Marks a string as already-safe HTML so `html` will not escape it. */
export class Raw {
  constructor(value) { this.value = value; }
  toString() { return this.value; }
}
export const raw = (value) => new Raw(value);

export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Tagged template that escapes every interpolation by default.
 * Arrays are joined, `raw()` values and nested `html` results pass through.
 */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    out += render(values[i]) + strings[i + 1];
  }
  return new Raw(out);
}

function render(value) {
  if (value instanceof Raw) return value.value;
  if (Array.isArray(value)) return value.map(render).join('');
  if (value === null || value === undefined || value === false) return '';
  return esc(value);
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export function setHTML(node, content) {
  node.innerHTML = content instanceof Raw ? content.value : String(content);
  return node;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

export function bytes(value) {
  const n = Number(value) || 0;
  if (n <= 0) return '0 B';
  let index = 0;
  let size = n;
  while (size >= 1024 && index < UNITS.length - 1) { size /= 1024; index += 1; }
  const digits = index < 2 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${UNITS[index]}`;
}

export function speed(bps) {
  const n = Number(bps) || 0;
  return n <= 0 ? '—' : `${bytes(n)}/s`;
}

export function duration(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return '∞';
  // Round once, up front: rounding the remainder alone turned 119.6s into "1m 60s".
  const whole = Math.round(n);
  if (whole < 60) return `${whole}s`;
  const m = Math.floor(whole / 60);
  if (m < 60) return `${m}m ${String(whole % 60).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
  return `${Math.floor(h / 24)}d ${String(h % 24).padStart(2, '0')}h`;
}

export function percent(fraction) {
  return `${((Number(fraction) || 0) * 100).toFixed(1)}%`;
}

export function parseDate(value) {
  if (!value) return null;
  // SQLite writes "YYYY-MM-DD HH:MM:SS" in UTC without a zone marker.
  const text = typeof value === 'string' && /^\d{4}-\d{2}-\d{2} /.test(value)
    ? value.replace(' ', 'T') + 'Z'
    : value;
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

const RELATIVE_STEPS = [
  [60, 'second', 1],
  [3600, 'minute', 60],
  [86400, 'hour', 3600],
  [604800, 'day', 86400],
  [2629800, 'week', 604800],
  [31557600, 'month', 2629800],
  [Infinity, 'year', 31557600],
];

// Built once. Constructing an Intl formatter is not cheap, and a calendar
// render calls this for every row.
let relativeFormatter = null;

export function relativeTime(value) {
  const date = parseDate(value);
  if (!date) return 'never';
  const delta = (date.getTime() - Date.now()) / 1000;
  const abs = Math.abs(delta);
  relativeFormatter ??= new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  for (const [limit, unit, divisor] of RELATIVE_STEPS) {
    if (abs < limit) return relativeFormatter.format(Math.round(delta / divisor), unit);
  }
  return date.toLocaleDateString();
}

export function clockTime(value) {
  const date = parseDate(value);
  return date ? date.toLocaleTimeString(undefined, { hour12: false }) : '—';
}

export function dayLabel(isoDate) {
  if (!isoDate) return 'Date unknown';
  const date = new Date(`${String(isoDate).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(date.getTime())) return 'Date unknown';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.round((date - today) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  if (days === -1) return 'Yesterday';
  if (days > 1 && days < 7) return date.toLocaleDateString(undefined, { weekday: 'long' });
  return date.toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}

export function countdown(isoDate) {
  if (!isoDate) return { text: 'TBA', past: true };
  const target = new Date(`${String(isoDate).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(target.getTime())) return { text: 'TBA', past: true };
  const delta = (target - Date.now()) / 1000;
  // Airing today is still coming up, not part of the backlog.
  if (delta <= 0 && delta > -86400) return { text: 'today', past: false };
  if (delta <= 0) return { text: 'aired', past: true };
  if (delta < 86400) return { text: duration(delta), past: false };
  return { text: `${Math.ceil(delta / 86400)}d`, past: false };
}

export function episodeCode(season, episode) {
  if (season === null || season === undefined) return '';
  const s = `S${String(season).padStart(2, '0')}`;
  return episode === null || episode === undefined
    ? s
    : `${s}E${String(episode).padStart(2, '0')}`;
}

export function posterUrl(path, size = 'w154') {
  return path ? `https://image.tmdb.org/t/p/${size}${path}` : null;
}

export function titleCase(value) {
  return String(value || '').replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export const debounce = (fn, wait = 250) => {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
};

export function groupBy(items, keyFn) {
  const map = new Map();
  for (const item of items) {
    const key = keyFn(item);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(item);
  }
  return map;
}
