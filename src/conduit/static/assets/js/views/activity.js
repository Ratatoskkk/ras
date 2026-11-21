import { api } from '../api.js';
import { card, emptyState, toast } from '../components.js';
import { store } from '../store.js';
import { clockTime, html, relativeTime, setHTML, titleCase } from '../util.js';

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR'];

let pane = 'events';
let level = 'INFO';
let category = '';
let events = [];
let logs = [];
let unsubscribe = null;
let pendingPaint = null;

/**
 * One repaint per frame, however many events land in it. A search pass can
 * publish dozens at once, and each one used to rebuild the whole timeline
 * and the task table with it.
 */
function schedulePaint(root) {
  if (pendingPaint) return;
  pendingPaint = requestAnimationFrame(() => {
    pendingPaint = null;
    if (root.isConnected) paint(root);
  });
}

export default {
  id: 'activity',
  title: 'Activity',
  icon: '⌁',
  badge: () => null,

  async render(root) {
    setHTML(root, shell(html`<div class="empty">Loading…</div>`));
    await reload();
    if (!root.isConnected) return;
    paint(root);

    // New events arrive over the WebSocket; prepend them without a refetch.
    unsubscribe?.();
    unsubscribe = store.on('activity', (evt) => {
      events.unshift({
        ts: new Date().toISOString(),
        level: evt.detail.level,
        category: evt.detail.category,
        message: evt.detail.message,
      });
      events = events.slice(0, 200);
      if (pane === 'events') schedulePaint(root);
    });
  },

  destroy() {
    unsubscribe?.();
    unsubscribe = null;
    if (pendingPaint) { cancelAnimationFrame(pendingPaint); pendingPaint = null; }
  },

  async onAction(action, target, root) {
    if (action === 'pane') { pane = target.dataset.value; await reload(); paint(root); return true; }
    if (action === 'level') { level = target.dataset.value; await reload(); paint(root); return true; }
    if (action === 'category') {
      category = target.dataset.value === category ? '' : target.dataset.value;
      await reload(); paint(root); return true;
    }
    if (action === 'reload') { await reload(); paint(root); toast('Refreshed'); return true; }
    if (action === 'run-task') {
      await api.runTask(target.dataset.name);
      toast(`Triggered ${target.dataset.name}`);
      return true;
    }
    if (action === 'toggle-task') {
      const enabled = target.dataset.enabled !== 'true';
      await api.setTask(target.dataset.name, enabled);
      toast(`${target.dataset.name} ${enabled ? 'enabled' : 'paused'}`);
      store.refresh();
      return true;
    }
    return false;
  },
};

async function reload() {
  try {
    if (pane === 'events') {
      events = await api.events({ limit: 200, category: category || undefined });
    } else {
      logs = await api.logs({ limit: 400, level: level === 'ALL' ? undefined : level });
    }
  } catch (error) {
    toast(error.message, 'err');
  }
}

function shell(body) {
  return html`
    <div class="filters">
      <button class="chip" data-action="pane" data-value="events" aria-pressed="${pane === 'events'}">
        Timeline
      </button>
      <button class="chip" data-action="pane" data-value="logs" aria-pressed="${pane === 'logs'}">
        Logs
      </button>
      <span class="filters__sep" aria-hidden="true"></span>
      ${pane === 'logs'
        ? html`${LEVELS.map((value) => html`
                 <button class="chip" data-action="level" data-value="${value}"
                         aria-pressed="${level === value}">${titleCase(value.toLowerCase())}</button>`)}`
        : html`${['grab', 'queue', 'download', 'search', 'watchlist', 'cleanup', 'task']
                  .map((value) => html`
                    <button class="chip" data-action="category" data-value="${value}"
                            aria-pressed="${category === value}">${titleCase(value)}</button>`)}`}
      <div class="grow"></div>
      <button class="btn btn--sm btn--ghost" data-action="reload">Refresh</button>
    </div>
    ${body}
    ${tasksCard()}`;
}

function paint(root) {
  const body = pane === 'events' ? eventsCard() : logsCard();
  setHTML(root, shell(body));
}

function eventsCard() {
  return card('Timeline',
    events.length
      ? html`<div class="list">
          ${events.map((e) => html`
            <div class="event event--${e.level}">
              <span class="event__time" title="${e.ts}">${relativeTime(e.ts)}</span>
              <span class="event__msg">
                <span class="event__cat">${e.category}</span>${e.message}
              </span>
            </div>`)}
        </div>`
      : emptyState('·', 'No events recorded yet'));
}

function logsCard() {
  return card('Application log',
    logs.length
      ? html`<div class="list" style="max-height:62vh;overflow:auto">
          ${[...logs].reverse().map((line) => html`
            <div class="logline logline--${line.level}">
              <span class="logline__time">${clockTime(line.ts)}</span>
              <span class="logline__lvl">${line.level}</span>
              <span>${line.message}${line.context
                ? html` <span class="faint">${Object.entries(line.context)
                    .map(([k, v]) => `${k}=${v}`).join(' ')}</span>`
                : ''}</span>
            </div>`)}
        </div>`
      : emptyState('·', 'Nothing logged at this level'));
}

function tasksCard() {
  const tasks = store.tasks;
  return card('Background tasks',
    tasks.length
      ? html`<div class="tablewrap">
          <table class="table">
            <thead><tr>
              <th>Task</th><th>What it does</th><th>Last run</th><th>Duration</th>
              <th>Next</th><th>Runs</th><th></th>
            </tr></thead>
            <tbody>
              ${tasks.map((task) => html`
                <tr>
                  <td>
                    <span class="mono">${task.name}</span>
                    ${task.running ? html`<span class="pill pill--info" style="margin-left:6px">running</span>` : ''}
                    ${!task.enabled ? html`<span class="pill" style="margin-left:6px">paused</span>` : ''}
                  </td>
                  <td class="faint">${task.description}</td>
                  <td class="faint">${task.last_finish ? relativeTime(task.last_finish) : '—'}</td>
                  <td class="mono">${task.last_duration ? `${task.last_duration.toFixed(2)}s` : '—'}</td>
                  <td class="mono">${task.seconds_until_next !== null && task.seconds_until_next !== undefined
                      ? `${task.seconds_until_next}s` : '—'}</td>
                  <td class="mono">${task.run_count}${task.error_count
                      ? html` <span style="color:var(--err)">/${task.error_count} failed</span>` : ''}</td>
                  <td>
                    <div class="item__actions">
                      <button class="btn btn--sm" data-action="run-task" data-name="${task.name}">Run</button>
                      <button class="btn btn--sm btn--ghost" data-action="toggle-task"
                              data-name="${task.name}" data-enabled="${task.enabled}">
                        ${task.enabled ? 'Pause' : 'Resume'}
                      </button>
                    </div>
                  </td>
                </tr>
                ${task.last_error ? html`
                  <tr><td colspan="7" style="color:var(--err);font:12px/1.5 var(--mono)">
                    ${titleCase(task.name)}: ${task.last_error}
                  </td></tr>` : ''}`)}
            </tbody>
          </table>
        </div>`
      : emptyState('·', 'No tasks registered'));
}
