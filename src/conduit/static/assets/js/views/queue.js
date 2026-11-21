import { api } from '../api.js';
import { STATE_STYLE, card, downloadRow, emptyState, paintProgress, toast } from '../components.js';
import { store } from '../store.js';
import { bytes, html, relativeTime, setHTML } from '../util.js';

const FILTERS = [
  ['all', 'All'],
  ['downloading', 'Downloading'],
  ['pending_approval', 'Needs approval'],
  ['queued', 'Queued'],
  ['no_space', 'No space'],
  ['failed', 'Failed'],
  ['completed', 'Completed'],
];

let filter = 'all';
let query = '';
let history = null;
// History is cached, so refetch it whenever a download finishes or fails.
let historyMark = '';

export default {
  id: 'queue',
  title: 'Queue',
  icon: '≡',
  badge: () => (store.summary.downloading || 0) + (store.summary.queued || 0),

  render(root) {
    const mark = `${store.summary.completed ?? 0}:${store.summary.failed ?? 0}`;
    if (mark !== historyMark) {
      historyMark = mark;
      history = null;
    }
    const items = visible();
    setHTML(root, html`
      <div class="filters">
        ${FILTERS.filter(([value]) => value === filter || countFor(value) !== null)
          .map(([value, label]) => html`
            <button class="chip" data-action="filter" data-value="${value}"
                    aria-pressed="${filter === value}">
              ${label} <span class="faint">${countFor(value) ?? 0}</span>
            </button>`)}
        <div class="grow"></div>
        <input class="filters__search" type="search" placeholder="Filter by title…"
               aria-label="Filter downloads by title" data-action="search" value="${query}">
      </div>

      ${card(`${items.length} download${items.length === 1 ? '' : 's'}`,
        items.length
          ? html`<div class="list">${items.map((d) => downloadRow(d, store.progressOf(d)))}</div>`
          : emptyState('◌', 'Nothing here'))}

      ${card('History',
        html`<div class="tablewrap" id="history-host">${emptyState('…', 'Loading…')}</div>`,
        html`<button class="btn btn--sm btn--ghost" data-action="clear-history">Clear failed, denied &amp; cancelled</button>`)}
    `);
    loadHistory(root);
  },

  onProgress(root) {
    for (const item of store.byState('downloading')) {
      paintProgress(root, item, store.progressOf(item));
    }
  },

  async onAction(action, target, root) {
    if (action === 'filter') {
      filter = target.dataset.value;
      this.render(root);
      return true;
    }
    if (action === 'clear-history') {
      if (!confirm('Clear every failed, denied and cancelled entry from history?\n\n'
                   + 'Completed downloads stay.')) return true;
      const result = await api.action('clear-history');
      history = null;
      toast(`Cleared ${result.removed ?? 0} entr${result.removed === 1 ? 'y' : 'ies'}`);
      store.refresh();
      return true;
    }
    return false;
  },

  onInput(action, target, root) {
    if (action === 'search') {
      query = target.value.toLowerCase();
      const scroll = root.scrollTop;
      this.render(root);
      root.scrollTop = scroll;
      const field = root.querySelector('[data-action="search"]');
      if (field) { field.focus(); field.setSelectionRange(field.value.length, field.value.length); }
      return true;
    }
    return false;
  },
};

function visible() {
  return store.downloads.filter((d) => {
    if (filter !== 'all' && d.state !== filter) return false;
    if (query && !`${d.title} ${d.release_name || ''}`.toLowerCase().includes(query)) return false;
    return true;
  });
}

function countFor(value) {
  if (value === 'all') return store.downloads.length;
  const count = store.downloads.filter((d) => d.state === value).length;
  return count || null;
}

async function loadHistory(root) {
  try {
    if (!history) history = await api.history({ limit: 60 });
    const host = root.querySelector('#history-host');
    if (!host) return;
    setHTML(host, history.length
      ? html`
        <table class="table">
          <thead><tr>
            <th>Title</th><th>Result</th><th>Size</th><th>Quality</th><th>When</th>
          </tr></thead>
          <tbody>
            ${history.map((row) => html`
              <tr>
                <td class="trunc" style="max-width:340px" title="${row.release_name || row.display_title}">
                  ${row.display_title}
                </td>
                <td><span class="pill ${(STATE_STYLE[row.state] || {}).cls || ''}">
                  ${(STATE_STYLE[row.state] || {}).label || row.state}</span></td>
                <td class="mono">${bytes(row.size_bytes)}</td>
                <td class="faint">${[row.resolution, row.source].filter(Boolean).join(' ') || '—'}</td>
                <td class="faint">${relativeTime(row.completed_at || row.updated_at)}</td>
              </tr>`)}
          </tbody>
        </table>`
      : emptyState('·', 'No history yet'));
  } catch (error) {
    const host = root.querySelector('#history-host');
    if (host) setHTML(host, emptyState('', 'History could not load', error.message));
  }
}
