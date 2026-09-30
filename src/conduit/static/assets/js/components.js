// Reusable markup fragments shared by the views.

import { bytes, duration, episodeCode, esc, html, percent, posterUrl, speed, titleCase } from './util.js';

export const STATE_STYLE = {
  downloading:      { label: 'Downloading',  cls: 'pill--info' },
  pending_approval: { label: 'Needs you',    cls: 'pill--warn' },
  queued:           { label: 'Queued',       cls: 'pill--accent' },
  completed:        { label: 'Complete',     cls: 'pill--ok' },
  failed:           { label: 'Failed',       cls: 'pill--err' },
  denied:           { label: 'Denied',       cls: '' },
  no_space:         { label: 'No space',     cls: 'pill--err' },
  cancelled:        { label: 'Cancelled',    cls: '' },
};

export const WANT_STATE_STYLE = {
  waiting:     { label: 'Waiting',     cls: '' },
  searching:   { label: 'Searching',   cls: 'pill--accent' },
  grabbed:     { label: 'Grabbed',     cls: 'pill--info' },
  downloaded:  { label: 'In library',  cls: 'pill--ok' },
  unavailable: { label: 'Checking daily', cls: 'pill--info' },
  ignored:     { label: 'Out of scope', cls: '' },
  watched:     { label: 'Seen',        cls: 'pill--ok' },
};

export function stateBadge(state) {
  const style = STATE_STYLE[state] || { label: titleCase(state), cls: '' };
  return html`<span class="pill ${style.cls}">${style.label}</span>`;
}

export function poster(path, alt = '') {
  const url = posterUrl(path);
  return url
    ? html`<img class="poster" src="${url}" alt="${alt}" loading="lazy" decoding="async">`
    : html`<div class="poster poster--ph" aria-hidden="true"></div>`;
}

export function qualityTags(item) {
  const tags = [];
  if (item.resolution) tags.push(html`<span class="tag tag--res">${item.resolution}</span>`);
  if (item.source) tags.push(html`<span class="tag">${item.source}</span>`);
  if (item.dynamic_range && item.dynamic_range !== 'sdr') {
    tags.push(html`<span class="tag tag--hdr">${item.dynamic_range.replace(/_/g, '+')}</span>`);
  }
  if (item.audio) tags.push(html`<span class="tag">${item.audio.replace(/_/g, ' ')}</span>`);
  if (item.release_group) tags.push(html`<span class="tag">${item.release_group}</span>`);
  return tags;
}

export function progressBar(fraction, variant = '') {
  const width = Math.max(0, Math.min(1, Number(fraction) || 0)) * 100;
  return html`<div class="bar"><div class="bar__fill ${variant}" style="width:${width.toFixed(2)}%"></div></div>`;
}

export function emptyState(_icon, title, hint = '') {
  return html`
    <div class="empty">
      <div>${title}</div>
      ${hint ? html`<div class="faint" style="margin-top:4px">${hint}</div>` : ''}
    </div>`;
}

export function card(title, body, actions = '') {
  return html`
    <section class="card">
      <header class="card__head">
        <h2>${title}</h2>
        ${actions ? html`<div class="card__actions">${actions}</div>` : ''}
      </header>
      ${body}
    </section>`;
}

export function switchControl(name, checked, label, extra = '') {
  return html`
    <label class="switch">
      <input type="checkbox" data-field="${name}" ${checked ? 'checked' : ''} ${extra}>
      <span class="switch__track"></span>
      <span>${label}</span>
    </label>`;
}

/** One download row, used by the dashboard and the queue. */
export function downloadRow(item, live, { showActions = true } = {}) {
  const isActive = item.state === 'downloading';
  const fraction = live ? live.progress : item.progress;
  const variant = item.state === 'completed' ? 'bar__fill--done'
    : isActive ? '' : 'bar__fill--idle';

  const episodes = item.is_season_pack
    ? `Season ${item.season}`
    : episodeCode(item.season, item.episode_from);

  return html`
    <div class="item" data-download="${item.id}">
      ${poster(item.poster_path, item.title)}
      <div class="grow">
        <div class="item__title trunc">${item.title}</div>
        <div class="item__meta">
          ${stateBadge(item.state)}
          ${episodes ? html`<span class="tag">${episodes}</span>` : ''}
          ${qualityTags(item)}
          <span class="faint">${bytes(item.size_bytes)}</span>
          ${item.indexer ? html`<span class="faint">· ${item.indexer}</span>` : ''}
          ${item.score ? html`<span class="faint">· score ${item.score}</span>` : ''}
        </div>
        ${item.release_name ? html`<div class="item__release trunc">${item.release_name}</div>` : ''}
        ${item.error ? html`<div class="item__release" style="color:var(--err)">${item.error}</div>` : ''}
        ${(isActive || item.state === 'completed')
          ? progressBar(fraction, variant)
          : ''}
      </div>
      <div class="item__side">
        ${isActive
          ? html`<span class="mono" data-progress-text="${item.id}">
                   ${percent(fraction)} · ${speed(live ? live.speed_bps : item.speed_bps)}
                 </span>
                 <span class="faint mono" data-eta-text="${item.id}">
                   ${(live ? live.eta_seconds : item.eta_seconds) >= 0
                      ? duration(live ? live.eta_seconds : item.eta_seconds) + ' left'
                      : 'estimating…'}
                 </span>`
          : ''}
        ${showActions ? rowActions(item) : ''}
      </div>
    </div>`;
}

function rowActions(item) {
  const buttons = [];
  if (item.state === 'pending_approval') {
    buttons.push(html`<button class="btn btn--sm btn--primary" data-action="approve" data-id="${item.id}">Approve</button>`);
    buttons.push(html`<button class="btn btn--sm btn--danger" data-action="deny" data-id="${item.id}">Deny</button>`);
  }
  if (item.state === 'failed' || item.state === 'no_space' || item.state === 'cancelled') {
    buttons.push(html`<button class="btn btn--sm" data-action="retry" data-id="${item.id}">Retry</button>`);
  }
  if (['downloading', 'completed', 'queued'].includes(item.state)) {
    buttons.push(html`<button class="btn btn--sm btn--ghost" data-action="remove" data-id="${item.id}" title="Remove from client and disk">Remove</button>`);
  }
  return buttons.length ? html`<div class="item__actions">${buttons}</div>` : '';
}

/**
 * Update only the moving parts of a download row. Called every animation
 * frame while something is downloading, so it must not touch innerHTML.
 */
export function paintProgress(root, item, live) {
  const row = root.querySelector(`[data-download="${item.id}"]`);
  if (!row) return;
  const fill = row.querySelector('.bar__fill');
  if (fill) fill.style.width = `${(Math.min(1, live.progress) * 100).toFixed(2)}%`;
  const text = row.querySelector(`[data-progress-text="${item.id}"]`);
  if (text) text.textContent = `${percent(live.progress)} · ${speed(live.speed_bps)}`;
  const eta = row.querySelector(`[data-eta-text="${item.id}"]`);
  if (eta) {
    eta.textContent = live.eta_seconds >= 0 ? `${duration(live.eta_seconds)} left` : 'estimating…';
  }
}

/**
 * Say what a manual search actually did.
 *
 * "Nothing matched your quality profile" used to be shown for every empty
 * result, including the case where nothing was even searched for because the
 * title had no outstanding episodes. That is a different problem with a
 * different fix, and blaming the profile hides it.
 */
export function searchOutcome(result) {
  if (result.grabbed) return `Grabbed ${result.grabbed} release(s)`;
  if (result.reason) return result.reason;
  if (!result.searched) return 'Nothing to search for — no episodes are outstanding';
  return `Searched ${result.searched} item(s) — nothing qualified. `
       + 'Releases shows the score for each candidate.';
}

export function toast(message, kind = '') {
  const host = document.getElementById('toasts');
  const node = document.createElement('div');
  node.className = `toast ${kind ? `toast--${kind}` : ''}`;
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .3s, transform .3s';
    node.style.opacity = '0';
    node.style.transform = 'translateY(6px)';
    setTimeout(() => node.remove(), 320);
  }, kind === 'err' ? 6000 : 3500);
}

export function openModal(title, bodyHtml) {
  const modal = document.getElementById('modal');
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML =
    typeof bodyHtml === 'string' ? bodyHtml : bodyHtml.value;
  modal.hidden = false;
  return modal;
}

export function closeModal() {
  document.getElementById('modal').hidden = true;
}

export { esc };
