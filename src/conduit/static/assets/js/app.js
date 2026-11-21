// Application shell: routing, navigation, global event delegation.

import { api } from './api.js';
import { closeModal, toast } from './components.js';
import { icon } from './icons.js';
import { store } from './store.js';
import { $, html, relativeTime, setHTML } from './util.js';

import activity from './views/activity.js';
import calendarView from './views/calendar.js';
import dashboard from './views/dashboard.js';
import library from './views/library.js';
import queue from './views/queue.js';
import settings from './views/settings.js';

const VIEWS = [dashboard, queue, calendarView, library, activity, settings];
const BY_ID = Object.fromEntries(VIEWS.map((view) => [view.id, view]));
const THEME_KEY = 'conduit.theme';

let current = null;
let viewRoot = null;
let frame = null;

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------
function routeId() {
  const id = (location.hash.replace(/^#\/?/, '') || 'dashboard').split('/')[0];
  return BY_ID[id] ? id : 'dashboard';
}

/**
 * A fresh container for every route render. Views await the API before they
 * paint; with one shared element, a slow response from the page you just left
 * painted itself over the page you moved to. Now it lands in a detached node.
 */
function freshViewRoot() {
  const node = document.createElement('div');
  node.className = 'view';
  $('#view').replaceChildren(node);
  viewRoot = node;
  return node;
}

async function renderRoute() {
  const view = BY_ID[routeId()];
  if (current && current !== view) current.destroy?.();
  // A new page starts at its top, not wherever the last one was scrolled to.
  // A calendar day link scrolls itself to the day once it has painted.
  if (current !== view) window.scrollTo(0, 0);
  current = view;

  $('#page-title').textContent = view.title;
  document.title = view.id === 'dashboard' ? 'rás' : `${view.title} · rás`;
  renderNav();

  const root = freshViewRoot();
  $('#view').focus({ preventScroll: true });
  closeRailOnMobile();
  try {
    await view.render(root);
  } catch (error) {
    if (root.isConnected) {
      setHTML(root, html`<div class="card"><div class="empty">${error.message}</div></div>`);
    }
  }
}

function renderNav() {
  const active = routeId();
  setHTML($('#nav'), html`
    ${VIEWS.map((view) => {
      const badge = view.badge?.();
      return html`
        <a class="navlink" href="#/${view.id}" ${active === view.id ? 'aria-current="page"' : ''}>
          <span class="navlink__icon">${icon(view.id)}</span>
          <span class="grow">${view.title}</span>
          ${badge ? html`<span class="navlink__badge">${badge}</span>` : ''}
        </a>`;
    })}`);
}

// ---------------------------------------------------------------------------
// Status strip
// ---------------------------------------------------------------------------
function renderStatus() {
  const summary = store.summary;
  const connection = store.connection;
  const connectionPill = {
    live: ['pill--ok', 'Live'],
    connecting: ['pill--warn', 'Connecting…'],
    offline: ['pill--err', 'Offline'],
  }[connection] || ['pill--warn', connection];

  const problems = (summary.failed || 0) + (summary.no_space || 0);

  setHTML($('#status-strip'), html`
    ${summary.dry_run ? html`<span class="pill pill--warn">Dry run</span>` : ''}
    ${problems ? html`<a class="pill pill--err" href="#/queue">${problems} problem${problems === 1 ? '' : 's'}</a>` : ''}
    ${summary.downloading
      ? html`<span class="pill pill--info">
               <span class="pill__dot pill__dot--live"></span>${summary.downloading} downloading
             </span>`
      : ''}
    <span class="pill" title="Watchlist last checked">
      Checked ${relativeTime(store.timestamps.watchlist_checked_at)}
    </span>
    <span class="pill ${connectionPill[0]}">
      <span class="pill__dot ${connection === 'live' ? 'pill__dot--live' : ''}"></span>${connectionPill[1]}
    </span>`);

  $('#version').textContent = store.state?.version ? `v${store.state.version}` : '';
}

// ---------------------------------------------------------------------------
// Global actions
// ---------------------------------------------------------------------------
const GLOBAL_ACTIONS = {
  async approve(target) {
    const result = await api.approve([Number(target.dataset.id)]);
    toast(result.approved ? 'Approved — it will start on the next queue pass'
                          : 'This approval was already handled', result.approved ? 'ok' : '');
    store.refresh();
  },
  async deny(target) {
    const result = await api.deny([Number(target.dataset.id)]);
    toast(result.denied ? 'Denied and blocklisted' : 'This approval was already handled');
    store.refresh();
  },
  async retry(target) {
    await api.retry(Number(target.dataset.id));
    toast('Requeued');
    store.refresh();
  },
  async remove(target) {
    if (!confirm('Remove this download from qBittorrent and delete its files?')) return;
    await api.removeDownload(Number(target.dataset.id), true);
    toast('Removed');
    store.refresh();
  },
};

document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-action], [data-close]');
  if (!target) return;

  if (target.hasAttribute('data-close')) {
    closeModal();
    return;
  }

  const action = target.dataset.action;
  if (!action || target.tagName === 'INPUT' || target.tagName === 'SELECT') return;
  event.preventDefault();
  target.closest('[popover]')?.hidePopover();

  // A button stays disabled while its request is in flight, so a second
  // click cannot approve, deny or delete the same thing twice.
  // A view that relabels its button ("Searching…") keeps it disabled until the
  // refresh repaints the row.
  const button = target.tagName === 'BUTTON' && !target.disabled ? target : null;
  const label = button?.innerHTML;
  if (button) button.disabled = true;
  try {
    if (await current?.onAction?.(action, target, viewRoot)) return;
    if (GLOBAL_ACTIONS[action]) await GLOBAL_ACTIONS[action](target);
  } catch (error) {
    toast(error.message || 'Something went wrong', 'err');
    if (button) button.innerHTML = label;
  } finally {
    if (button?.isConnected && button.innerHTML === label) button.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Row menus: popovers sit in the top layer, so a card's overflow cannot clip
// them; this pins each one under (or above) the button that opened it.
// ---------------------------------------------------------------------------
document.addEventListener('toggle', (event) => {
  const menu = event.target;
  if (!(menu instanceof HTMLElement) || !menu.matches('.menu') || event.newState !== 'open') return;
  const opener = document.querySelector(`[popovertarget="${menu.id}"]`);
  if (!opener) return;
  const box = opener.getBoundingClientRect();
  const below = box.bottom + 4 + menu.offsetHeight <= window.innerHeight;
  menu.style.top = `${below ? box.bottom + 4 : box.top - 4 - menu.offsetHeight}px`;
  menu.style.left = `${Math.max(8, box.right - menu.offsetWidth)}px`;
  menu.querySelector('button')?.focus();
}, true);
window.addEventListener('scroll', () => {
  document.querySelector('.menu:popover-open')?.hidePopover();
}, { passive: true });

const onFieldEvent = (event) => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  current?.onInput?.(target.dataset.action, target, viewRoot);
};
document.addEventListener('input', onFieldEvent);
document.addEventListener('change', onFieldEvent);

document.addEventListener('keydown', (event) => {
  const modal = $('#modal');
  if (modal.hidden) return;
  if (event.key === 'Escape') {
    closeModal();
    return;
  }
  // Keep Tab inside the open dialog.
  if (event.key !== 'Tab') return;
  const focusable = [...modal.querySelectorAll('button:not([disabled]), a[href], input, select, textarea')];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

// ---------------------------------------------------------------------------
// Theme + chrome
// ---------------------------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* private window */ }
  const label = `Theme: ${theme}`;
  $('#theme-toggle').setAttribute('aria-label', label);
  $('#theme-toggle').title = label;
}

$('#theme-toggle').addEventListener('click', () => {
  const order = ['auto', 'dark', 'light'];
  const next = order[(order.indexOf(document.documentElement.dataset.theme) + 1) % order.length];
  applyTheme(next);
  toast(`Theme: ${next}`);
});

function setRailOpen(open, focusSelector) {
  const rail = $('#rail');
  rail.dataset.open = String(open);
  $('#menu-toggle').setAttribute('aria-expanded', String(open));
  if (focusSelector) $(focusSelector).focus();
}

$('#menu-toggle').addEventListener('click', () => {
  const open = $('#rail').dataset.open !== 'true';
  setRailOpen(open, open ? '#rail-close' : '#menu-toggle');
});
$('#rail-close').addEventListener('click', () => setRailOpen(false, '#menu-toggle'));
$('#rail-scrim').addEventListener('click', () => setRailOpen(false, '#menu-toggle'));
$('#nav').addEventListener('click', (event) => {
  if (event.target.closest('a')) setRailOpen(false, '#view');
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && $('#rail').dataset.open === 'true') setRailOpen(false, '#menu-toggle');
});

function closeRailOnMobile() {
  if (window.matchMedia('(max-width: 820px)').matches) setRailOpen(false);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function scheduleProgressPaint() {
  if (frame) return;
  const paint = () => {
    frame = null;
    if (current?.onProgress && viewRoot) current.onProgress(viewRoot);
    if (store.summary.downloading) scheduleProgressPaint();
  };
  frame = requestAnimationFrame(() => setTimeout(paint, 200));
}

store.on('state', () => {
  renderNav();
  renderStatus();
  // Views that cache their own data reload it on the next visit.
  if (current && ['dashboard', 'queue', 'calendar'].includes(current.id)) {
    current.render(viewRoot);
  }
  scheduleProgressPaint();
});
store.on('connection', renderStatus);
store.on('progress', scheduleProgressPaint);
store.on('tick', () => {
  if (store.summary.downloading) scheduleProgressPaint();
});
store.on('error', (event) => toast(event.detail.message, 'err'));
store.on('notice', (event) => {
  const { topic } = event.detail;
  if (topic === 'download.created') toast(`Found: ${event.detail.title}`, 'ok');
  if (topic === 'download.completed') toast(`Finished: ${event.detail.title}`, 'ok');
  if (topic === 'config.reloaded') settings.invalidate?.();
  if (topic === 'library.indexed') library.invalidate?.();
});

window.addEventListener('hashchange', renderRoute);

applyTheme(localStorage.getItem(THEME_KEY) || 'auto');
renderNav();
renderStatus();
renderRoute();
store.start();
