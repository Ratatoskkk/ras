import { api } from '../api.js';
import {
  card, downloadRow, emptyState, paintProgress, poster, toast,
} from '../components.js';
import { store } from '../store.js';
import { bytes, dayLabel, episodeCode, html, relativeTime, setHTML } from '../util.js';

export default {
  id: 'dashboard',
  title: 'Dashboard',
  icon: '◈',
  badge: () => store.summary.pending_approval || 0,

  render(root) {
    const s = store.summary;
    const active = store.byState('downloading');
    const groups = store.pendingGroups;
    const continuations = groups.filter((g) => g.kind === 'continuation');
    const fresh = groups.filter((g) => g.kind !== 'continuation');
    const now = new Date();
    const today = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'),
                   String(now.getDate()).padStart(2, '0')].join('-');
    const scheduled = store.upcoming.filter((item) => item.air_date?.slice(0, 10) >= today);
    const next = scheduled.slice(0, 5);
    const later = scheduled.slice(5, 8);

    setHTML(root, html`
      <section class="program" aria-labelledby="program-title">
        <div class="program__head">
          <div>
            <h2 id="program-title">Coming up</h2>
            <p>${s.upcoming ?? 0} tracked releases</p>
          </div>
          <a class="btn btn--sm" href="#/calendar">Open calendar</a>
        </div>
        ${next.length ? html`
          <div class="program__body">
            ${featureRelease(next[0])}
            <div class="program__run" aria-label="Following releases">
              ${next.slice(1).length
                ? next.slice(1).map(showtimeRow)
                : html`<div class="program__quiet">No other dates scheduled yet.</div>`}
            </div>
          </div>`
          : html`<div class="program__empty">
              <p>No releases have a future date yet.</p>
              <a class="btn btn--primary" href="#/calendar">View calendar</a>
            </div>`}
      </section>

      <div class="status-board" aria-label="Current status">
        ${statusCell('Downloading', s.downloading ?? 0, active.length ? 'in flight now' : 'idle', 'status-board__cell--active')}
        ${statusCell('Awaiting you', s.pending_approval ?? 0, 'approvals')}
        ${statusCell('Queued', s.queued ?? 0, 'ready to start')}
        ${statusCell('Completed', s.completed ?? 0, s.library_size || '0 B')}
        ${statusCell('Problems', (s.failed ?? 0) + (s.no_space ?? 0), 'failed or out of space',
                     ((s.failed ?? 0) + (s.no_space ?? 0)) ? 'status-board__cell--fault' : '')}
      </div>

      ${unmatchedCard()}

      ${continuations.length ? html`
        <div class="stack">
          <div class="sectionhead">
            <h2>Ready when you are</h2>
            <span class="sectionhead__note">
              the next season of something you already watch — it waits here until you start it
            </span>
          </div>
          <div class="approval-grid">${continuations.map(continuationCard)}</div>
        </div>` : ''}

      ${fresh.length ? html`
        <div class="stack">
          <div class="sectionhead">
            <h2>Needs your approval</h2>
            <span class="sectionhead__note">new titles, and anything over the size gate</span>
          </div>
          <div class="approval-grid">${fresh.map(approvalCard)}</div>
        </div>` : ''}

      <div class="split">
        <div class="stack">
          ${card('Active downloads',
            active.length
              ? html`<div class="list">${active.map((d) => downloadRow(d, store.progressOf(d)))}</div>`
              : emptyState('◌', 'Nothing downloading',
                           'Approved grabs start automatically when a slot frees up.'),
            html`<button class="btn btn--sm btn--ghost" data-action="sync-watchlist">Check watchlist</button>
                 <button class="btn btn--sm" data-action="dispatch">Run queue now</button>`)}

          ${card('Recent activity',
            html`<div class="list" id="dash-activity">${emptyState('…', 'Loading…')}</div>`,
            html`<a class="btn btn--sm btn--ghost" href="#/activity">View all</a>`)}
        </div>

        <div class="stack">
          ${card('Tracker account',
            html`<div class="card__body stack" style="gap:8px" id="dash-accounts">
              <span class="muted">Loading…</span>
            </div>`)}
          ${card('Storage', html`<div class="card__body">${store.drives.map(driveRow)}</div>`)}
          ${later.length ? card('Later on',
            html`<div class="list">${later.map(upcomingRow)}</div>`,
            html`<a class="btn btn--sm btn--ghost" href="#/calendar">Calendar</a>`) : ''}
          ${card('System', html`<div class="card__body stack" style="gap:8px">${systemRows()}</div>`)}
        </div>
      </div>
    `);

    loadActivity(root);
    loadAccounts(root);
  },

  onProgress(root) {
    for (const item of store.byState('downloading')) {
      paintProgress(root, item, store.progressOf(item));
    }
  },

  async onAction(action, target, root) {
    if (action === 'dispatch') {
      await api.action('dispatch-queue');
      toast('Queue dispatch triggered');
      store.refresh();
      return true;
    }
    if (action === 'sync-watchlist') {
      target.disabled = true;
      const result = await api.action('sync-watchlist');
      toast(
        result.seen
          ? `Watchlist: ${result.seen} item(s), ${result.added} taken on`
          : 'Watchlist is empty',
        result.added ? 'ok' : '',
      );
      store.refresh();
      return true;
    }
    if (action === 'approve-group' || action === 'deny-group') {
      const ids = JSON.parse(target.dataset.ids);
      const approving = action === 'approve-group';
      const result = approving ? await api.approve(ids) : await api.deny(ids);
      const changed = approving ? result.approved : result.denied;
      toast(changed
        ? `${approving ? 'Approved' : 'Denied'} ${changed} item(s)`
        : 'These approvals were already handled', approving && changed ? 'ok' : '');
      store.refresh();
      return true;
    }
    return false;
  },
};

/**
 * The one blind spot in de-duplication, made visible.
 *
 * Everything Conduit knows about what you already own is keyed on the TMDB id
 * Plex assigns. An entry Plex has not matched carries no id, so it looks like
 * missing media and gets bought again -- which costs real credit on a private
 * tracker. Rare, but silent, so it is worth a card rather than a log line.
 */
function unmatchedCard() {
  const rows = store.unmatched;
  if (!rows.length) return '';
  return html`
    <section class="card card--warn">
      <header class="card__head">
        <h2>${rows.length} librar${rows.length === 1 ? 'y entry Plex has' : 'y entries Plex has'} not matched</h2>
      </header>
      <div class="card__body stack" style="gap:10px">
        <p class="muted" style="margin:0">
          rás decides what you already own by TMDB id, and these entries have none —
          so it cannot see these files and may pay to download them again.
          Fix each one in Plex (⋯ → <strong>Match</strong>, then <strong>Merge</strong> into
          the right title), then use <a href="#/library">Rescan watched state</a>.
        </p>
        <div class="list">
          ${rows.map((row) => html`
            <div class="row">
              <span class="tag">${row.kind === 'movie' ? 'film' : 'series'}</span>
              <span class="grow trunc">${row.title}</span>
              ${row.episodes
                ? html`<span class="faint mono">${row.episodes} episode${row.episodes === 1 ? '' : 's'} hidden</span>`
                : html`<span class="faint mono">not de-duplicated</span>`}
            </div>`)}
        </div>
      </div>
    </section>`;
}

function statusCell(label, value, hint, cls = '') {
  return html`
    <div class="status-board__cell ${cls}">
      <span class="status-board__value">${value}</span>
      <span class="status-board__text"><strong>${label}</strong><small>${hint}</small></span>
    </div>`;
}

function releaseDate(item) {
  const date = new Date(`${item.air_date.slice(0, 10)}T12:00:00`);
  return {
    day: date.toLocaleDateString(undefined, { day: '2-digit' }),
    month: date.toLocaleDateString(undefined, { month: 'short' }),
    weekday: date.toLocaleDateString(undefined, { weekday: 'long' }),
  };
}

function featureRelease(item) {
  const date = releaseDate(item);
  const code = episodeCode(item.season, item.episode);
  return html`
    <a class="program__feature" href="#/calendar/${item.air_date.slice(0, 10)}">
      <div class="program__date">
        <span>${date.weekday}</span>
        <strong>${date.day}</strong>
        <span>${date.month}</span>
      </div>
      <div class="program__poster">${poster(item.poster_path, item.title)}</div>
      <div class="program__feature-copy">
        <h3>${item.title}</h3>
        ${code ? html`<p>${code}${item.episode_title ? ` · ${item.episode_title}` : ''}</p>` : ''}
        <span>View this date in Calendar</span>
      </div>
    </a>`;
}

function showtimeRow(item) {
  const date = releaseDate(item);
  const code = episodeCode(item.season, item.episode);
  return html`
    <a class="program__row" href="#/calendar/${item.air_date.slice(0, 10)}">
      <span class="program__row-date"><strong>${date.day}</strong><small>${date.month}</small></span>
      <span class="program__row-title"><strong>${item.title}</strong>
        <small>${code ? `${code}${item.episode_title ? ` · ${item.episode_title}` : ''}` : date.weekday}</small>
      </span>
      <span class="program__row-arrow" aria-hidden="true"></span>
    </a>`;
}

function pendingRow(item) {
  return html`
    <div class="approval__row">
      <div class="grow">
        <div class="trunc">${item.title}</div>
        <div class="item__release trunc">${item.release_name}</div>
      </div>
      <span class="faint mono">${bytes(item.size_bytes)}</span>
      <div class="item__actions">
        <button class="btn btn--sm" data-action="approve" data-id="${item.id}">Approve</button>
        <button class="btn btn--sm btn--ghost" data-action="deny" data-id="${item.id}">Deny</button>
      </div>
    </div>`;
}

function approvalDetails(group) {
  return group.items.length > 1
    ? html`<details class="approval__details">
        <summary>Review ${group.items.length} releases</summary>
        <div>${group.items.map(pendingRow)}</div>
      </details>`
    : '';
}

function continuationCard(group) {
  const ids = JSON.stringify(group.ids);
  const prev = group.previous_season;
  const season = group.target_season;
  return html`
    <section class="approval approval--next">
      <header class="approval__head">
        ${poster(group.poster_path, group.title)}
        <div class="grow">
          <div class="item__title trunc" title="${group.title}">${group.title}</div>
          <div class="item__meta">
            ${season !== undefined && season !== null
              ? html`<span class="pill pill--accent">Season ${season} ready</span>` : ''}
            <span class="faint">${group.total_size}</span>
          </div>
          ${prev ? html`
            <div class="approval__context faint">
              ${prev.watched}/${prev.episodes} through season ${prev.season}
            </div>` : ''}
          ${group.items.length === 1
            ? html`<div class="approval__release trunc" title="${group.items[0].release_name}">${group.items[0].release_name}</div>`
            : ''}
          <div class="approval__actions">
            <button class="btn btn--sm btn--primary" data-action="approve-group" data-ids='${ids}'>
              ${season !== undefined && season !== null ? html`Start season ${season}` : 'Start'}
            </button>
            <button class="btn btn--sm btn--ghost" data-action="deny-group" data-ids='${ids}'
                    title="Blocklists this release so it is never offered again. If you are simply not ready yet, leave the card where it is.">
              Not this one
            </button>
          </div>
        </div>
      </header>
      ${approvalDetails(group)}
    </section>`;
}

function approvalCard(group) {
  const ids = JSON.stringify(group.ids);
  return html`
    <section class="approval">
      <header class="approval__head">
        ${poster(group.poster_path, group.title)}
        <div class="grow">
          <div class="item__title trunc" title="${group.title}">${group.title}</div>
          <div class="item__meta">
            <span class="pill pill--warn">${group.count} item${group.count === 1 ? '' : 's'}</span>
            <span class="faint">${group.total_size} total</span>
          </div>
          ${group.items.length === 1
            ? html`<div class="approval__release trunc" title="${group.items[0].release_name}">${group.items[0].release_name}</div>`
            : ''}
          <div class="approval__actions">
            <button class="btn btn--sm btn--primary" data-action="approve-group" data-ids='${ids}'>
              ${group.count === 1 ? 'Approve' : 'Approve all'}
            </button>
            <button class="btn btn--sm btn--danger" data-action="deny-group" data-ids='${ids}'>
              ${group.count === 1 ? 'Deny' : 'Deny all'}
            </button>
          </div>
        </div>
      </header>
      ${approvalDetails(group)}
    </section>`;
}

function driveRow(drive) {
  const used = drive.percent_used || 0;
  const variant = used > 92 ? 'drive__fill--full' : used > 80 ? 'drive__fill--warn' : '';
  return html`
    <div class="drive">
      <div class="row">
        <strong>${drive.label}</strong>
        <span class="faint mono trunc grow">${drive.path}</span>
        ${drive.exists
          ? html`<span class="mono">${bytes(drive.free_bytes)} free</span>`
          : html`<span class="pill pill--err">offline</span>`}
      </div>
      <div class="drive__bar"><div class="drive__fill ${variant}" style="width:${used}%"></div></div>
      <div class="faint">
        ${bytes(drive.used_bytes)} of ${bytes(drive.total_bytes)} used · ${used}%
      </div>
    </div>`;
}

function upcomingRow(item) {
  const code = episodeCode(item.season, item.episode);
  return html`
    <div class="item" style="grid-template-columns:36px 1fr auto">
      ${poster(item.poster_path, item.title)}
      <div class="grow">
        <div class="trunc">${item.title}</div>
        <div class="faint">
          ${code ? `${code} · ` : ''}${item.episode_title || ''}
        </div>
      </div>
      <span class="faint mono">${dayLabel(item.air_date)}</span>
    </div>`;
}

function systemRows() {
  const t = store.timestamps;
  const rows = [
    ['Watchlist checked', relativeTime(t.watchlist_checked_at)],
    ['Library indexed', relativeTime(t.library_indexed_at)],
    ['Calendar refreshed', relativeTime(t.calendar_refreshed_at)],
    ['Running since', relativeTime(t.started_at)],
    ['Trackers', `${store.summary.indexers ?? 0} enabled`],
  ];
  if (store.summary.dry_run) rows.push(['Mode', 'DRY RUN — nothing will be sent']);
  return rows.map(([label, value]) => html`
    <div class="row"><span class="muted grow">${label}</span><span class="mono">${value}</span></div>`);
}

async function loadAccounts(root) {
  const host = root.querySelector('#dash-accounts');
  if (!host) return;
  try {
    const accounts = await api.accounts();
    if (!accounts.length) {
      setHTML(host, html`<span class="faint">No tracker reachable.</span>`);
      return;
    }
    setHTML(host, html`${accounts.map((a) => html`
      <div class="stack" style="gap:6px">
        <div class="row">
          <strong>${a.indexer}</strong>
          ${a.group ? html`<span class="pill">${a.group}</span>` : ''}
          <span class="grow"></span>
          <span class="faint">${a.username || ''}</span>
        </div>
        <div class="row"><span class="muted grow">Ratio</span>
          <span class="mono" style="color:var(--ok)">${a.ratio ?? '—'}</span></div>
        <div class="row"><span class="muted grow">Buffer</span>
          <span class="mono">${a.buffer ?? '—'}</span></div>
        <div class="row"><span class="muted grow">Up / down</span>
          <span class="mono">${a.uploaded ?? '—'} / ${a.downloaded ?? '—'}</span></div>
        <div class="row"><span class="muted grow">Seeding</span>
          <span class="mono">${a.seeding ?? 0} · ${a.leeching ?? 0} leeching</span></div>
        ${Number(a.hit_and_runs) > 0
          ? html`<div class="row"><span class="muted grow">Hit and runs</span>
                   <span class="mono" style="color:var(--warn)">${a.hit_and_runs}</span></div>`
          : ''}
      </div>`)}`);
  } catch {
    setHTML(host, html`<span class="faint">Tracker unreachable.</span>`);
  }
}

async function loadActivity(root) {
  try {
    const events = await api.events({ limit: 12 });
    const host = root.querySelector('#dash-activity');
    if (!host) return;
    setHTML(host, events.length
      ? html`${events.map((e) => html`
          <div class="event event--${e.level}">
            <span class="event__time">${relativeTime(e.ts)}</span>
            <span class="event__msg">
              <span class="event__cat">${e.category}</span>${e.message}
            </span>
          </div>`)}`
      : emptyState('·', 'No activity yet'));
  } catch { /* the card just stays on its loading state */ }
}
