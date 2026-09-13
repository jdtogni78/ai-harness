/* review.js — Alpine component for the report-v2 review renderer.
 * Reads the embedded #report-data JSON blob (req 4) and drives a multi-page,
 * one-page-at-a-time sidebar app (req 1). No network, no build step.
 * All view logic lives here so review.template.html stays declarative (req 2). */

function loadModel() {
  // The single source of truth: the embedded JSON blob. If it is malformed we
  // fail LOUD (a blank page is worse than an error the author can see and fix).
  const el = document.getElementById('report-data');
  try {
    return JSON.parse(el.textContent);
  } catch (e) {
    document.body.innerHTML =
      '<pre style="color:#f85149;padding:24px;font:14px monospace">'
      + 'report-data JSON failed to parse — the report cannot render.\n\n'
      + String(e) + '</pre>';
    throw e;
  }
}

const STATUS_ORDER = { blocked: 0, 'in-progress': 1, todo: 2, done: 3 };
const STATUS_LABEL = { blocked: 'blocked', 'in-progress': 'in progress', todo: 'to-do', done: 'done' };

function reportApp() {
  const model = loadModel();

  // Sidebar pages. `badge` closures compute a live count shown on the right.
  const pages = [
    { id: 'progress',   label: 'Progress',      icon: '▤', badge: () => (model.tasks || []).length },
    { id: 'todos',      label: 'To-dos',        icon: '☑', badge: () => openTodoCount(model) },
    { id: 'resources',  label: 'Resources',     icon: '🔗', badge: () => countResources(model) },
    { id: 'validation', label: 'Validation',    icon: '✓', badge: () => verifiedCount(model) },
    { id: 'facts',      label: 'Facts / Memory', icon: '🧠', badge: () => (model.facts || []).length },
    { id: 'timeline',   label: 'Timeline',      icon: '↧', badge: () => (model.timeline || []).length },
    { id: 'data',       label: 'Data',          icon: '{ }', badge: () => '' },
  ];

  return {
    model,
    pages,
    route: 'progress',
    toast: '',

    init() {
      // Route from the URL hash so deep-links + print keep a stable entry point.
      const fromHash = () => {
        const h = (location.hash || '').replace(/^#\/?/, '');
        if (pages.some(p => p.id === h)) this.route = h;
      };
      fromHash();
      window.addEventListener('hashchange', fromHash);
      // Keyboard nav (spec req 1): [ ] and j/k move between pages.
      window.addEventListener('keydown', (e) => {
        if (e.target.matches('input,textarea')) return;
        if (e.key === ']' || e.key === 'j') this.step(1);
        else if (e.key === '[' || e.key === 'k') this.step(-1);
        else if (e.key === 'p') { e.preventDefault(); window.print(); }
      });
    },

    go(id) {
      this.route = id;
      try { history.replaceState(null, '', '#/' + id); } catch (_) { location.hash = '/' + id; }
    },
    step(d) {
      const i = pages.findIndex(p => p.id === this.route);
      this.go(pages[Math.max(0, Math.min(pages.length - 1, i + d))].id);
    },

    // ---- derived views -------------------------------------------------
    statusLabel: (s) => STATUS_LABEL[s] || s,

    tasksWorstFirst() {
      return [...(model.tasks || [])].sort(
        (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9)
      );
    },
    tasksWithTodos() {
      return (model.tasks || []).filter(t => (t.todos || []).length);
    },
    tasksForValidation() {
      // Worst-first here too: unverified/blocked work should be seen first.
      return this.tasksWorstFirst();
    },
    resourceGroups() {
      const groups = {};
      const push = (r, from) => {
        const k = r.kind || 'other';
        (groups[k] = groups[k] || []).push({ ...r, from });
      };
      (model.resources || []).forEach(r => push(r, null));
      (model.tasks || []).forEach(t => (t.resources || []).forEach(r => push(r, t.title)));
      return Object.keys(groups).sort().map(k => ({ kind: k, items: groups[k] }));
    },

    // ---- data export ---------------------------------------------------
    prettyJSON() { return JSON.stringify(model, null, 2); },
    copyJSON() {
      const txt = this.prettyJSON();
      const done = () => this.flash('Copied JSON to clipboard');
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done, () => this.fallbackCopy(txt, done));
      } else { this.fallbackCopy(txt, done); }
    },
    fallbackCopy(txt, done) {
      const ta = document.createElement('textarea');
      ta.value = txt; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } finally { ta.remove(); }
    },
    downloadJSON() {
      const blob = new Blob([this.prettyJSON()], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (model.meta.slug || 'report') + '.json';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      this.flash('Downloaded ' + a.download);
    },
    flash(msg) { this.toast = msg; setTimeout(() => { this.toast = ''; }, 1800); },
  };
}

// ---- badge helpers (pure) ----------------------------------------------
function openTodoCount(m) {
  let n = 0;
  (m.tasks || []).forEach(t => (t.todos || []).forEach(c => { if (!c.done) n++; }));
  return n;
}
function countResources(m) {
  let n = (m.resources || []).length;
  (m.tasks || []).forEach(t => { n += (t.resources || []).length; });
  return n;
}
function verifiedCount(m) {
  let n = 0;
  (m.tasks || []).forEach(t => (t.evidence || []).forEach(e => { if (e.verified) n++; }));
  return n;
}

window.reportApp = reportApp;
document.addEventListener('alpine:init', () => { /* component registered globally above */ });
