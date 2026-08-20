/* Agent Lab — pick a model, run a task, watch the loop. */
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
};
const post = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body || {}) });

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
const bytes = (n) => n < 1024 ? `${n} B`
  : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const clip = (s, n) => (s = String(s ?? ''), s.length > n ? s.slice(0, n) + '…' : s);
const ago = (ts) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

/* One drawn set at one weight, replacing the emoji that labelled these rows.
   Emoji are rendered by the OS in its own colours and drawing style, so a
   column of them reads as a sticker sheet pasted onto a monochrome interface,
   and the same file looks different on every machine. These are strokes in
   currentColor, so they inherit the row's own ink and both themes.
   Geometry matches the folder glyph already in the topbar: a 16 unit box at
   1.3 stroke, round caps and joins. */
const ICONS = {
  folder: ['M1.5 4.2A1.2 1.2 0 0 1 2.7 3h3.1l1.4 1.6h5.1a1.2 1.2 0 0 1 1.2 1.2v6A1.2 1.2 0 0 1 12.3 13H2.7a1.2 1.2 0 0 1-1.2-1.2z'],
  inbox: ['M2.4 9.4h2.7l.9 1.6h4l.9-1.6h2.7',
          'M2.4 9.4 4.1 3.9a1.2 1.2 0 0 1 1.1-.9h5.6a1.2 1.2 0 0 1 1.1.9l1.7 5.5v2.3a1.2 1.2 0 0 1-1.2 1.2H3.6a1.2 1.2 0 0 1-1.2-1.2z'],
  calendar: ['M2.2 5.4A1.2 1.2 0 0 1 3.4 4.2h9.2a1.2 1.2 0 0 1 1.2 1.2v7.4a1.2 1.2 0 0 1-1.2 1.2H3.4a1.2 1.2 0 0 1-1.2-1.2z',
             'M2.2 7.3h11.6', 'M5.4 2.6v2.3', 'M10.6 2.6v2.3'],
  chat: ['M13.6 8.3c0 2.5-2.5 4.6-5.6 4.6-.7 0-1.4-.1-2-.3l-3 1.1 1-2.4a4.4 4.4 0 0 1-1.6-3C2.4 5.8 4.9 3.7 8 3.7s5.6 2.1 5.6 4.6z'],
  clock: ['M8 2.9a5.1 5.1 0 1 1 0 10.2 5.1 5.1 0 0 1 0-10.2z', 'M8 5.5V8l1.9 1.4'],
  send: ['M13.9 2.6 7.3 9.2', 'M13.9 2.6 9.7 13.9 7.3 9.2 2.6 6.8z'],
  layers: ['M8 2.5 14 5.4 8 8.3 2 5.4z', 'M2 8.5 8 11.4l6-2.9', 'M2 11.2 8 14.1l6-2.9'],
  drive: ['M2.2 6.1a1.2 1.2 0 0 1 1.2-1.2h9.2a1.2 1.2 0 0 1 1.2 1.2v5.7a1.2 1.2 0 0 1-1.2 1.2H3.4a1.2 1.2 0 0 1-1.2-1.2z',
          'M2.2 9.4h11.6', 'M11.1 11.3h.7'],
  log: ['M3.2 4.6h1.1', 'M3.2 8h1.1', 'M3.2 11.4h1.1',
        'M6.6 4.6h6.2', 'M6.6 8h6.2', 'M6.6 11.4h6.2'],
};

const SVG_NS = 'http://www.w3.org/2000/svg';

function icon(name, size = 15) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.3');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of ICONS[name] || []) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

/* A call that changes the world, rather than reading it. The dot on the
   timeline is green for these and blue for a read, so the shape of a run is
   legible before a word of it is read. */
const MUTATORS = new Set([
  'send_email', 'add_event', 'send_message', 'set_reminder', 'save_memory',
  'create_presentation', 'create_spreadsheet',
  'write_file', 'append_file', 'delete_path', 'move_path', 'run_command',
]);

/* Which argument names the file a tool produced. Office files land in the
   agent's workspace, which is what /api/preview can read, so those get a real
   preview pane. write_file and append_file go to the user's own folder: they
   belong in the touched strip, but there is nothing to render for them. */
const ARTIFACT_ARG = { create_presentation: 'filename', create_spreadsheet: 'filename' };
const TOUCH_ARG = { ...ARTIFACT_ARG, write_file: 'path', append_file: 'path' };

const S = {
  agents: [], agent: null, ws: null, run: null, es: null,
  call: null, t0: 0, timer: null, seen: {}, first: true,
  thread: null, resumed: false,
  open: new Set(['files', 'inbox', 'calendar']),
};

/* ------------------------------------------------------------- models --- */

async function loadAgents(keep) {
  const data = await api('/api/agents');
  S.agents = data.agents;
  S.available = data.available || [];
  $('meter-ollama').className = 'meter dotmeter ' + (data.ollama ? 'up' : 'down');
  $('meter-ollama').querySelector('.label').textContent =
    data.ollama ? 'Ollama running' : 'Ollama not running';
  renderPresets(data.presets);
  renderAgents();
  renderModels(data.installed_models);
  if (!keep) {
    const pick = S.agents.find((a) => a.installed) || S.agents[0];
    if (pick) selectAgent(pick.id);
  }
}

/* The model tag is what a person picks by, so it leads. The folder label ("8B")
   was the headline and the tag was supporting text under it, which had the
   naming backwards: every folder is named after its model, so the folder label
   only repeats the tag in a vaguer form.
   Below it, the model's own description, generated from openrouter into
   model_catalog.json and shipped, so describing a model needs no network. */
function agentRow(a) {
  const on = a.id === S.agent;
  const cat = a.catalog || {};
  const card = el('button', 'agent' + (on ? ' on' : ''));
  card.type = 'button';
  card.setAttribute('role', 'radio');
  card.setAttribute('aria-checked', on ? 'true' : 'false');
  card.onclick = () => selectAgent(a.id);

  const head = el('div', 'agent-head');
  head.append(el('span', 'agent-name', a.model));
  if (!a.installed) head.append(el('span', 'agent-flag', 'not installed'));
  else if (a.runs) head.append(el('span', 'agent-trail', `${a.runs} run${a.runs === 1 ? '' : 's'}`));
  card.append(head);

  if (cat.title) {
    card.append(el('div', 'agent-support',
      [cat.vendor, cat.title].filter(Boolean).join(' ')));
  }
  card.append(el('div', 'agent-desc', cat.description || a.blurb || ''));

  const meta = [
    cat.context ? `${Math.round(cat.context / 1024)}k context` : null,
    a.speed,
    a.profile ? a.profile.label : null,
    `${a.files} file${a.files === 1 ? '' : 's'}`,
    `${a.memories} learned`,
  ].filter(Boolean).join('  ·  ');
  card.append(el('div', 'agent-meta', meta));

  if (!a.installed) card.append(downloadRow(a, cat));
  return card;
}

/* The command to run, one click to copy, plus a link to the model's own page.
   There used to be a Download button here that posted to the local ollama's
   /api/pull and streamed progress in place. It was removed: it only works
   against a real ollama, and anything else answering on that port (an
   OpenAI-compatible proxy, say) returns 404, so the row's reward for a click
   was a raw HTTPError quoting a 127.0.0.1 URL at someone who cannot act on it.
   The command works on every machine and says exactly what it will do. */
function downloadRow(a, cat) {
  const row = el('div', 'agent-get');
  const cmd = cat.pull || `ollama pull ${a.model}`;

  const copy = el('button', 'cmd', '');
  copy.type = 'button';
  copy.title = 'Copy this command';
  copy.append(el('code', null, cmd), el('span', 'cmd-hint', 'copy'));
  copy.onclick = (e) => {
    e.stopPropagation();
    navigator.clipboard.writeText(cmd).then(
      () => { copy.querySelector('.cmd-hint').textContent = 'copied'; },
      () => { copy.querySelector('.cmd-hint').textContent = 'select it'; });
    setTimeout(() => { copy.querySelector('.cmd-hint').textContent = 'copy'; }, 1600);
  };
  row.append(copy);

  if (cat.url) {
    const link = el('a', 'agent-link', 'Model page');
    link.href = cat.url;
    link.target = '_blank';
    link.rel = 'noreferrer noopener';
    link.onclick = (e) => e.stopPropagation();
    row.append(link);
  }
  return row;
}

function renderAgents() {
  const box = $('agents');
  box.textContent = '';
  const q = ($('agent-filter').value || '').trim().toLowerCase();
  /* Most used first, so the model you reach for is the one at the top. Ties
     break on the tag rather than on folder order, which is arbitrary. */
  const ranked = [...S.agents].sort((x, y) =>
    y.runs - x.runs || x.model.localeCompare(y.model));
  const shown = ranked.filter((a) => !q ||
    [a.model, a.name, a.speed, (a.catalog || {}).title, (a.catalog || {}).vendor]
      .some((f) => String(f || '').toLowerCase().includes(q)));

  for (const a of shown) box.append(agentRow(a));
  $('agents-none').classList.toggle('hidden', !!shown.length);
  /* A filter over three things is furniture. It appears once the list is long
     enough that finding a model is actually work. */
  $('rail-search').classList.toggle('hidden', S.agents.length < 6);
  $('agent-count').textContent = S.agents.length > 1 ? String(S.agents.length) : '';
  renderAvailable(q);
}

/* Models the catalog knows about that are not installed. Same row shape as an
   agent, minus the counts it cannot have yet, so the column reads as one list
   of models rather than two unrelated ones. */
function renderAvailable(q) {
  const box = $('available');
  box.textContent = '';
  const shown = (S.available || []).filter((m) => !q ||
    [m.tag, m.title, m.vendor].some((f) => String(f || '').toLowerCase().includes(q)));
  for (const m of shown) {
    const row = el('div', 'agent avail');
    const head = el('div', 'agent-head');
    head.append(el('span', 'agent-name', m.tag),
                el('span', 'agent-trail', m.context ? `${Math.round(m.context / 1024)}k` : ''));
    row.append(head);
    row.append(el('div', 'agent-support', [m.vendor, m.title].filter(Boolean).join(' ')));
    row.append(el('div', 'agent-desc avail-desc', m.description || ''));
    row.append(downloadRow({ model: m.tag }, m));
    box.append(row);
  }
  $('rail-more').classList.toggle('hidden', !shown.length);
}

async function selectAgent(id) {
  S.agent = id;
  /* Threads belong to an agent folder, so switching model switches the
     conversation list with it rather than showing another agent's. */
  if (S.thread) newChat(); else loadThreads();
  S.first = true;
  S.seen = {};
  renderAgents();
  syncModel();
  await loadWorkspace();
  $('run').disabled = !!S.run;
}

// The agent folder decides the harness profile and owns the state; this only
// decides which installed tag does the talking. Defaults to the model
// config.json names, and falls back to whatever IS installed so a fresh
// machine can demo without a 4.7 GB download first.
const MORE = '__more__';

function renderModels(list) {
  const sel = $('model');
  sel.textContent = '';
  for (const m of list || []) sel.append(new Option(m, m));
  if (!sel.options.length) sel.append(new Option('no models installed', ''));
  /* The picker is where you go when you want a different model, so it is also
     where "I want one I do not have" belongs. Choosing it opens the rail
     rather than changing the model. */
  const more = new Option('More models…', MORE);
  more.className = 'opt-more';
  sel.append(more);
  syncModel();
}

function syncModel() {
  const sel = $('model');
  const a = S.agents.find((x) => x.id === S.agent);
  if (!a || !sel.options.length) return;
  const exact = [...sel.options].find((o) => o.value === a.model && o.value !== MORE);
  sel.value = exact ? a.model : sel.options[0].value;
  sel.classList.toggle('substituted', !exact);
  sel.title = exact
    ? `${a.model} — the model this agent folder is configured for`
    : `${a.model} is not installed; running on ${sel.value} instead`;
}

function renderPresets(list) {
  const box = $('presets');
  box.textContent = '';
  /* One row that scrolls sideways, rather than a block that wraps to five.
     Six wrapped suggestions took more vertical space than the transcript they
     sit under and read as the main content of the pane. Shorter labels than
     the wrapped version carried, since a row is scanned, not read. */
  for (const t of list) {
    const b = el('button', 'preset', clip(t, 44));
    b.type = 'button';
    b.title = t;
    b.onclick = () => { $('task').value = t; $('task').focus(); growTask(); };
    box.append(b);
  }
}

/* ---------------------------------------------------------- the folder --- */

async function loadWorkspace() {
  if (!S.agent) return;
  S.ws = await api(`/api/workspace?agent=${S.agent}`);
  $('folder-path').textContent = S.ws.folder;
  renderTree(S.ws);
}

function section(key, iconName, name, items, render, emptyText) {
  const d = el('details', 'node');
  d.open = S.open.has(key);
  d.ontoggle = () => d.open ? S.open.add(key) : S.open.delete(key);

  const sum = el('summary');
  const count = el('span', 'count', String(items.length));
  const ico = el('span', 'ico');
  ico.append(icon(iconName));
  sum.append(el('span', 'caret', '▶'), ico, el('span', 'nm', name), count);
  d.append(sum);

  const list = el('div', 'items');
  if (!items.length) {
    list.append(el('div', 'empty-note', emptyText));
  } else {
    const prev = S.seen[key] || null;
    const keys = [];
    items.forEach((item, i) => {
      const node = render(item, i);
      const k = JSON.stringify(item);
      keys.push(k);
      if (prev && !prev.has(k)) {
        node.classList.add('fresh');
        count.classList.add('bump');
        d.open = true;
        S.open.add(key);
      }
      list.append(node);
    });
    if (!S.first || !prev) S.seen[key] = new Set(keys);
  }
  d.append(list);
  return d;
}

function itemNode(line1, line2, onclick) {
  const n = el('button', 'item');
  const t1 = el('div', 't1');
  t1.innerHTML = line1;
  n.append(t1);
  if (line2) n.append(el('div', 't2', line2));
  if (onclick) n.onclick = onclick; else n.style.cursor = 'default';
  return n;
}

const esc = (s) => String(s ?? '').replace(/[&<>]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

function renderTree(ws) {
  const tree = $('tree');
  tree.textContent = '';

  tree.append(section('files', 'folder', 'files', ws.files, (f) =>
    itemNode(`<b>${esc(f.name)}</b>`, `${bytes(f.size)} · ${ago(f.mtime)}`,
             () => openFile(f.name)),
    'nothing created yet'));

  tree.append(section('inbox', 'inbox', 'inbox', ws.emails, (e) =>
    itemNode(`<b>${esc(e.subject)}</b>`, `${e.from} · ${e.date}`,
             () => openEmail(e)),
    'inbox empty'));

  tree.append(section('calendar', 'calendar', 'calendar', ws.events, (v) =>
    itemNode(`<b>${esc(v.title)}</b>`,
             `${v.date} · ${v.start}–${v.end}${v.location ? ' · ' + v.location : ''}` +
             (v.attendees && v.attendees.length ? ` · ${v.attendees.join(', ')}` : '')),
    'no events'));

  tree.append(section('messages', 'chat', 'messages', ws.messages, (m) =>
    itemNode(`to <b>${esc(m.to)}</b>`, clip(m.text, 160)),
    'none sent'));

  tree.append(section('reminders', 'clock', 'reminders', ws.reminders, (r) =>
    itemNode(esc(r.text), `${r.date} at ${r.time}`),
    'none set'));

  tree.append(section('sent', 'send', 'sent mail', ws.sent, (m) =>
    itemNode(`<b>${esc(m.subject || '(no subject)')}</b>`, `to ${m.to} · ${clip(m.body, 90)}`,
             () => openViewer(m.subject || 'Sent mail', mailBody({ ...m, from: 'you' }))),
    'nothing sent'));

  tree.append(section('memory', 'layers', 'memory', ws.memory, (f) =>
    itemNode(esc(f), null), 'nothing learned yet'));

  if (ws.tree) {
    tree.append(section('real', 'drive', 'working folder', ws.tree, (f) =>
      itemNode((f.dir ? icon('folder', 13).outerHTML + ' ' : '') + esc(f.name),
               f.dir ? null : bytes(f.size || 0)),
      'empty'));
  }

  tree.append(section('runs', 'log', 'past runs', ws.logs || [], (l) =>
    itemNode(esc(l.name.replace('.json', '')), ago(l.mtime), () => openLog(l.name)),
    'no runs yet'));

  S.first = false;
}

/* -------------------------------------------------------------- viewer --- */

function openViewer(title, node, dl) {
  $('viewer-title').textContent = title;
  const body = $('viewer-body');
  body.textContent = '';
  body.append(node);
  const a = $('viewer-dl');
  if (dl) { a.href = dl; a.classList.remove('hidden'); } else a.classList.add('hidden');
  $('viewer').classList.remove('hidden');
}
const closeViewer = () => $('viewer').classList.add('hidden');

function mailBody(e) {
  const box = el('div');
  box.append(el('div', 'mail-meta',
    `${e.from ? 'from ' + e.from : ''}${e.to ? 'to ' + e.to : ''}${e.date ? ' · ' + e.date : ''}`));
  box.append(el('div', 'mail-body', e.body || ''));
  return box;
}
const openEmail = (e) => openViewer(e.subject, mailBody(e));

// ---------------------------------------------------------- office viewers --
// Both render from real geometry the server pulled out of the file, so what you
// see is close to what PowerPoint/Excel would draw — not a text summary.

const COL_LETTER = (n) => {
  let s = '';
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - 1 - m) / 26; }
  return s;
};

function renderDeck(p) {
  const wrap = el('div', 'deck');
  // Every length is a fraction of the slide, so one CSS container query unit
  // (cqw = 1% of the frame width) scales text and boxes together at any size.
  const pctW = (v) => (v / p.w_pt) * 100;
  p.slides.forEach((s, i) => {
    wrap.append(el('div', 'n', `slide ${i + 1} of ${p.slides.length}`));
    const frame = el('div', 'slide-frame');
    frame.style.aspectRatio = `${p.w_pt} / ${p.h_pt}`;
    for (const sh of s.shapes) {
      const node = el('div', 'sh sh-' + sh.kind);
      node.style.left = sh.x * 100 + '%';
      node.style.top = sh.y * 100 + '%';
      node.style.width = sh.w * 100 + '%';
      node.style.height = sh.h * 100 + '%';
      if (sh.fill) node.style.background = sh.fill;
      if (sh.kind === 'picture') {
        const img = el('img');
        img.src = sh.src;
        node.append(img);
      } else if (sh.kind === 'table') {
        const t = el('table', 'sh-table');
        sh.rows.forEach((row, ri) => {
          const tr = el('tr');
          row.forEach((c) => tr.append(el(ri === 0 ? 'th' : 'td', null, c)));
          t.append(tr);
        });
        node.append(t);
      } else if (sh.kind === 'text') {
        for (const para of sh.paragraphs) {
          const line = el('p', 'sh-p');
          line.textContent = para.text;
          line.style.fontSize = pctW(para.size) + 'cqw';
          if (para.color) line.style.color = para.color;
          if (para.bold) line.style.fontWeight = '700';
          if (para.align) line.style.textAlign = para.align;
          if (para.level) line.style.paddingLeft = para.level * 3 + '%';
          if (!sh.title && para.level >= 0 && sh.paragraphs.length > 1) {
            line.classList.add('bullet');
          }
          node.append(line);
        }
      }
      frame.append(node);
    }
    wrap.append(frame);
  });
  return wrap;
}

function renderWorkbook(p) {
  const wrap = el('div', 'book');
  p.sheets.forEach((sh, si) => {
    const tab = el('div', 'sheet-tab' + (si === 0 ? ' on' : ''));
    tab.textContent = sh.sheet;
    wrap.append(tab);

    // cells swallowed by a merge must not be emitted at all
    const skip = new Set();
    for (const m of sh.merges) {
      for (let r = m.r; r < m.r + m.rs; r++) {
        for (let c = m.c; c < m.c + m.cs; c++) {
          if (r !== m.r || c !== m.c) skip.add(r + ':' + c);
        }
      }
    }
    const span = {};
    for (const m of sh.merges) span[m.r + ':' + m.c] = m;

    const scroll = el('div', 'sheet-scroll');
    const t = el('table', 'grid');
    const head = el('tr');
    head.append(el('th', 'corner'));
    for (let c = 1; c <= sh.cols; c++) {
      const th = el('th', 'colhead', COL_LETTER(c));
      th.style.minWidth = Math.max(48, sh.widths[c - 1] || 72) + 'px';
      head.append(th);
    }
    t.append(head);

    sh.rows.forEach((row, ri) => {
      const tr = el('tr');
      tr.append(el('th', 'rowhead', String(ri + 1)));
      row.forEach((cell, ci) => {
        const key = (ri + 1) + ':' + (ci + 1);
        if (skip.has(key)) return;
        const td = el('td', null, cell.v);
        const m = span[key];
        if (m) { if (m.cs > 1) td.colSpan = m.cs; if (m.rs > 1) td.rowSpan = m.rs; }
        if (cell.b) td.style.fontWeight = '700';
        if (cell.a) td.style.textAlign = cell.a;
        if (cell.f) { td.classList.add('formula'); td.title = cell.f; }
        tr.append(td);
      });
      t.append(tr);
    });
    scroll.append(t);
    wrap.append(scroll);
    if (sh.truncated) {
      wrap.append(el('div', 'n', `showing the first ${sh.rows.length} rows`));
    }
  });
  return wrap;
}

/* One place that turns a preview payload into a node, so the full pane, the
   thumbnail and the modal cannot drift apart. */
function renderPreview(p) {
  if (p.kind === 'pptx') return renderDeck(p);
  if (p.kind === 'xlsx') return renderWorkbook(p);
  if (p.kind === 'text') return el('div', 'plain', p.text);
  return el('div', 'plain', `binary file, ${bytes(p.size)} — download to open it`);
}

async function openFile(name) {
  const url = `/api/download?agent=${S.agent}&name=${encodeURIComponent(name)}`;
  let p;
  try {
    p = await api(`/api/preview?agent=${S.agent}&name=${encodeURIComponent(name)}`);
  } catch (err) {
    return openViewer(name, el('div', 'plain', String(err.message)));
  }
  const box = el('div');
  box.append(renderPreview(p));
  openViewer(name, box, url);
}

async function openLog(name) {
  const log = await api(`/api/log?agent=${S.agent}&name=${encodeURIComponent(name)}`);
  const box = el('div');
  box.append(el('div', 'mail-meta',
    `${log.model || ''} · ${log.finished ? 'finished' : 'ran out of model calls'}${log.summary ? ' · ' + log.summary : ''}`));
  box.append(el('div', 'mail-body', log.task));
  const pre = el('pre', 'raw');
  pre.textContent = (log.transcript || [])
    .filter((t) => t.kind !== 'system')
    .map((t) => `[${t.kind}] ${t.content}`).join('\n\n');
  box.append(pre);
  openViewer(name, box);
}

/* ----------------------------------------------- the artifact pane ------ */
/* The point of this layout: a file lands, it is on screen. No click, no modal,
   at the size the thing is actually meant to be read at. */

/* The All and Workspace tabs exist before anything does. Codex keeps its
   top-level tabs visible whether or not they have content, so the right side
   always says what it is for instead of being an unexplained empty box. */
const panes = {
  all: { pane: $('pane-all'), tab: null },
  ws: { pane: $('pane-ws'), tab: null },
};
const allCount = el('span', 'count', '0');

function makeTab(label, cls, onSelect) {
  const tab = el('button', 'tab' + (cls ? ' ' + cls : ''));
  tab.type = 'button';
  tab.setAttribute('role', 'tab');
  tab.append(document.createTextNode(label));
  tab.onclick = onSelect;
  $('tabs').append(tab);
  return tab;
}

/* A role=tablist promises arrow-key navigation and a roving tabindex: one stop
   for the whole group, arrows to move within it. Every tab being tabindex 0
   means Tab walks through all of them, which is the behaviour the role tells
   screen reader users will not happen. */
$('tabs').addEventListener('keydown', (ev) => {
  const tabs = [...$('tabs').querySelectorAll('.tab')];
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[ev.key];
  if (to === undefined) return;
  ev.preventDefault();
  const next = tabs[(to + tabs.length) % tabs.length];
  next.focus();
  next.click();
});

panes.all.tab = makeTab('All', null, () => select('all'));
panes.all.tab.append(allCount);
panes.ws.tab = makeTab('Workspace', null, () => select('ws'));

function select(name) {
  for (const [k, v] of Object.entries(panes)) {
    const on = k === name;
    v.pane.classList.toggle('on', on);
    v.tab.classList.toggle('on', on);
    v.tab.setAttribute('aria-selected', on ? 'true' : 'false');
    // roving tabindex: the selected tab is the group's single tab stop
    v.tab.tabIndex = on ? 0 : -1;
  }
}
select('all');   // the All tab reads as selected from the first frame

async function showArtifact(name, stat) {
  if (panes[name]) {
    // a rewrite: the caption has to follow, or the All view disagrees with the
    // chip strip about what the agent just produced
    if (panes[name].stat) panes[name].stat.textContent = stat || '';
    return select(name);
  }
  let payload;
  try {
    payload = await api(`/api/preview?agent=${S.agent}&name=${encodeURIComponent(name)}`);
  } catch (_) {
    return;                    // the touched chip already records that it exists
  }
  if (panes[name]) return;     // two events for the same file raced here

  $('holding').classList.add('hidden');
  $('grid-all').classList.remove('hidden');
  // the first file is the moment the workspace becomes worth looking at
  setWorkspace(true);

  const pane = el('div', 'pane');
  // the static panes are marked up as tabpanels; panes built at runtime were
  // not, so most of the tablist pointed at nothing
  pane.setAttribute('role', 'tabpanel');
  pane.setAttribute('aria-label', name);
  pane.style.padding = '22px 26px';
  pane.append(renderPreview(payload));
  $('canvas').append(pane);

  const tab = makeTab(name, 'new', () => select(name));

  /* The same renderers again, into a small box. They size themselves from
     their container, so there is no separate thumbnail code path to keep in
     sync with the real one. */
  const thumb = el('button', 'thumb');
  thumb.type = 'button';
  const cap = el('div', 'cap');
  const capStat = el('span', null, stat || '');
  cap.append(el('b', null, name), capStat);
  const shot = el('div', 'shot');
  shot.append(renderPreview(payload));
  thumb.append(cap, shot);
  thumb.onclick = () => select(name);
  $('grid-all').append(thumb);

  panes[name] = { pane, tab, stat: capStat };
  const made = Object.keys(panes).length - 2;
  allCount.textContent = String(made);
  if (!document.body.classList.contains('ws-open')) {
    $('ws-count').textContent = String(made);
    $('ws-count').classList.remove('hidden');
  }
  select(name);
  setTimeout(() => tab.classList.remove('new'), 950);
}

/* --- what this run touched --- */
/* Scoped to the run, not the folder: a file the agent never opened is not part
   of the story being told. Keyed by name, because an agent revising its own
   deck writes the same file twice and a second chip for it is a lie about how
   many things it made. */
const touched = {};
/* Every product that lists changed files caps the list. Ours appended without
   a ceiling, so a long run would grow the strip until it pushed the canvas off
   screen. Measured: at 1440 the strip fits 5 chips per row, at 1280 it fits 4.
   8 is two rows, and the CSS bounds the strip to two rows independently so a
   long filename can never push it to three. */
const TOUCHED_MAX = 8;
let overflowChip = null;

function addTouched(name, stat) {
  $('touched-none').classList.add('hidden');
  let chip = touched[name];
  if (!chip) {
    if (Object.keys(touched).length >= TOUCHED_MAX) {
      if (!overflowChip) {
        overflowChip = el('span', 'more', '');
        $('touched').append(overflowChip);
      }
      touched[name] = null;                       // counted, not drawn
      overflowChip.textContent = `+${Object.keys(touched).length - TOUCHED_MAX} more`;
      return;
    }
    chip = el('button', 'chip');
    chip.type = 'button';
    chip.append(el('span', 'nm', name), el('span', 'add', ''));
    chip.onclick = () => panes[name] ? select(name) : openFile(name);
    $('touched').append(chip);
    touched[name] = chip;
  }
  if (!chip) return;                              // an overflowed file, rewritten
  chip.querySelector('.add').textContent = stat || '';
  chip.classList.remove('fresh');
  void chip.offsetWidth;           // restart the animation on a rewrite
  chip.classList.add('fresh');
  setTimeout(() => chip.classList.remove('fresh'), 950);
}

/* The runner already tells us how big the thing it made is, so showing it here
   beats leaving it buried in the arguments. */
function statFor(e) {
  const a = e.args || {};
  if (Array.isArray(a.rows)) return `+${a.rows.length} rows`;
  if (Array.isArray(a.slides)) return `+${a.slides.length} slides`;
  return '';
}

/* ------------------------------------------------------- the timeline --- */

const feed = $('timeline');
function push(cls) {
  const n = el('div', 'ev ' + (cls || ''));
  feed.append(n);
  autoscroll();
  return n;
}

/* Only auto-scroll when the viewport is already within 100px of the bottom.
   Before this, every event yanked the pane down, so you could not read back
   through a run while it was still going. Scroll up once and the feed leaves
   you alone until you return to the bottom yourself. */
const STICK_PX = 100;
function autoscroll() {
  const f = feed.parentElement;
  if (f.scrollHeight - f.scrollTop - f.clientHeight <= STICK_PX) {
    f.scrollTop = f.scrollHeight;
  }
}

/* The clock stops when the run does. It used to be a bare setInterval that was
   only cleared on finishRun, so a stream that ended without closing kept
   counting: a screenshot twenty minutes later read "1270s" for an 11-second
   run. A number that keeps moving after the thing it measures has stopped is
   worse than no number. */
const paintClock = () =>
  $('time-val').textContent = `${Math.round((Date.now() - S.t0) / 1000)}s`;
function startClock() {
  stopClock();
  S.t0 = Date.now();
  paintClock();
  S.timer = setInterval(paintClock, 250);
}
function stopClock() {
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  paintClock();                       // land on the true final value
}

function meters(c, budget) {
  const box = $('meter-calls');
  $('calls-val').textContent = `${c}/${budget}`;
  const r = budget ? c / budget : 0;
  $('calls-bar').style.transform = `scaleX(${r})`;
  // classList, not className: assigning the whole string used to drop the
  // layout classes the moment a run crossed a threshold
  box.classList.toggle('warn', r > 0.7 && r <= 0.9);
  box.classList.toggle('bad', r > 0.9);
}

/* Finding 7. The plan arrives once, as lines like "1. read_email - get the Q3
   numbers". Only the tool name is kept: the prose after it repeats what the
   timeline is about to say anyway, and this strip has to stay one or two rows
   tall beside the Steps only button. */
let planSteps = [];
let planCursor = -1;   // index of the furthest step reached so far
function drawPlan(content) {
  /* Idempotent: clear before drawing. Appending meant a second plan event
     duplicated the whole strip, and the harness can legitimately emit one
     again after a failure. A step already spent must never reappear. */
  $('plan').textContent = '';
  planCursor = -1;
  planSteps = String(content).split('\n').filter(Boolean).map((line) => {
    const m = line.match(/^\d+\.\s*(\S+)/);
    const node = el('span', 'step', m ? m[1] : clip(line, 28));
    node.title = line;
    $('plan').append(node);
    return { tool: m ? m[1] : null, node, done: false };
  });
  if (planSteps[0]) planSteps[0].node.classList.add('now');
}

/* The pointer only moves forward. A real run showed why: the model skipped
   step 3, completed 4 and 5, and "now" walked backwards onto the skipped step,
   so a finished plan pointed at something it had already moved past. A step
   the run overtook is not what happens next, it is a step that did not happen,
   so it goes quiet rather than reclaiming the cursor. */
function advancePlan(tool) {
  const i = planSteps.findIndex((s) => !s.done && s.tool === tool);
  if (i < 0) return;                       // an unplanned call: leave the plan alone
  planSteps[i].done = true;
  planSteps[i].node.classList.remove('now');
  planSteps[i].node.classList.add('done');
  if (i > planCursor) planCursor = i;
  for (const s of planSteps) s.node.classList.remove('now');
  const next = planSteps.find((s, j) => !s.done && j > planCursor);
  if (next) next.node.classList.add('now');
}

/* Nothing is "next" once the run is over. Without this the strip kept a live
   pointer on a finished run. */
function endPlan() {
  for (const s of planSteps) s.node.classList.remove('now');
}

/* --- events ------------------------------------------------------------ */

function onBanner(e) {
  resetRun();
  meters(0, e.budget);
  const n = push('act');
  n.append(el('div', 'banner-task', e.task));

  /* One line of facts, not a wall. This used to print five run chips followed
     by eight harness knobs, thirteen boxes stacked three rows deep above the
     first thing the model said. The run line keeps what changes between runs;
     the harness settings are fixed configuration and live behind the
     disclosure that was already there to explain them. */
  const p = e.profile;
  const facts = [e.model, `${e.budget} calls`, e.toolset];
  if (p) facts.push(p.label);
  if (e.root) facts.push(`folder: ${e.root}`);
  if (e.yolo) facts.push('confirmations off');
  if (e.tiers) facts.push(`tiers: ${Object.values(e.tiers.roles).join(', ')}`);
  n.append(el('div', 'banner-facts', facts.join('  ·  ')));

  if (p) {
    const det = el('details', 'harness-why');
    det.append(el('summary', null, 'harness settings'));
    const hz = el('div', 'harness-strip');
    const knob = (on, label) => el('span', 'knob' + (on ? ' on' : ' off'), label);
    hz.append(knob(p.plan, p.plan ? `plan ≤${p.plan_max_steps}` : 'no plan'),
              knob(p.verify_rounds > 0, p.verify_rounds ? `verify ×${p.verify_rounds}` : 'no verify'),
              knob(p.loop_break, p.loop_break ? 'loop-break' : 'loops ok'),
              knob(true, `out ≤${p.num_predict}`),
              knob(true, `ctx ${(p.num_ctx / 1024).toFixed(0)}k`),
              knob(true, `think ≤${p.think_streak_cap}`),
              knob(true, `mem ${p.memory_k}`));
    det.append(hz);
    if (p.rationale) det.append(el('div', 'note', p.rationale));
    det.append(el('div', 'note', `${e.today} · ${e.endpoint}`));
    n.append(det);
  }
  S.banner = n;
}

/* A disclosure that keeps the long text out of the flow but never out of
   reach. Two callers wanted the same thing with different bodies. */
function details(label, text, cls) {
  const det = el('details');
  det.append(el('summary', null, label));
  det.append(cls === 'note' ? el('div', 'note', text) : (() => {
    const pre = el('pre', 'raw');
    pre.textContent = text;
    return pre;
  })());
  return det;
}

/* The model speaks JSON, but its `thought` field is the only part a person
   wants. Pulling the field out of a half-written object means the sentence can
   stream as it is written, without the braces and quoting around it ever
   reaching the screen. */
const THOUGHT_RE = /"(?:thought|reasoning)"\s*:\s*"((?:[^"\\]|\\.)*)/;
function liveThought(raw) {
  const m = raw.match(THOUGHT_RE);
  if (!m) return null;
  // a fragment can end mid-escape, which is not parseable; drop the dangling
  // backslash and let the next token complete it
  const body = m[1].replace(/\\$/, '');
  try { return JSON.parse('"' + body + '"'); }
  catch (_) { return body.replace(/\\n/g, '\n').replace(/\\"/g, '"'); }
}

function onCallStart(e) {
  const n = push('');
  // Until a thought appears there is nothing worth reading, so the row says it
  // is working rather than showing an object being assembled.
  const body = el('div', 'thinking', 'Thinking');
  n.append(body);
  S.call = { node: n, body, text: '' };
  meters(e.call, e.budget);
}

function onToken(e) {
  if (!S.call) return;
  S.call.text += e.text;
  const t = liveThought(S.call.text);
  if (t) {
    S.call.body.className = 'think';
    S.call.body.textContent = t;
  }
  autoscroll();
}

/* Every path that ends a call has to land the row in a readable state, or it
   shimmers "Thinking" forever. This is the one place that does it. */
function settleCall(text, raw) {
  if (!S.call) return;
  if (text) {
    S.call.body.className = 'think';
    S.call.body.textContent = text;
  } else {
    // A model call with no "thought" is ordinary: the tool row underneath is
    // what the call was for. Printing "no reasoning given" turned that into a
    // line of its own, and on a five-call run four of the eleven rows existed
    // only to report the absence. The row keeps its timing, which is real, and
    // the raw reply is still one click away.
    S.call.body.remove();
    S.call.node.classList.add('wordless');
  }
  if (raw) S.call.node.append(details('raw reply', raw));
  S.call.node.classList.add('settled');
  S.call = null;
}

function onCallEnd(e) {
  $('tok-val').textContent = (+$('tok-val').textContent + e.output_tokens);
  if (S.call) {
    S.call.node.append(el('div', 'when',
      `${(e.ms / 1000).toFixed(1)}s · ${e.output_tokens} tokens`));
  }
}

/* The parsed thought replaces the streamed one. They are usually identical, but
   the streamed version came out of a half-written object and the parsed one is
   authoritative. The JSON stays reachable as evidence, collapsed. */
function onModelReply(content) {
  if (!S.call) return;
  let obj = null;
  try { obj = JSON.parse(content); } catch (_) { /* the harness will repair it */ }
  const thought = obj && (obj.thought || obj.reasoning);
  settleCall(thought ? String(thought) : (obj ? '' : 'reply was not valid JSON'), content);
}

let doneSummary = null;

function onNote(e) {
  const k = e.kind;
  if (k === 'system') {
    if (S.banner) S.banner.append(details('the prompt the harness built', e.content));
    return;
  }
  if (k === 'task' || k === 'observation') return;   // shown by the banner / tool row
  /* The plan call has to settle its row too. It did not, so the row that
     produced the plan kept whatever the model had streamed into it: a raw
     {"steps": [...]} object sitting at the top of every run forever. The plan
     strip already shows the steps, so the row only reports that it planned. */
  if (k === 'plan') {
    drawPlan(e.content);
    const n = planSteps.length;
    settleCall(`Planned ${n} step${n === 1 ? '' : 's'}.`, e.content);
    return;
  }
  if (k === 'model') return onModelReply(e.content);

  if (k === 'repair') {
    const d = el('div', 'note');
    d.append(el('b', null, 'harness repaired the call'),
             document.createTextNode(' · ' + e.content));
    push('').append(d);
    return;
  }
  /* The harness correcting the model is the recovery half of a failure.
     Without it the timeline shows a tool failing and then, unexplained, the
     same tool working, which reads as luck rather than as a system. */
  if (k === 'feedback') {
    const d = el('div', 'note');
    d.append(el('b', null, 'harness → model'), document.createTextNode(' · ' + e.content));
    push('act').append(d);
    return;
  }
  if (k === 'verify') {
    let v = {};
    try { v = JSON.parse(e.content); } catch (_) { /* keep the raw text */ }
    const ok = v.complete !== false;
    const d = el('div', 'note');
    /* The verifier fails open so a broken one cannot trap the agent, but that
       makes a failure look exactly like a pass. When the harness marks the
       verdict unverified, say so rather than claiming the run checks out. */
    if (ok && v.unverified) {
      d.append(el('b', 'warn-tag', 'not verified'),
               document.createTextNode(' · ' + v.unverified));
      push('').append(d);
      return;
    }
    d.append(el('b', ok ? 'good-tag' : 'bad-tag',
                ok ? 'verified complete' : 'verifier: not done'));
    if (!ok) d.append(document.createTextNode(' · ' + (v.missing || e.content)));
    push(ok ? 'made' : 'bad').append(d);
    return;
  }
  /* Held, not drawn. The `done` note and the `end` event both carry a sentence
     about the same run, and rendering both produced two near-identical
     paragraphs in a row. They belong in one card. */
  if (k === 'done') doneSummary = e.content;
}

/* A message the agent chose to send, rendered as a turn in the conversation
   rather than as a row in a log — it is speech, not a step. */
function onSay(e) {
  const text = String((e.args || {}).text || '').trim();
  if (!text) return;
  const n = push('act has-tool');
  const turn = el('div', 'turn assistant');
  turn.append(el('div', 'turn-text', text));
  n.append(turn);
}

function onTool(e) {
  if (e.name === 'think') return;
  if (e.name === 'say') return onSay(e);
  const mut = MUTATORS.has(e.name);
  /* A failed mutator used to get the green "made" dot, so a create_deck that
     wrote nothing looked exactly like one that worked. Failure outranks
     intent: the dot follows what happened, not what was attempted. */
  if (e.ok) advancePlan(e.name);   // a failed call has not completed its step
  /* has-tool is what "Steps only" filters on: this event carries an action,
     so it survives the strip. */
  const n = push((!e.ok ? 'bad' : mut ? 'made' : 'act') + ' has-tool');

  const row = el('div', 'tool' + (e.ok ? '' : ' err'));
  const arg = Object.values(e.args || {})[0];
  row.append(el('span', 'nm', e.name),
             el('span', 'arg', arg == null ? '' : clip(typeof arg === 'string' ? arg : JSON.stringify(arg), 90)),
             el('span', 'out', e.ok ? (mut ? 'written' : 'completed') : 'failed'));
  /* The sentence leads (harness/narrate.py sends it); the tool name and its
     arguments move behind a disclosure. Both readings stay available — one for
     the person who asked about their Thursday, one for whoever is debugging
     the loop — but the log is no longer the only thing on offer. */
  if (e.line) {
    n.append(el('div', 'step-line' + (e.ok ? '' : ' step-bad'), e.line));
    const det = el('details', 'step-detail');
    det.append(el('summary', null, 'details'));
    det.append(row);
    n.append(det);
  } else {
    n.append(row);
  }
  /* Why it failed is the whole point of showing the failure. */
  if (!e.ok) n.append(el('div', 'reason', clip(e.result, 600)));

  if (!e.ok) return;
  const key = TOUCH_ARG[e.name];
  const name = key && e.args ? e.args[key] : null;
  if (!name) return;
  const stat = statFor(e);
  addTouched(name, stat);
  if (ARTIFACT_ARG[e.name]) showArtifact(name, stat);
}

function onConfirm(e) {
  const n = push('act');
  /* A confirmation that touches a live mailbox looked exactly like one that
     overwrites a scratch file. Same words, same buttons, same colour, and the
     only clue was a server id buried in the argument dump. Anything reaching a
     real account says so first, in its own words, and in live mode says that
     the thing on the other side is a person. */
  const box = el('div', 'confirm-box' + (e.real ? ' real' : ''));
  if (e.real) {
    const sends = e.mode === 'live';
    box.append(el('div', 'real-flag',
      sends ? `This goes out from your real ${e.real} account. It cannot be undone.`
            : `This touches your real ${e.real} account.`));
  }
  box.append(el('div', 'tag', `the agent wants to ${e.action}`),
             el('div', 'note', e.detail));
  const row = el('div', 'confirm-actions');
  const allow = el('button', 'allow', e.real && e.mode === 'live' ? 'Send it' : 'Allow');
  const deny = el('button', 'deny', 'Deny');
  const answer = (ok) => {
    post('/api/confirm', { id: e.id, allow: ok }).catch(() => {});
    box.classList.add('answered');
    row.textContent = '';
    row.append(el('div', 'note', ok ? 'you allowed it' : 'you declined it'));
  };
  allow.onclick = () => answer(true);
  deny.onclick = () => answer(false);
  row.append(allow, deny);
  box.append(row);
  n.append(box);
  autoscroll();
}

/* The run already emitted a summary twice — a `done` note and an `end` event —
   both rendered as ordinary rows in the same type as everything else, so the
   conclusion read as two more log lines. A run needs a full stop: one card,
   answering what it did and what it made. Bounded, not a dump. */
function onEnd(e) {
  stopClock();
  endPlan();
  // a call still open at the end would shimmer "Thinking" on a finished run
  settleCall('', S.call ? S.call.text : '');
  const n = push(e.finished ? 'made' : 'bad');
  const card = el('div', 'endcard' + (e.finished ? '' : ' cut'));

  /* "Budget" is vague: it reads as money or tokens. What actually ends a run
     is MAX_CALLS, a cap on model calls. Tokens are capped per-call by
     num_predict and bounded by num_ctx, but neither stops a run, so naming
     tokens here would describe a mechanism the harness does not have. */
  card.append(el('div', 'endhead', e.finished ? 'Run complete' : 'Out of model calls'));

  // what it did: the model's own sentence, not the harness's tally
  const say = doneSummary || e.summary;
  if (say) card.append(el('div', 'endsay', say));

  /* The verifier's report of side effects the task never asked for. Nothing is
     auto-undone - undoing a send is impossible and undoing an edit is a bigger
     side effect than the one being reported - so the honest move is to say it
     plainly to the person who can judge. */
  if (e.unrequested) {
    const w = el('div', 'endwarn');
    w.append(el('b', null, 'Not asked for: '), document.createTextNode(e.unrequested));
    card.append(w);
  }

  /* What it made. The timeline says a file was written and then scrolls away;
     this is the only place the outputs are listed together, and each one is
     the same click as its tab. */
  const made = Object.keys(panes).filter((k) => k !== 'all' && k !== 'ws');
  if (made.length) {
    const box = el('div', 'endmade');
    box.append(el('div', 'endlabel', 'produced'));
    for (const name of made) {
      const row = el('button', 'endfile');
      row.type = 'button';
      row.append(el('span', 'nm', name),
                 el('span', 'add', panes[name].stat ? panes[name].stat.textContent : ''));
      row.onclick = () => select(name);
      box.append(row);
    }
    card.append(box);
  }

  const stats = el('div', 'endstats');
  const stat = (v, l, bad) => {
    const d = el('div', bad ? 'bad-stat' : null);
    d.append(el('b', null, String(v)), el('span', null, l));
    return d;
  };
  const plural = (nn, word) => `${word}${nn === 1 ? '' : 's'}`;
  /* calls first, flagged red when they are what stopped the run, because the
     stat that ended the run should be the one the eye lands on */
  stats.append(stat(`${e.calls}/${e.budget}`, 'model calls', !e.finished),
               stat(e.output_tokens, 'tokens out'),
               stat(`${e.wall}s`, 'model time'),
               stat(e.actions.length, plural(e.actions.length, 'action')));
  if (e.tool_errors) {
    stats.append(stat(e.tool_errors, plural(e.tool_errors, 'tool error'), true));
  }
  const badReplies = e.parse_failures + e.invalid_calls;
  if (badReplies) stats.append(stat(badReplies, plural(badReplies, 'bad reply'), true));
  /* The counters are for whoever wants to audit the run, and a lot of people
     never do: they asked for a summary and a file, not a token count. They fold
     away behind a one-line digest, and the preferences menu can drop them
     entirely. On by default, because the numbers are the honest part. */
  const details = el('details', 'endmore');
  details.open = true;
  const sum = el('summary', 'endmore-sum');
  sum.append(el('span', null, 'Run details'),
             el('span', 'endmore-digest',
                `${e.calls} ${plural(e.calls, 'call')} · ${e.wall}s`));
  details.append(sum, stats);

  /* Its own class, not .endlabel: that one uppercases, which turned a
     case-sensitive path into AGENTS/8B/LOGS/RUN_003.JSON. */
  if (e.log) {
    const f = el('div', 'endfoot');
    f.append(el('span', null, 'Transcript'), el('code', null, e.log));
    details.append(f);
  }
  card.append(details);
  n.append(card);
}

function onError(e) {
  const n = push('bad');
  const d = el('div', 'note');
  d.append(el('b', 'bad-tag', 'error'), document.createTextNode(' ' + e.message));
  n.append(d);
  if (e.trace) {
    const det = details('traceback', e.trace);
    det.className = 'trace';
    n.append(det);
  }
}

/* One place that knows everything a run accumulates. Anything added to that
   list later has to be cleared here too, which is why it is one function
   rather than scattered resets. Without it a second Run stacks on the first:
   the plan strip doubles, spent steps reappear, artifact tabs pile up. */
function resetRun() {
  feed.textContent = '';
  $('empty').classList.add('hidden');
  $('plan').textContent = '';
  planSteps = [];
  planCursor = -1;
  S.call = null;
  S.banner = null;
  doneSummary = null;
  $('tok-val').textContent = '0';
  startClock();

  for (const k of Object.keys(panes)) {
    if (k === 'all' || k === 'ws') continue;
    panes[k].pane.remove();
    panes[k].tab.remove();
    delete panes[k];
  }
  $('grid-all').textContent = '';
  $('grid-all').classList.add('hidden');
  $('holding').classList.remove('hidden');
  allCount.textContent = '0';
  $('ws-count').classList.add('hidden');
  select('all');

  for (const k of Object.keys(touched)) delete touched[k];
  if (overflowChip) { overflowChip.remove(); overflowChip = null; }
  $('touched').querySelectorAll('.chip').forEach((c) => c.remove());
  $('touched-none').classList.remove('hidden');
}

/* ---------------------------------------------------------------- run --- */

function handle(e) {
  switch (e.t) {
    case 'banner': return onBanner(e);
    case 'llm_start': return onCallStart(e);
    case 'token': return onToken(e);
    case 'llm_end': return onCallEnd(e);
    case 'note': return onNote(e);
    case 'tool': return onTool(e);
    case 'world': return renderTree({ ...S.ws, ...e, logs: (S.ws || {}).logs || [] });
    case 'confirm': return onConfirm(e);
    case 'end': return onEnd(e);
    case 'error': return onError(e);
    case 'stdout': return void console.log('[runner]', e.text);
    case 'closed': return finishRun();
  }
}

async function startRun() {
  if (!S.agent) return;
  const task = $('task').value.trim();
  if (!task) { $('task').focus(); return; }
  /* The thread is created from the first message rather than by the New chat
     button, so an empty conversation never appears in the sidebar. */
  if (!S.thread) {
    try {
      S.thread = (await post('/api/thread/new', { agent: S.agent, task })).id;
    } catch (err) { /* a thread is a nicety; the run should still go */ }
  }
  const body = {
    agent: S.agent, task, thread: S.thread,
    root: $('opt-root').value.trim(),
    shell: $('opt-shell').checked,
    yolo: $('opt-yolo').checked,
    with_office: $('opt-office').checked,
    tiers: $('opt-tiers').checked,
    max_calls: parseInt($('opt-calls').value, 10) || null,
    model: $('model').value || null,
    mcp: mcpSelected(),
    mcp_mode: $('opt-mcp-mode').value,
  };
  resetRun();
  let res;
  try {
    res = await post('/api/run', body);
  } catch (err) {
    return onError({ message: err.message });
  }
  S.run = res.run;
  S.seen = {};
  S.first = false;
  $('run').disabled = true;
  $('stop').classList.remove('hidden');
  ['meter-calls', 'meter-time', 'meter-tok'].forEach((id) => $(id).classList.remove('hidden'));

  S.es = new EventSource(`/api/events?run=${S.run}`);
  S.es.onmessage = (m) => handle(JSON.parse(m.data));
  S.es.onerror = () => { if (S.run) finishRun(); };
}

function finishRun() {
  if (S.es) { S.es.close(); S.es = null; }
  stopClock();
  S.run = null;
  S.call = null;
  $('run').disabled = false;
  $('stop').classList.add('hidden');
  loadAgents(true);
  loadWorkspace();
  refreshThread();
}

/* --------------------------------------------------------------- chrome -- */

/* Appearance and glass, in one popover. An explicit theme choice beats the OS
   preference in both directions; no attribute at all means "follow the system",
   which is the honest default and what a first run gets. The old control was a
   two-state toggle, which could not express that third state at all: once you
   had clicked it once you were pinned to a theme forever. */
const prefsBtn = $('prefs-btn');
const prefsBox = $('prefs');

const store = (key, value) => {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (_) { /* storage blocked; the session still works, it just forgets */ }
};
const stored = (key) => {
  try { return localStorage.getItem(key); } catch (_) { return null; }
};

function setTheme(choice) {
  if (choice === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', choice);
  store('agentlab-theme', choice === 'system' ? null : choice);
  paintPrefs();
}

function setGlass(on) {
  document.body.classList.toggle('flat', !on);
  $('opt-glass').checked = on;
  store('agentlab-glass', on ? 'on' : 'off');
}

function paintPrefs() {
  const current = document.documentElement.getAttribute('data-theme') || 'system';
  for (const b of prefsBox.querySelectorAll('[data-theme-set]')) {
    b.setAttribute('aria-checked', String(b.dataset.themeSet === current));
    b.setAttribute('role', 'radio');
  }
}

function setPrefs(open) {
  prefsBox.hidden = !open;
  prefsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

setTheme(stored('agentlab-theme') || 'system');
setGlass(stored('agentlab-glass') !== 'off');

prefsBtn.onclick = (e) => { e.stopPropagation(); setPrefs(prefsBox.hidden); };
prefsBox.onclick = (e) => e.stopPropagation();
for (const b of prefsBox.querySelectorAll('[data-theme-set]')) {
  b.onclick = () => setTheme(b.dataset.themeSet);
}
$('opt-glass').onchange = (e) => setGlass(e.target.checked);

function setStats(on) {
  document.body.classList.toggle('no-stats', !on);
  $('opt-stats').checked = on;
  store('agentlab-stats', on ? 'on' : 'off');
}
setStats(stored('agentlab-stats') !== 'off');
$('opt-stats').onchange = (e) => setStats(e.target.checked);

document.addEventListener('click', () => { if (!prefsBox.hidden) setPrefs(false); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !prefsBox.hidden) { setPrefs(false); prefsBtn.focus(); }
});

// follow the OS while the user has not overridden it
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', paintPrefs);

/* Ratio, not pixels: a pixel width would mean the split silently changes
   meaning when the window resizes. Percent keeps the user's intent ("show me
   more of the run") true at any size. */
const splitter = $('splitter');
const MIN_PCT = 24, MAX_PCT = 68;

function setSplit(pct) {
  const v = Math.min(MAX_PCT, Math.max(MIN_PCT, pct));
  document.documentElement.style.setProperty('--run-pct', v + '%');
  splitter.setAttribute('aria-valuenow', Math.round(v));
  try { localStorage.setItem('agentlab-split', String(v)); } catch (_) {}
}
try {
  const saved = parseFloat(localStorage.getItem('agentlab-split'));
  if (!Number.isNaN(saved)) setSplit(saved);
} catch (_) {}

splitter.addEventListener('pointerdown', (ev) => {
  ev.preventDefault();
  splitter.setPointerCapture(ev.pointerId);   // keep tracking outside the 6px
  splitter.classList.add('dragging');
  document.body.classList.add('resizing');
  const rect = document.querySelector('.body').getBoundingClientRect();
  const move = (m) => setSplit(((m.clientX - rect.left) / rect.width) * 100);
  const up = () => {
    splitter.classList.remove('dragging');
    document.body.classList.remove('resizing');
    splitter.removeEventListener('pointermove', move);
    splitter.removeEventListener('pointerup', up);
    splitter.removeEventListener('pointercancel', up);
  };
  splitter.addEventListener('pointermove', move);
  splitter.addEventListener('pointerup', up);
  splitter.addEventListener('pointercancel', up);
});

/* A separator that only responds to a mouse is not a control. Arrows nudge,
   Home/End jump to the limits, and double-click resets to the default rather
   than leaving the user to hunt for it. */
splitter.addEventListener('keydown', (ev) => {
  const now = parseFloat(splitter.getAttribute('aria-valuenow'));
  const to = { ArrowLeft: now - 2, ArrowRight: now + 2,
               Home: MIN_PCT, End: MAX_PCT }[ev.key];
  if (to === undefined) return;
  ev.preventDefault();
  setSplit(to);
});
splitter.addEventListener('dblclick', () => setSplit(50));

/* --- run options --------------------------------------------------------- */
/* The popover floats over the transcript, so the feed has to know how tall the
   dock is or the last events sit underneath it unreachable. Measured rather
   than assumed, because the field grows with the task text. */
const dockEl = document.querySelector('.dock');
new ResizeObserver(([entry]) => {
  document.documentElement.style.setProperty('--dock-h', entry.contentRect.height + 'px');
}).observe(dockEl);

const optsBtn = $('opts-btn'), optsBox = $('opts');
function setOpts(open) {
  optsBox.hidden = !open;
  optsBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) optsBox.querySelector('input').focus();
}
optsBtn.onclick = (e) => { e.stopPropagation(); setOpts(optsBox.hidden); };
// click-away and Escape, because a popover you can only close with the button
// that opened it is a trap
document.addEventListener('click', (e) => {
  if (!optsBox.hidden && !optsBox.contains(e.target) && e.target !== optsBtn) setOpts(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !optsBox.hidden) { setOpts(false); optsBtn.focus(); }
});


/* ------------------------------------------------------ conversations --- */

/* A thread is the unit the app is organised around; a run is one turn inside
   it. The sidebar lists threads, the feed shows the turns, and the live run
   streams under the newest one. */
async function loadThreads() {
  if (!S.agent) return;
  let list = [];
  try {
    list = (await api(`/api/threads?agent=${encodeURIComponent(S.agent)}`)).threads;
  } catch (err) { return; }
  /* Reopen where you left off. A local app with one user has no reason to
     greet a returning session with a blank page it has to be told about. */
  if (S.thread === null && !S.run && list.length && !S.resumed) {
    S.resumed = true;
    openThread(list[0].id);
    return;
  }
  const box = $('threads');
  box.textContent = '';
  $('threads-none').classList.toggle('hidden', list.length > 0);
  for (const t of list) {
    const row = el('button', 'thread-row' + (t.id === S.thread ? ' on' : ''));
    row.type = 'button';
    row.setAttribute('role', 'listitem');
    row.append(el('span', 'thread-title', t.title));
    const del = el('button', 'thread-del', '✕');
    del.type = 'button';
    del.title = 'Delete this conversation';
    del.onclick = async (ev) => {
      ev.stopPropagation();
      await post('/api/thread/delete', { agent: S.agent, id: t.id });
      if (S.thread === t.id) newChat();
      loadThreads();
    };
    row.append(del);
    row.onclick = () => openThread(t.id);
    box.append(row);
  }
}

async function openThread(id) {
  S.thread = id;
  let msgs = [];
  try {
    msgs = (await api(`/api/thread?agent=${encodeURIComponent(S.agent)}&id=${id}`)).messages;
  } catch (err) { /* fall through to an empty thread */ }
  $('timeline').textContent = '';          // the previous turn's reasoning
  renderThread(msgs);
  loadThreads();
}

function newChat() {
  S.thread = null;
  $('timeline').textContent = '';
  $('thread').textContent = '';
  $('empty').classList.remove('hidden');
  loadThreads();
  $('task').focus();
}

function renderThread(msgs) {
  const box = $('thread');
  box.textContent = '';
  for (const m of msgs) {
    const turn = el('div', `turn ${m.role}`);
    turn.append(el('div', 'turn-text', m.text));
    box.append(turn);
  }
  $('empty').classList.toggle('hidden', msgs.length > 0);
  box.scrollIntoView({ block: 'end' });
}

/* The reply lands in the thread only once the run has closed, because that is
   when the server has written it. Re-reading is cheaper than duplicating the
   server's rule for what counts as the answer. */
async function refreshThread() {
  if (!S.thread) return;
  try {
    const d = await api(`/api/thread?agent=${encodeURIComponent(S.agent)}&id=${S.thread}`);
    renderThread(d.messages);
  } catch (err) { /* leave what is on screen */ }
  loadThreads();
}

/* ----------------------------------------------- real accounts (mcp) --- */

/* The registry, fetched once — mcp/servers.json is static for the life of the
   process. Rows reuse .menu-row so a server reads as one more switch in the
   menu rather than a form bolted into it. */
async function loadMcp() {
  let servers;
  try {
    servers = await api('/api/mcp');
  } catch (err) {
    return;                       // no registry is not a reason to break the menu
  }
  const box = $('opt-mcp');
  box.textContent = '';
  for (const s of servers) {
    // Built as nodes, not markup: the setup text goes into a title attribute and
    // esc() only covers &<>, so a quote in servers.json would break out of it.
    const row = el('label', 'menu-row mcp-row');
    row.title = s.setup;
    const cb = el('input');
    cb.type = 'checkbox';
    cb.className = 'mcp-server';
    cb.value = s.name;
    const label = el('span', 'menu-label', s.name);
    label.append(el('em', null, s.summary));
    const tick = el('span', 'tick', '✓');
    tick.setAttribute('aria-hidden', 'true');
    row.append(cb, label, tick);
    box.append(row);
  }
  paintOptDots();
}

function mcpSelected() {
  return [...document.querySelectorAll('.mcp-server:checked')].map((el) => el.value);
}

/* The popover closes and takes any memory of what is switched on with it, so
   the count stays behind on the bar. */
const OPT_LABELS = { 'opt-shell': 'shell', 'opt-yolo': 'no confirm',
                     'opt-office': 'office', 'opt-tiers': 'tiers' };
function paintOptDots() {
  const on = Object.keys(OPT_LABELS).filter((id) => $(id).checked).map((id) => OPT_LABELS[id]);
  const root = $('opt-root').value.trim();
  /* The limit lives in preferences now, but it still changes how a run behaves,
     so it stays visible on the bar next to the run options. */
  const calls = $('opt-calls').value.trim();
  if (root) on.unshift('folder');
  if (calls) on.push(`${calls} calls`);
  /* Real accounts lead the summary. Everything else here changes how the agent
     works; this is the only one that decides whether it can touch live mail. */
  const conn = mcpSelected();
  const mode = $('opt-mcp-mode').value;
  if (conn.length) on.unshift(mode === 'live' ? `${conn.length} live` : `${conn.length} real`);
  $('conn-state').textContent = conn.length
    ? `${conn.length} · ${mode === 'read_only' ? 'read only' : mode}` : 'none';
  $('conn-state').classList.toggle('hot', conn.length > 0 && mode === 'live');
  $('opt-dots').textContent = on.join(' · ');
  /* The two rows that take a value show it on the row, so the menu still reads
     as set or unset once it is closed and reopened. */
  $('root-val').textContent = root ? root.replace(/^.*\//, '') || root : 'simulated';
}
optsBox.addEventListener('input', paintOptDots);
optsBox.addEventListener('change', paintOptDots);

/* The call limit persists like the other preferences: it is a standing choice
   about how long this machine is willing to let a run go, not a per-task one.
   It lives down here rather than with the rest of the preferences because it
   paints the options summary, and that reads OPT_LABELS above.

   Blank is a real value and means "the model's own default", so it stores as a
   removal rather than an empty string. The clamp here is a courtesy to the
   person typing; runner.call_budget is what actually holds the line, since the
   HTTP API takes a number from anywhere, not just from this input. */
function setCalls(raw) {
  const n = parseInt(raw, 10);
  // An out-of-range number is clamped and shown, not cleared: typing 5000 and
  // watching the field empty itself reads as a bug rather than as a limit.
  const v = Number.isFinite(n) ? String(Math.min(200, Math.max(2, n))) : '';
  $('opt-calls').value = v;
  store('agentlab-calls', v || null);
  paintOptDots();
}
setCalls(stored('agentlab-calls'));
$('opt-calls').onchange = (e) => setCalls(e.target.value);

/* The field grows with the text instead of scrolling inside two fixed rows. */
const taskBox = $('task');
function growTask() {
  // Empty means back to the CSS resting height, not "whatever it grew to last
  // time". The inline height was set unconditionally, so once the box had been
  // tall it stayed tall even with nothing in it, and on a short window an empty
  // composer could hold a third of the screen.
  if (!taskBox.value) {
    taskBox.style.height = '';
    return;
  }
  taskBox.style.height = 'auto';
  // Also bounded by the window: 180px is reasonable on a desktop and absurd in
  // a 700px-tall pane, where it plus the button row is a third of everything.
  const cap = Math.min(180, Math.round(window.innerHeight * 0.22));
  taskBox.style.height = Math.min(taskBox.scrollHeight, cap) + 'px';
}
taskBox.addEventListener('input', growTask);
// A window that got shorter has to re-clamp, or the cap only ever applied to
// the size the window happened to be while typing.
window.addEventListener('resize', growTask);

/* Enter sends, Shift+Enter writes a newline, the way every chat box works.
   IME composition is exempt: mid-composition Enter commits the candidate word
   and must not also fire the run, which is how a Korean or Japanese task gets
   sent half-typed. */
taskBox.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  if (!$('run').disabled) startRun();
});

/* --- the two side panels ------------------------------------------------- */
/* Both closed by default. The conversation is the product; a rail of models
   and a pane of files you have not made yet are furniture around it. */
function setWorkspace(open) {
  document.body.classList.toggle('ws-open', open);
  $('ws-btn').setAttribute('aria-pressed', open ? 'true' : 'false');
  if (open) $('ws-count').classList.add('hidden');
}
function setRail(open) {
  document.body.classList.toggle('rail-open', open);
}
$('ws-btn').onclick = () => setWorkspace(!document.body.classList.contains('ws-open'));
/* The rail holds conversations now, not just a list of models, so it starts
   open: which thread you are in is the first thing the app should answer. */
$('rail-btn').onclick = () => {
  const open = !document.body.classList.contains('rail-open');
  setRail(open);
  $('rail-btn').setAttribute('aria-pressed', open ? 'true' : 'false');
};
document.body.classList.add('rail-open');
$('rail-close').onclick = () => setRail(false);
$('model').addEventListener('change', (e) => {
  if (e.target.value !== MORE) return;
  setRail(true);
  syncModel();          // put the picker back on the model actually in use
});

/* Steps only. Pure presentation: nothing is dropped from the DOM, so toggling
   back mid-run loses nothing and the filter costs one class on <body>. */
const stepsToggle = $('steps-toggle');
stepsToggle.onchange = () => {
  document.body.classList.toggle('steps-only', stepsToggle.checked);
  autoscroll();
};

/* --------------------------------------------------------------- boot --- */

$('run').onclick = startRun;
$('stop').onclick = () => post('/api/stop').catch(() => {});
$('viewer-close').onclick = closeViewer;
$('viewer').onclick = (e) => { if (e.target === $('viewer')) closeViewer(); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeViewer();
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) startRun();
});

$('reveal').onclick = () => post('/api/reveal', { agent: S.agent }).catch((e) => alert(e.message));
$('reset').onclick = async () => {
  if (!S.agent) return;
  const a = S.agents.find((x) => x.id === S.agent);
  if (!confirm(`Factory-reset ${a.name}?\n\nThis clears its inbox and calendar back to the ` +
               `starting fixtures, deletes the files it created, and erases everything ` +
               `it has learned. Past run transcripts are kept.`)) return;
  await post('/api/reset', { agent: S.agent, what: ['world', 'memory', 'files'] });
  S.seen = {};
  S.first = true;
  await loadWorkspace();
  await loadAgents(true);
};

$('agent-filter').addEventListener('input', renderAgents);
paintOptDots();
growTask();

loadAgents();
loadMcp();
$('new-chat').onclick = newChat;
setInterval(() => { if (!S.run) loadAgents(true); }, 20000);

/* Registering the worker is what makes the browser offer "Install app". Nothing
   on the page needs it, so a failure is silent — an old browser, or the
   pywebview window, still works normally. */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

/* ------------------------------------------------------------------ setup ---
   Shown only when something a run needs is missing. The checks come from
   webui/preflight.py; every button here maps to one named action there, so the
   page can install this interpreter's packages, start Ollama or pull a model,
   and nothing else. Installing Ollama itself is a link the user clicks, since
   a local web page should not put a system binary on their machine behind
   their back. */
const SETUP = { polling: null };

function setupStep(c, i) {
  const li = el('li', `setup-step ${c.state}`);
  const mark = c.state === 'ok' ? '✓' : c.state === 'warn' ? '!' : String(i + 1);
  li.append(el('span', 'setup-dot', mark));
  const body = el('div', 'setup-body');
  body.append(el('div', 'setup-name', c.title));
  body.append(el('div', 'setup-detail', c.detail));
  li.append(body);
  if (c.fix === 'open_url' && c.url) {
    const a = el('a', 'ghost small', c.fix_label);
    a.href = c.url; a.target = '_blank'; a.rel = 'noreferrer';
    li.append(a);
  } else if (c.fix) {
    const b = el('button', 'ghost small', c.fix_label);
    b.type = 'button';
    b.onclick = async () => {
      b.disabled = true; b.textContent = 'Working…';
      try {
        await post('/api/setup/fix', { action: c.fix, tag: c.tag || '' });
      } catch (e) {
        b.textContent = 'Failed'; b.title = e.message; return;
      }
      // Starting Ollama and pip both take a moment to become true.
      setTimeout(checkSetup, c.fix === 'pull_model' ? 400 : 2500);
    };
    li.append(b);
  }
  return li;
}

async function checkSetup(force) {
  let data;
  try {
    data = await api('/api/setup');
  } catch { return; }            // server not up yet; the next poll will do
  const pane = $('setup');
  if (data.ready && !force && pane.hidden) return;   // installed machine: never shown
  $('setup-steps').replaceChildren(...data.checks.map(setupStep));

  const pull = data.pull || {};
  const pulling = pull.tag && !pull.done;
  $('setup-pull').hidden = !(pulling || pull.error);
  if (pulling) {
    $('setup-bar-fill').style.width = `${pull.percent || 0}%`;
    $('setup-pull-text').textContent =
      `${pull.status || 'downloading'} ${pull.percent ? pull.percent + '%' : ''}`;
  } else if (pull.error) {
    $('setup-pull-text').textContent = `Download failed: ${pull.error}`;
  }

  $('setup-done').disabled = !data.ready;
  if (data.ready && !pulling) $('setup-done').textContent = 'Start using it';
  pane.hidden = data.ready && !pane.dataset.sticky;

  // Poll only while something is in flight, so an idle app is not hitting the
  // server every second forever.
  const busy = pulling || (!data.ready && !pane.hidden);
  if (busy && !SETUP.polling) SETUP.polling = setInterval(checkSetup, 1500);
  if (!busy && SETUP.polling) { clearInterval(SETUP.polling); SETUP.polling = null; }
}

$('setup-recheck').onclick = () => checkSetup(true);
$('setup-done').onclick = () => {
  $('setup').hidden = true;
  delete $('setup').dataset.sticky;
  loadAgents(true);
};
checkSetup();

/* --------------------------------------------------------------- new mail ---
   The agent is told that waiting is not a step: nothing arrives while it is
   working, so it must not sit and poll the inbox. That leaves someone needing
   to notice when mail DOES turn up, and comparing two lists of ids is the
   app's job, not the model's. Clicking clears the flag. */
async function checkMail() {
  if (!S.agent) return;
  let data;
  try { data = await api(`/api/mail?agent=${encodeURIComponent(S.agent)}`); }
  catch { return; }
  const pill = $('mailflag');
  pill.hidden = !data.count;
  if (!data.count) return;
  const who = data.new.map((m) => String(m.from || '').split('@')[0]).filter(Boolean);
  pill.textContent = data.count === 1
    ? `1 new email${who[0] ? ` from ${who[0]}` : ''}`
    : `${data.count} new emails${who.length ? ` from ${who.slice(0, 2).join(', ')}` : ''}`;
  pill.title = data.new.map((m) => `${m.from}: ${m.subject}`).join('\n');
}

$('mailflag').onclick = async () => {
  try { await post('/api/mail/seen', { agent: S.agent }); } catch {}
  $('mailflag').hidden = true;
};
checkMail();
setInterval(checkMail, 20000);
