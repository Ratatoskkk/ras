import { api } from '../api.js';
import {
  card, closeModal, emptyState, openModal, poster, progressBar, searchOutcome, toast,
} from '../components.js';
import { icon } from '../icons.js';
import { store } from '../store.js';
import { bytes, html, relativeTime, setHTML, titleCase } from '../util.js';

let rows = null;
let cleanup = null;
let tab = 'monitored';
let query = '';
let preview = null;
let previewTitle = '';
const PREVIEW_PAGE_SIZE = 50;

export default {
  id: 'library',
  title: 'Library',
  icon: '▤',
  badge: () => null,

  async render(root) {
    if (!rows) {
      setHTML(root, html`${card('Library', emptyState('…', 'Loading…'))}`);
      try {
        [rows, cleanup] = await Promise.all([api.media(), api.cleanup()]);
      } catch (error) {
        setHTML(root, html`${card('Library', emptyState('⚠', error.message))}`);
        return;
      }
    }
    paint(root);
  },

  async onAction(action, target, root) {
    const id = Number(target.dataset.id);
    switch (action) {
      case 'tab':
        tab = target.dataset.value;
        paint(root);
        return true;
      case 'toggle-monitor': {
        const ignored = target.dataset.ignored === 'true';
        await api.patchMedia(id, { ignored: !ignored, monitored: ignored });
        rows = await api.media();
        paint(root);
        toast(ignored ? 'Following again' : 'Stopped following');
        return true;
      }
      case 'search-media': {
        target.disabled = true;
        target.textContent = 'Searching…';
        const result = await api.searchMedia(id);
        toast(searchOutcome(result), result.grabbed ? 'ok' : '');
        store.refresh();
        rows = await api.media();
        paint(root);
        return true;
      }
      case 'preview':
        await showPreview(id, target.dataset.title);
        return true;
      case 'preview-page':
        paintPreview(Number(target.dataset.page));
        return true;
      case 'seen-all': {
        const title = target.dataset.title;
        if (!confirm(
          `Mark every outstanding episode of "${title}" as already watched?\n\n`
          + 'The show stays followed and new episodes will still be picked up.'
        )) return true;
        const result = await api.markMediaWatched(id, {});
        // Re-fetch before repainting: the row shows its outstanding count, so
        // without this the action succeeds while the UI looks untouched.
        rows = await api.media();
        paint(root);
        store.refresh();
        toast(
          result.changed
            ? `Marked ${result.changed} episode(s) of "${title}" as seen`
            : `"${title}" had nothing outstanding`,
          result.changed ? 'ok' : '',
        );
        return true;
      }
      case 'refresh-media':
        await api.refreshMedia(id);
        toast('Refreshed from TMDB');
        store.refresh();
        return true;
      case 'unfollow':
        if (!confirm('Remove this title and everything rás tracks for it?')) return true;
        await api.deleteMedia(id);
        rows = await api.media();
        paint(root);
        toast('Removed');
        return true;
      case 'reclaim': {
        const name = target.dataset.title || 'this download';
        if (!confirm(
          `Delete "${name}" from qBittorrent and erase its files from disk?\n\n`
          + 'This cannot be undone.'
        )) return true;
        try {
          await api.removeDownload(id, true, true);
        } catch (error) {
          // 409 means the seed goal is not met yet -- say so rather than
          // silently doing nothing.
          toast(error.message, 'err');
          return true;
        }
        cleanup = await api.cleanup();
        paint(root);
        store.refresh();
        toast(`Deleted "${name}" and freed its space`, 'ok');
        return true;
      }
      case 'rescan':
        await api.cleanupScan();
        toast('Rescanning Plex for watched state…');
        return true;
      case 'clear-ignored': {
        const result = await api.action('clear-ignored');
        rows = await api.media();
        paint(root);
        store.refresh();
        toast(`Restored ${result.titles} title(s) and ${result.wants} episode(s)`, 'ok');
        return true;
      }
      default:
        return false;
    }
  },

  onInput(action, target, root) {
    if (action === 'search') {
      query = target.value.toLowerCase();
      paint(root);
      const field = root.querySelector('[data-action="search"]');
      if (field) { field.focus(); field.setSelectionRange(field.value.length, field.value.length); }
      return true;
    }
    return false;
  },

  invalidate() { rows = null; cleanup = null; },
};

function paint(root) {
  const reclaimable = (cleanup || []).reduce((sum, r) => sum + Number(r.size_bytes || 0), 0);
  setHTML(root, html`
    <div class="filters">
      <button class="chip" data-action="tab" data-value="monitored" aria-pressed="${tab === 'monitored'}">
        Followed <span class="faint">${(rows || []).filter((r) => !r.ignored).length}</span>
      </button>
      <button class="chip" data-action="tab" data-value="ignored" aria-pressed="${tab === 'ignored'}">
        Ignored <span class="faint">${(rows || []).filter((r) => r.ignored).length}</span>
      </button>
      <button class="chip" data-action="tab" data-value="cleanup" aria-pressed="${tab === 'cleanup'}">
        Reclaim space <span class="faint">${bytes(reclaimable)}</span>
      </button>
      <div class="grow"></div>
      <input type="search" placeholder="Search titles…" aria-label="Search library titles" data-action="search" value="${query}"
             style="max-width:240px">
      <button class="btn btn--sm btn--ghost" data-action="clear-ignored"
              title="Un-ignore every title and bring its episodes back into play">
        Clear ignore list
      </button>
    </div>
    ${tab === 'cleanup' ? cleanupPanel() : mediaPanel()}
  `);
}

function mediaPanel() {
  const wantIgnored = tab === 'ignored';
  const items = (rows || [])
    .filter((r) => Boolean(r.ignored) === wantIgnored)
    .filter((r) => !query || r.title.toLowerCase().includes(query));

  if (!items.length) {
    return card(wantIgnored ? 'Ignored titles' : 'Followed titles',
      emptyState('▤', wantIgnored ? 'Nothing ignored' : 'Nothing followed yet',
        'Add to your Plex watchlist, or start watching a series — rás picks it up automatically.'));
  }

  return card(`${items.length} title${items.length === 1 ? '' : 's'}`, html`
    <div class="list">
      ${items.map((row) => html`
        <div class="item library-item">
          ${poster(row.poster_path, row.title)}
          <div class="grow">
            <div class="item__title trunc">
              ${row.title}${row.year ? html` <span class="faint">(${row.year})</span>` : ''}
            </div>
            <div class="item__meta">
              <span class="tag">${row.media_type === 'show' ? 'series' : 'film'}</span>
              ${row.tmdb_status ? html`<span class="tag">${row.tmdb_status}</span>` : ''}
              ${row.wanted_outstanding
                ? html`<span class="pill pill--accent">${row.wanted_outstanding} wanted</span>`
                : html`<span class="pill pill--ok">nothing outstanding</span>`}
              ${row.wanted_seen
                ? html`<span class="pill">${row.wanted_seen} marked seen</span>` : ''}
              ${row.media_type === 'show' && row.library_episodes
                ? html`<span class="faint">${row.library_watched}/${row.library_episodes} watched</span>`
                : ''}
              ${row.profile ? html`<span class="faint">· profile ${row.profile}</span>` : ''}
              <span class="faint">· added ${relativeTime(row.created_at)}</span>
            </div>
          </div>
          <div class="item__side">
            <div class="item__actions">
              <button class="btn btn--sm" data-action="search-media" data-id="${row.id}">Search</button>
              <button class="btn btn--sm btn--ghost" data-action="preview" data-id="${row.id}"
                      data-title="${row.title}" title="Show what the trackers have, and how it scores">
                Releases
              </button>
              <button class="btn btn--sm btn--ghost" data-action="seen-all" data-id="${row.id}"
                      data-title="${row.title}"
                      title="Mark everything outstanding for this title as already watched">
                Seen all
              </button>
              <button class="btn btn--sm btn--ghost" data-action="refresh-media" data-id="${row.id}"
                      aria-label="Refresh ${row.title}" title="Refresh ${row.title} from TMDB">${icon('refresh')}</button>
              <button class="btn btn--sm btn--ghost" data-action="toggle-monitor" data-id="${row.id}"
                      data-ignored="${Boolean(row.ignored)}">
                ${row.ignored ? 'Follow' : 'Ignore'}
              </button>
              <button class="btn btn--sm btn--danger" data-action="unfollow" data-id="${row.id}"
                      aria-label="Unfollow ${row.title}" title="Unfollow ${row.title}">${icon('close')}</button>
            </div>
          </div>
        </div>`)}
    </div>`);
}

function cleanupPanel() {
  const all = (cleanup || []).filter(
    (r) => !query || r.display_title.toLowerCase().includes(query)
  );
  const ready = all.filter((r) => r.seed_satisfied);
  const seeding = all.filter((r) => !r.seed_satisfied);
  const readyBytes = ready.reduce((sum, r) => sum + Number(r.size_bytes || 0), 0);
  const seedingBytes = seeding.reduce((sum, r) => sum + Number(r.size_bytes || 0), 0);

  return html`
    ${card(`Ready to reclaim — ${bytes(readyBytes)}`,
      ready.length
        ? html`<div class="list">${ready.map((row) => reclaimRow(row, true))}</div>`
        : emptyState('✓', 'Nothing ready yet',
            'Watched downloads appear here once they have met the seeding requirement.'),
      html`<button class="btn btn--sm" data-action="rescan">Rescan watched state</button>`)}

    ${seeding.length
      ? card(`Watched, still seeding — ${bytes(seedingBytes)}`, html`
          <div class="list">${seeding.map((row) => reclaimRow(row, false))}</div>`)
      : ''}
  `;
}

function reclaimRow(row, ready) {
  const pct = Math.round((row.seed_progress || 0) * 100);
  return html`
    <div class="item">
      ${poster(row.poster_path, row.display_title)}
      <div class="grow">
        <div class="item__title trunc">${row.display_title}</div>
        <div class="item__meta">
          <span class="tag">${row.drive_label}</span>
          ${row.resolution ? html`<span class="tag tag--res">${row.resolution}</span>` : ''}
          ${ready
            ? html`<span class="pill pill--ok">${row.seed_reason}</span>`
            : html`<span class="pill pill--warn">${row.seed_reason}</span>`}
          <span class="faint">seeded ${row.seeding_human}</span>
          ${row.seed_ratio ? html`<span class="faint">· ratio ${row.seed_ratio}</span>` : ''}
        </div>
        ${!ready ? progressBar(row.seed_progress, '') : ''}
        <div class="faint mono trunc" style="margin-top:4px">${row.save_path || ''}</div>
      </div>
      <div class="item__side">
        <span class="mono">${row.human_size}</span>
        ${ready
          ? html`<button class="btn btn--sm btn--danger" data-action="reclaim"
                         data-id="${row.id}" data-title="${row.display_title}">
                   Delete files
                 </button>`
          : html`<span class="faint mono">${pct}% seeded</span>`}
      </div>
    </div>`;
}

async function showPreview(mediaId, title) {
  openModal(`Releases for ${title}`, html`<div class="empty">Querying trackers…</div>`);
  try {
    preview = await api.previewMedia(mediaId);
  } catch (error) {
    openModal(`Releases for ${title}`, html`<div class="empty">${error.message}</div>`);
    return;
  }
  previewTitle = title;
  paintPreview(0);
}

function paintPreview(page) {
  if (!preview) return;
  const data = preview;
  const pages = Math.max(1, Math.ceil(data.candidates.length / PREVIEW_PAGE_SIZE));
  const currentPage = Math.max(0, Math.min(page, pages - 1));
  const start = currentPage * PREVIEW_PAGE_SIZE;
  const visible = data.candidates.slice(start, start + PREVIEW_PAGE_SIZE);

  const body = html`
    <p class="muted" style="margin-top:0">
      ${data.total} release${data.total === 1 ? '' : 's'} found, ranked by the
      <strong>${data.profile}</strong> profile. Rejected entries show the exact rule that
      excluded them.
    </p>
    ${pages > 1 ? html`<p class="muted">Showing ${start + 1}–${start + visible.length} of ${data.total}</p>` : ''}
    ${visible.length
      ? visible.map((c) => html`
          <div class="candidate ${c.accepted ? '' : 'candidate--rejected'}">
            <div class="row">
              <span class="score">${c.accepted ? c.score : '—'}</span>
              <div class="grow">
                <div class="candidate__name">${c.name}</div>
                <div class="item__meta">
                  ${c.resolution ? html`<span class="tag tag--res">${c.resolution}</span>` : ''}
                  ${c.source ? html`<span class="tag">${c.source}</span>` : ''}
                  ${c.dynamic_range && c.dynamic_range !== 'sdr'
                    ? html`<span class="tag tag--hdr">${c.dynamic_range.replace(/_/g, '+')}</span>` : ''}
                  ${c.audio ? html`<span class="tag">${c.audio.replace(/_/g, ' ')}</span>` : ''}
                  ${c.freeleech ? html`<span class="pill pill--ok">freeleech</span>` : ''}
                  ${c.internal ? html`<span class="tag">internal</span>` : ''}
                  <span class="faint">${bytes(c.size_bytes)} · ${c.seeders} seeders · ${c.indexer}</span>
                </div>
                ${c.rejections.length
                  ? html`<div class="candidate__why">${c.rejections.join(' · ')}</div>`
                  : html`<div class="faint" style="margin-top:5px">
                           ${Object.entries(c.breakdown).map(([k, v]) =>
                             `${titleCase(k)} +${v}`).join('  ')}
                         </div>`}
              </div>
            </div>
          </div>`)
      : emptyState('∅', 'The trackers returned nothing for this title')}
    ${pages > 1 ? html`
      <div class="row" style="justify-content:space-between;margin-top:16px">
        <button class="btn btn--sm btn--ghost" data-action="preview-page"
                data-page="${currentPage - 1}" ${currentPage === 0 ? 'disabled' : ''}>Previous</button>
        <span class="muted">Page ${currentPage + 1} of ${pages}</span>
        <button class="btn btn--sm btn--ghost" data-action="preview-page"
                data-page="${currentPage + 1}" ${currentPage === pages - 1 ? 'disabled' : ''}>Next</button>
      </div>` : ''}
  `;
  openModal(`Releases for ${previewTitle}`, body);
}

export { closeModal };
