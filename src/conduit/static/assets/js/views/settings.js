import { api } from '../api.js';
import { card, emptyState, toast } from '../components.js';
import { store } from '../store.js';
import { html, setHTML, titleCase } from '../util.js';

let config = null;
let paths = null;
let authMode = '';
let dirty = false;

// Field metadata: [key, label, type, hint]
const POLICY_FIELDS = [
  ['max_active_downloads', 'Concurrent downloads', 'number', 'How many torrents may download at once.'],
  ['approval_size_threshold_gb', 'Approval gate (GB)', 'number', 'Anything larger waits for you.'],
  ['auto_approve_below_gb', 'Auto-approve below (GB)', 'number', '0 disables this shortcut.'],
  ['reserve_free_space_gb', 'Keep free (GB)', 'number', 'Space rás refuses to consume.'],
  ['size_headroom_percent', 'Size headroom (%)', 'number', 'Slack added to every space check.'],
  ['season_pack_min_missing', 'Pack threshold', 'number', 'Missing episodes before a season pack is preferred.'],
  ['sequential_lead_episodes', 'Next-season lead', 'number', 'Episodes from the end of a season that trigger the next one.'],
  ['max_search_attempts', 'Misses before daily checks', 'number', 'After the fresh window, slow to daily checks after this many misses.'],
  ['min_seed_days', 'Minimum seed days', 'number', 'Nothing is offered for reclaim until it has seeded this long.'],
  ['min_seed_ratio', 'Or minimum ratio', 'number', '0 disables — time only, which is the safe default.'],
  ['torrent_category', 'qBittorrent category', 'text',
   'Category applied to every torrent added. Changing it orphans torrents already tagged.'],
  ['torrent_tag_prefix', 'Tag prefix', 'text',
   'Identifies our torrents in the client. Changing it loses track of in-flight downloads.'],
];

const POLICY_TOGGLES = [
  ['require_approval_for_everything',
   'Approve every grab manually — nothing reaches qBittorrent on its own'],
  ['require_approval_for_season_packs', 'Season packs need approval'],
  ['require_approval_for_multi_season', 'Multi-season grabs need approval'],
  ['prefer_season_packs', 'Prefer season packs over singles'],
  ['auto_remove_from_watchlist', 'Remove items from the Plex watchlist once handled'],
  ['trigger_plex_refresh', 'Tell Plex to rescan when a download finishes'],
  ['skip_watched_seasons', 'Skip seasons you have already watched'],
  ['assume_prior_seasons_watched',
   'Assume earlier seasons are watched — if you are on season 3, seasons 1–2 count as seen'],
  ['sequential_seasons',
   'Fetch the next season just before you need it — as you near the end of a season, the following one is queued'],
  ['allow_delete_before_seed_goal',
   'Allow reclaiming before the seed requirement is met (risks a hit-and-run)'],
  ['dry_run', 'Dry run — decide everything, send nothing'],
];

const INTERVAL_FIELDS = [
  ['watchlist_sync', 'Watchlist sync'],
  ['library_index', 'Library index'],
  ['calendar_refresh', 'Calendar refresh'],
  ['release_poll', 'Full tracker search'],
  ['fresh_release_poll', 'Fresh-release search'],
  ['queue_dispatch', 'Queue dispatch'],
  ['download_monitor', 'Download monitor'],
  ['watched_scan', 'Watched-state scan'],
  ['housekeeping', 'Housekeeping'],
];

const CALENDAR_FIELDS = [
  ['fresh_window_days', 'Fresh window (days)', 'Recent airings get the aggressive poll.'],
  ['give_up_days_tv', 'Slow checks: episodes (days)', 'After this many days of searching, check once daily.'],
  ['give_up_days_movie', 'Slow checks: films (days)', 'After this many days of searching, check once daily.'],
  ['pre_air_lead_hours', 'Search lead (hours)', 'Start looking this long before the air date.'],
  ['max_seasons_back', 'Seasons to look back', '0 means every season.'],
];

export default {
  id: 'settings',
  title: 'Settings',
  icon: '⚙',
  badge: () => null,

  async render(root) {
    if (!config) {
      setHTML(root, card('Settings', emptyState('…', 'Loading…')));
      try {
        const payload = await api.config();
        config = payload.config;
        paths = payload.paths;
        authMode = payload.auth_mode;
      } catch (error) {
        setHTML(root, card('Settings', emptyState('⚠', error.message)));
        return;
      }
    }
    paint(root);
  },

  async onAction(action, target, root) {
    if (action === 'save') {
      target.disabled = true;
      try {
        const payload = await api.saveConfig(config);
        config = payload.config;
        dirty = false;
        toast('Settings saved', 'ok');
        store.refresh();
        paint(root);
      } catch (error) {
        toast(error.message, 'err');
        target.disabled = false;
      }
      return true;
    }
    if (action === 'reload') {
      config = null;
      dirty = false;
      await this.render(root);
      return true;
    }
    if (action === 'add-indexer') {
      config.indexers.push({
        name: 'New tracker', type: 'unit3d', base_url: 'https://', api_key_env: 'MYTRACKER_API_KEY',
        enabled: false, priority: config.indexers.length + 1, rate_limit_per_minute: 30,
        timeout_seconds: 20, score_bonus: 0, verify_ssl: true,
      });
      dirty = true;
      paint(root);
      return true;
    }
    if (action === 'remove-indexer') {
      config.indexers.splice(Number(target.dataset.index), 1);
      dirty = true;
      paint(root);
      return true;
    }
    if (action === 'format-profiles') {
      const field = root.querySelector('[data-field="profiles_json"]');
      try {
        field.value = JSON.stringify(JSON.parse(field.value), null, 2);
        toast('Formatted');
      } catch (error) {
        toast(`Not valid JSON: ${error.message}`, 'err');
      }
      return true;
    }
    return false;
  },

  onInput(action, target) {
    if (action !== 'field') return false;
    const path = target.dataset.field;
    dirty = true;

    if (path === 'profiles_json') {
      try {
        config.profiles = JSON.parse(target.value);
        target.style.borderColor = '';
      } catch {
        target.style.borderColor = 'var(--err)';
      }
      return true;
    }

    let value;
    if (target.type === 'checkbox') value = target.checked;
    else if (target.type === 'number') value = Number(target.value);
    else value = target.value;

    const segments = path.split('.');
    let node = config;
    for (const key of segments.slice(0, -1)) node = node[key];
    node[segments.at(-1)] = value;

    const banner = document.querySelector('#save-hint');
    if (banner) banner.textContent = 'Unsaved changes';
    return true;
  },

  invalidate() { config = null; dirty = false; },
};

function paint(root) {
  setHTML(root, html`
    <div class="settings-savebar">
      <div class="settings-savebar__location">
        <span class="muted">Behaviour lives in </span>
        <span class="mono settings-savebar__path">${paths?.config_file || 'conduit.toml'}</span>
        <span class="faint" id="save-hint" style="margin-left:10px">
          ${dirty ? 'Unsaved changes' : ''}
        </span>
      </div>
      <div class="settings-savebar__actions">
        <button class="btn btn--ghost btn--sm" data-action="reload">Discard</button>
        <button class="btn btn--primary" data-action="save">Save settings</button>
      </div>
    </div>

    ${card('Policy', html`
      <div class="card__body stack">
        <div class="formgrid">
          ${POLICY_FIELDS.map(([key, label, type, hint]) => field(`policy.${key}`, label, type,
            config.policy[key], hint))}
        </div>
        <div class="stack" style="gap:9px">
          ${POLICY_TOGGLES.map(([key, label]) => toggle(`policy.${key}`, label, config.policy[key]))}
        </div>
      </div>`)}

    ${card('Quality profiles', html`
      <div class="card__body stack">
        <div class="formgrid">
          ${select('default_profile', 'Default profile', config.default_profile,
                   config.profiles.map((p) => p.name))}
          ${select('movie_profile', 'Films use', config.movie_profile,
                   config.profiles.map((p) => p.name))}
          ${select('tv_profile', 'Series use', config.tv_profile,
                   config.profiles.map((p) => p.name))}
        </div>
        <div class="field">
          <span class="field__label">Profile definitions</span>
          <span class="field__hint">
            Each attribute value carries a score; a release must clear
            <code>min_score</code>, avoid every <code>blocked_terms</code> entry and fit the size
            window. Anything not listed under <code>resolutions</code> or <code>sources</code> is
            rejected outright.
          </span>
          <textarea data-action="field" data-field="profiles_json" aria-label="Profile definitions" spellcheck="false"
                    style="min-height:320px">${JSON.stringify(config.profiles, null, 2)}</textarea>
        </div>
      </div>`,
      html`<button class="btn btn--sm btn--ghost" data-action="format-profiles">Format JSON</button>`)}

    ${card('Trackers', html`
      <div class="card__body stack">
        ${config.indexers.length
          ? config.indexers.map(indexerBlock)
          : html`<p class="muted">No trackers configured.</p>`}
      </div>`,
      html`<button class="btn btn--sm" data-action="add-indexer">Add tracker</button>`)}

    ${card('Timing', html`
      <div class="card__body">
        <p class="muted" style="margin-top:0">All values in seconds.</p>
        <div class="formgrid">
          ${INTERVAL_FIELDS.map(([key, label]) =>
            field(`intervals.${key}`, label, 'number', config.intervals[key]))}
        </div>
      </div>`)}

    ${card('Release tracking', html`
      <div class="card__body stack">
        <div class="formgrid">
          ${select('policy.backlog_mode', 'Already-aired backlog', config.policy.backlog_mode,
                   ['all', 'current_season', 'upcoming_only'])}
          ${CALENDAR_FIELDS.map(([key, label, hint]) =>
            field(`calendar.${key}`, label, 'number', config.calendar[key], hint))}
        </div>
        <p class="field__hint" style="margin:0">
          <strong>all</strong> chases every missing episode, however old.
          <strong>current_season</strong> finishes the season you are on, then keeps up.
          <strong>upcoming_only</strong> takes nothing that aired more than the fresh
          window ago — "just keep me current".
        </p>
        ${toggle('calendar.track_watched_shows',
                 'Automatically follow series you are watching in Plex',
                 config.calendar.track_watched_shows)}
      </div>`)}

    ${card('Environment', html`
      <div class="card__body stack" style="gap:8px">
        <p class="muted" style="margin:0">
          Credentials and paths come from <span class="mono">.env</span> and are deliberately not
          editable here.
        </p>
        <div class="row"><span class="muted grow">Database</span>
          <span class="mono trunc">${paths?.database}</span></div>
        <div class="row"><span class="muted grow">Download roots</span>
          <span class="mono trunc">${(paths?.download_dirs || []).join('  ·  ')}</span></div>
        <div class="row"><span class="muted grow">Access mode</span>
          <span class="mono">${authMode}</span></div>
      </div>`)}
  `);
}

function field(path, label, type, value, hint = '') {
  return html`
    <label class="field">
      <span class="field__label">${label}</span>
      <input type="${type}" data-action="field" data-field="${path}" value="${value ?? ''}"
             ${type === 'number' ? 'step="any"' : ''}>
      ${hint ? html`<span class="field__hint">${hint}</span>` : ''}
    </label>`;
}

function select(path, label, value, options) {
  return html`
    <label class="field">
      <span class="field__label">${label}</span>
      <select data-action="field" data-field="${path}">
        ${options.map((option) => html`
          <option value="${option}" ${option === value ? 'selected' : ''}>${option}</option>`)}
      </select>
    </label>`;
}

function toggle(path, label, checked) {
  return html`
    <label class="switch">
      <input type="checkbox" data-action="field" data-field="${path}" ${checked ? 'checked' : ''}>
      <span class="switch__track"></span>
      <span>${label}</span>
    </label>`;
}

function indexerBlock(indexer, index) {
  return html`
    <section class="indexer-block">
      <div class="card__head">
        <h2>${indexer.name || 'Tracker'}</h2>
        <div class="card__actions">
          ${toggle(`indexers.${index}.enabled`, 'Enabled', indexer.enabled)}
          <button class="btn btn--sm btn--danger" data-action="remove-indexer" data-index="${index}">
            Remove
          </button>
        </div>
      </div>
      <div class="card__body formgrid">
        ${field(`indexers.${index}.name`, 'Name', 'text', indexer.name)}
        ${field(`indexers.${index}.base_url`, 'Base URL', 'text', indexer.base_url)}
        ${field(`indexers.${index}.api_key_env`, 'API key env var', 'text', indexer.api_key_env,
                'Name of the variable in .env holding this tracker’s key.')}
        ${field(`indexers.${index}.priority`, 'Priority', 'number', indexer.priority)}
        ${field(`indexers.${index}.rate_limit_per_minute`, 'Requests / minute', 'number',
                indexer.rate_limit_per_minute)}
        ${field(`indexers.${index}.score_bonus`, 'Score bonus', 'number', indexer.score_bonus,
                'Added to every release from this tracker.')}
      </div>
    </section>`;
}

export { titleCase };
