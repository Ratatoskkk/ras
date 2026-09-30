import { api } from '../api.js';
import {
  WANT_STATE_STYLE, card, emptyState, poster, searchOutcome, toast,
} from '../components.js';
import { store } from '../store.js';
import { icon } from '../icons.js';
import {
  countdown, dayLabel, episodeCode, groupBy, html, posterUrl, relativeTime, setHTML,
} from '../util.js';

// Module scope, not the DOM: the view re-renders on every WebSocket state push,
// so anything held in an element would reset itself a few seconds later.
let mode = 'month';
let viewMonth = startOfMonth(new Date());
let selectedDay = null;
let routeDay = null;
// The backlog is deliberately behind a toggle in agenda mode. A library with
// years of unwatched back catalogue would otherwise bury next week's releases.
let showBacklog = false;

const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MAX_POSTERS = 3;

export default {
  id: 'calendar',
  title: 'Calendar',
  icon: '◷',
  badge: () => null,

  destroy() {
    routeDay = null;
  },

  render(root) {
    const day = location.hash.match(/^#\/calendar\/(\d{4}-\d{2}-\d{2})$/)?.[1] || null;
    const revealDay = day && day !== routeDay;
    if (revealDay) {
      const date = new Date(`${day}T12:00:00`);
      if (!Number.isNaN(date.getTime())) {
        viewMonth = startOfMonth(date);
        selectedDay = day;
        mode = 'month';
      }
    }
    routeDay = day;
    setHTML(root, html`
      <div class="filters">
        <button class="chip" data-action="cal-mode" data-value="month"
                aria-pressed="${mode === 'month'}">Month</button>
        <button class="chip" data-action="cal-mode" data-value="agenda"
                aria-pressed="${mode === 'agenda'}">Agenda</button>
        <div class="grow"></div>
        <button class="btn btn--sm" data-action="refresh-calendar">Refresh from TMDB</button>
        <button class="btn btn--sm btn--primary" data-action="search-now">Search trackers now</button>
      </div>
      ${mode === 'month' ? monthView() : agendaView()}
    `);
    if (revealDay) {
      requestAnimationFrame(() => root.querySelector('.cal__detail')?.scrollIntoView({
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        block: 'start',
      }));
    }
  },

  async onAction(action, target, root) {
    if (action === 'cal-mode') {
      mode = target.dataset.value;
      selectedDay = null;
      this.render(root);
      return true;
    }
    if (action === 'cal-prev' || action === 'cal-next') {
      const step = action === 'cal-next' ? 1 : -1;
      viewMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + step, 1);
      selectedDay = null;
      this.render(root);
      return true;
    }
    if (action === 'cal-today') {
      viewMonth = startOfMonth(new Date());
      selectedDay = isoOf(new Date());
      this.render(root);
      return true;
    }
    if (action === 'cal-day') {
      // Clicking the open day closes it, so the grid is never stuck expanded.
      selectedDay = selectedDay === target.dataset.day ? null : target.dataset.day;
      this.render(root);
      return true;
    }
    if (action === 'toggle-backlog') {
      showBacklog = !showBacklog;
      this.render(root);
      return true;
    }
    if (action === 'refresh-calendar') {
      toast('Refreshing air dates from TMDB…');
      await api.action('refresh-calendar');
      store.refresh();
      return true;
    }
    if (action === 'search-now') {
      toast('Searching trackers…');
      const result = await api.action('search-now');
      toast(`Searched ${result.searched ?? 0} item(s), grabbed ${result.grabbed ?? 0}`,
            result.grabbed ? 'ok' : '');
      store.refresh();
      return true;
    }
    if (action === 'search-media') {
      target.disabled = true;
      target.textContent = 'Searching…';
      const result = await api.searchMedia(Number(target.dataset.id));
      toast(searchOutcome(result), result.grabbed ? 'ok' : '');
      store.refresh();
      return true;
    }
    if (action === 'ignore-media') {
      await api.patchMedia(Number(target.dataset.id), { ignored: true });
      toast('Stopped following');
      store.refresh();
      return true;
    }
    if (action === 'mark-seen') {
      await api.markWatched([Number(target.dataset.id)]);
      toast('Marked as seen — future episodes still tracked', 'ok');
      store.refresh();
      return true;
    }
    if (action === 'seen-through') {
      const season = Number(target.dataset.season);
      const episode = Number(target.dataset.episode);
      if (!season) {
        toast('That item has no season to work back through', 'err');
        return true;
      }
      const result = await api.markMediaWatched(Number(target.dataset.id), {
        season, up_to_episode: episode || undefined,
      });
      toast(`Marked ${result.changed} episode(s) as seen`, 'ok');
      store.refresh();
      return true;
    }
    return false;
  },
};

// ---------------------------------------------------------------------------
// Month grid
// ---------------------------------------------------------------------------
function monthView() {
  const dated = store.upcoming.filter((item) => item.air_date);
  const undated = store.upcoming.length - dated.length;
  const byDay = groupBy(dated, (item) => String(item.air_date).slice(0, 10));
  const cells = monthCells(viewMonth);
  const todayIso = isoOf(new Date());
  const inMonth = cells.filter((d) => d.getMonth() === viewMonth.getMonth())
    .reduce((sum, d) => sum + (byDay.get(isoOf(d))?.length ?? 0), 0);

  return html`
    <div class="cal">
      <div class="cal__head">
        <div class="cal__month">
          ${viewMonth.toLocaleDateString(undefined, { month: 'long' })}
          <span>${viewMonth.getFullYear()}</span>
        </div>
        <div class="cal__nav">
          <button class="iconbtn" data-action="cal-prev" aria-label="Previous month">${icon('chevronLeft')}</button>
          <button class="iconbtn" data-action="cal-next" aria-label="Next month">${icon('chevronRight')}</button>
        </div>
        <button class="btn btn--sm btn--ghost" data-action="cal-today">Today</button>
        <div class="grow"></div>
        <span class="faint">
          ${inMonth} release${inMonth === 1 ? '' : 's'} this month${
            undated ? ` · ${undated} with no date yet` : ''}
        </span>
      </div>

      <div class="cal__dow">${DOW.map((day) => html`<span>${day}</span>`)}</div>
      <div class="cal__grid">
        ${cells.map((date, index) => dayCell(date, index, byDay, todayIso))}
      </div>

      ${selectedDay ? dayDetail(byDay.get(selectedDay) || []) : ''}
    </div>`;
}

function dayCell(date, index, byDay, todayIso) {
  const iso = isoOf(date);
  const items = byDay.get(iso) || [];
  const classes = ['daycell'];
  if (date.getMonth() !== viewMonth.getMonth()) classes.push('daycell--out');
  if (iso === todayIso) classes.push('daycell--today');
  if (items.length) classes.push('daycell--has');

  // An empty day is a div, not a disabled button: nothing to activate, and it
  // keeps the grid out of the tab order.
  if (!items.length) {
    return html`
      <div class="${classes.join(' ')}" style="--i:${index}">
        <span class="daycell__num">${date.getDate()}</span>
      </div>`;
  }

  const shown = items.slice(0, MAX_POSTERS);
  return html`
    <button class="${classes.join(' ')}" style="--i:${index}"
            data-action="cal-day" data-day="${iso}"
            aria-pressed="${selectedDay === iso}"
            aria-label="${items.length} release${items.length === 1 ? '' : 's'} on ${iso}">
      <span class="daycell__num">${date.getDate()}</span>
      <span class="daycell__count">${items.length}</span>
      <span class="daycell__posters">
        ${shown.map((item) => {
          const url = posterUrl(item.poster_path, 'w92');
          return url
            ? html`<img class="daycell__poster" src="${url}" alt="" loading="lazy" decoding="async">`
            : html`<span class="daycell__ph" title="${item.title}"></span>`;
        })}
        ${items.length > shown.length
          ? html`<span class="daycell__more">+${items.length - shown.length}</span>` : ''}
      </span>
    </button>`;
}

function dayDetail(items) {
  if (!items.length) return '';
  return html`
    <div class="cal__detail">
      <div class="daygroup__label">${dayLabel(selectedDay)} · ${items.length} release${
        items.length === 1 ? '' : 's'}</div>
      ${items.map(airingRow)}
    </div>`;
}

// ---------------------------------------------------------------------------
// Agenda
// ---------------------------------------------------------------------------
function agendaView() {
  const all = store.upcoming;
  const upcoming = all.filter((item) => !countdown(item.air_date).past);
  const backlog = all.filter((item) => countdown(item.air_date).past);

  return html`
    <div class="stack">
      <div class="filters">
        <button class="chip" data-action="toggle-backlog" aria-pressed="${showBacklog}">
          Show backlog <span class="faint">${backlog.length}</span>
        </button>
      </div>

      ${upcoming.length
        ? html`<div class="stack">
            <div class="sectionhead"><h2>Coming up</h2>
              <span class="sectionhead__note">${upcoming.length} tracked</span></div>
            ${dayGroups(upcoming, 'asc')}
          </div>`
        : card('Coming up', emptyState('◷', 'Nothing scheduled',
            'Add to your Plex watchlist, or start watching a series — rás follows it automatically.'))}

      ${showBacklog && backlog.length
        ? html`<div class="stack">
            <div class="sectionhead"><h2>Already aired, still missing</h2>
              <span class="sectionhead__note">${backlog.length} — searched on every pass, newest first</span></div>
            ${dayGroups(backlog.slice(0, 200), 'desc')}
          </div>`
        : ''}
    </div>`;
}

function dayGroups(items, direction) {
  const byDay = groupBy(items, (item) => (item.air_date || '').slice(0, 10) || 'unknown');
  const days = [...byDay.keys()].sort((a, b) => {
    if (a === 'unknown') return 1;
    if (b === 'unknown') return -1;
    return direction === 'asc' ? a.localeCompare(b) : b.localeCompare(a);
  });

  return html`${days.map((day) => html`
    <div class="daygroup">
      <div class="daygroup__label">
        ${day === 'unknown' ? 'Date to be announced' : dayLabel(day)}
        <span class="faint">
          ${byDay.get(day).length} item${byDay.get(day).length === 1 ? '' : 's'}
        </span>
      </div>
      ${byDay.get(day).map(airingRow)}
    </div>`)}`;
}

function airingRow(item) {
  const timer = countdown(item.air_date);
  const code = episodeCode(item.season, item.episode);
  const style = WANT_STATE_STYLE[item.state] || { label: item.state, cls: '' };
  return html`
    <div class="airing ${timer.past ? '' : 'airing--soon'}">
      ${poster(item.poster_path, item.title)}
      <div class="grow">
        <div class="trunc"><strong>${item.title}</strong>
          ${code ? html` <span class="faint mono">${code}</span>` : ''}</div>
        <div class="item__meta">
          <span class="pill ${style.cls}">${style.label}</span>
          ${item.episode_title ? html`<span class="faint trunc">${item.episode_title}</span>` : ''}
          ${item.search_attempts > 0
            ? html`<span class="faint">· ${item.search_attempts} search${item.search_attempts === 1 ? '' : 'es'}${
                     item.last_search_at ? `, last ${relativeTime(item.last_search_at)}` : ''}</span>`
            : ''}
        </div>
        ${item.reason && item.state === 'searching'
          ? html`<div class="faint" style="margin-top:5px">${item.reason}</div>`
          : ''}
      </div>
      <div class="item__side">
        <span class="countdown ${timer.past ? 'countdown--past' : ''}">${timer.text}</span>
        <div class="item__actions">
          <button class="btn btn--sm btn--ghost" data-action="mark-seen" data-id="${item.id}"
                  title="I have already watched this — stop looking for it, but keep following the show">
            Seen
          </button>
          <button class="btn btn--sm btn--ghost" data-action="seen-through" data-id="${item.media_id}"
                  data-season="${item.season ?? ''}" data-episode="${item.episode ?? ''}"
                  title="Mark this and every earlier episode of the season as watched">
            Seen through
          </button>
          <button class="btn btn--sm btn--ghost" data-action="search-media" data-id="${item.media_id}"
                  title="Search trackers for this title now">Search</button>
          <button class="btn btn--sm btn--ghost" data-action="ignore-media" data-id="${item.media_id}"
                  title="Stop following this title">Ignore</button>
        </div>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Dates. All local -- an air date is a calendar day, not an instant, so
// anything going via UTC lands on the wrong square either side of midnight.
// ---------------------------------------------------------------------------
function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function isoOf(date) {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

/** Whole weeks covering the month, Monday first, with no trailing empty row. */
function monthCells(month) {
  const first = startOfMonth(month);
  const offset = (first.getDay() + 6) % 7;          // getDay() is 0=Sunday
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const total = Math.ceil((offset + days) / 7) * 7;
  return Array.from({ length: total }, (_, i) =>
    new Date(month.getFullYear(), month.getMonth(), 1 - offset + i));
}
