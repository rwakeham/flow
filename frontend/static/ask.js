// ═══════════════════════════════════════════════════════════════════════════════
// ASSISTANT (code name "ask"): a chat panel answered from Flow's own data, which can also
// propose changes. Proposals are cards the user applies; the browser sends each one to Flow's
// normal endpoint, which validates it (see backend/ask_proposals.py).
// Answers are rendered by building DOM nodes; model text never goes through innerHTML.
// ═══════════════════════════════════════════════════════════════════════════════

const ASK_URL = '/api/ask';
const ASK_STORE = 'flow-ask:' + ASK_URL;
const ASK = {status: null, busy: false, items: [], attachment: null};   // attachment: the file stays in memory only

function askEl(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) el.append(kid);
  return el;
}

// ── Storage (this tab only) ───────────────────────────────────────────────────
function askLoad() {
  try { ASK.items = JSON.parse(sessionStorage.getItem(ASK_STORE) || '[]'); } catch (e) { ASK.items = []; }
}
function askSave() {
  try { sessionStorage.setItem(ASK_STORE, JSON.stringify(ASK.items)); } catch (e) { /* storage blocked */ }
}
function askHistory() {
  return ASK.items.filter(m => m.keep).map(m => ({role: m.role, text: m.text}));
}

// ── Status, button, meter ─────────────────────────────────────────────────────
async function askRefreshStatus() {
  try {
    const res = await fetch(ASK_URL + '/status');
    if (!res.ok) return;
    ASK.status = await res.json();
  } catch (e) { return; }
  document.getElementById('ask-btn').classList.toggle('hidden', !ASK.status.enabled);
  if (!ASK.status.enabled) askClose();
  askRenderMeter();
}

function askSetMeter(m) {
  if (!m || !ASK.status) return;
  Object.assign(ASK.status, m);
  askRenderMeter();
}

function askMonthName(ym, offset = 0) {
  const [y, mo] = ym.split('-').map(Number);
  return new Date(y, mo - 1 + offset, 1).toLocaleDateString('en-US', {month: 'long'});
}

function askRenderMeter() {
  const box = document.getElementById('ask-meter');
  if (!box || !ASK.status || !ASK.status.month) return;
  const {month, this_month: n, last_month: prev} = ASK.status;
  box.replaceChildren(askEl('div', {class: 'row'}, askEl('span', {class: 'grow'},
    `${n} message${n === 1 ? '' : 's'} sent in ${askMonthName(month)}` +
    (prev ? ` · ${prev} in ${askMonthName(month, -1)}` : ''))));
  if (ASK.status.paused) box.append(askEl('div', {class: 'ask-note error', style: 'margin-top:6px'}, '⚠ AI paused: ' + ASK.status.paused));
}

// ── Open / close ──────────────────────────────────────────────────────────────
function askToggle() {
  if (document.getElementById('ask-panel').classList.contains('hidden')) askOpen(); else askClose();
}
function askOpen() {
  document.getElementById('ask-panel').classList.remove('hidden');
  document.getElementById('ask-btn').classList.add('open');
  document.body.classList.add('ask-open');
  askLoad();
  askRenderLog();
  askRefreshStatus();
  document.getElementById('ask-q').focus();
}
function askClose() {
  const p = document.getElementById('ask-panel');
  if (!p) return;
  p.classList.add('hidden');
  document.getElementById('ask-btn').classList.remove('open');
  document.body.classList.remove('ask-open');
}
function askNewChat() {
  ASK.items = [];
  askDetach();
  askSave();
  askRenderLog();
}

// ── Page context and suggestions ──────────────────────────────────────────────
function askPage() {
  const section = Object.keys(SECTIONS).find(n => !document.getElementById(SECTIONS[n]).classList.contains('hidden')) || 'dashboard';
  const page = {section};
  if (section === 'cards') {
    page.tab = CD.tab;
    if (CD.tab === 'categories' && CD.catDetail) page.category = CD.catDetail;
    if (CD.tab === 'merchants' && CD.merchantDetail) page.merchant = CD.merchantDetail;
    const v = document.getElementById('cd-card-filter').value;
    if (v) page.card_id = parseInt(v, 10);
  }
  if (section === 'cashflow' && cfCurrentId) page.account_id = cfCurrentId;
  return page;
}

function askSuggestions() {
  const p = askPage();
  if (p.section === 'cashflow') return ["What's coming out of this account in the next 30 days?",
    'How did money in and out compare over the last 3 months?', 'Who are my largest payees from this account?'];
  if (p.section === 'cards') {
    const byTab = {
      monthly: ['Why was my highest month so high?', 'How does last month compare with my typical month?'],
      subscriptions: ['Which subscriptions could I cancel?', 'Did any subscription prices go up?'],
      merchants: ['Which merchants am I spending more at lately?', 'Where do I shop most often?'],
      categories: ['What are my biggest spending categories?', 'Which categories grew the most recently?'],
      transactions: ['What were my largest charges last month?', 'Do I have any duplicate-looking charges?'],
    };
    if (p.tab === 'merchants' && p.merchant) return [`How much do I spend at ${p.merchant} each month?`,
      `Is ${p.merchant} in the right category?`, `Has my spending at ${p.merchant} gone up lately?`];
    if (p.tab === 'categories' && p.category) return [`What's driving my ${p.category} spending?`,
      `How has ${p.category} changed over the last few months?`, 'Where can I easily trim spending?'];
    if (p.tab === 'review') return ['Which of my uncategorized merchants can you sort out?', 'Where can I easily trim spending?'];
    return [...(byTab[p.tab] || ['Where is most of my money going?']), 'Where can I easily trim spending?'];
  }
  return ['Where can I easily trim spending?', 'How much did I spend on my cards last month?', 'What subscriptions am I paying for?'];
}

// ── Rendering ─────────────────────────────────────────────────────────────────
function askInline(line) {
  const frag = document.createDocumentFragment();
  line.split(/(\*\*[^*]+\*\*)/).forEach(part => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) frag.append(askEl('strong', {}, part.slice(2, -2)));
    else if (part) frag.append(document.createTextNode(part));
  });
  return frag;
}

function askFormat(text) {
  const frag = document.createDocumentFragment();
  let list = null;
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-•]\s+(.*)$/);
    if (bullet) {
      if (!list) { list = askEl('ul'); frag.append(list); }
      list.append(askEl('li', {}, askInline(bullet[1])));
    } else {
      list = null;
      if (line.trim()) frag.append(askEl('p', {}, askInline(line)));
    }
  }
  return frag;
}

// Also used by the Dashboard's Needs attention rows. nav.action opens a dialog instead of a page:
// {action:'settings', section} a Settings section; balances = Update balances; import; formats = the formats list.
const ASK_ACTIONS = {
  settings: nav => settingsOpen(nav.section),
  balances: () => balOpen(),
  import: () => openImport(),
  formats: () => ipOpenList(),
};
function askNavigate(nav) {
  if (!nav) return;
  if (nav.action) {
    const fn = ASK_ACTIONS[nav.action];
    if (fn) fn(nav);
    return;
  }
  if (nav.section === 'dashboard') {
    showSection('dashboard');
    const btn = nav.tab && [...document.querySelectorAll('.tab-btn')].find(b => (b.getAttribute('onclick') || '').includes(`'${nav.tab}'`));
    if (btn) btn.click();
    if (nav.payees && typeof cfwOpenPayees === 'function') setTimeout(() => cfwOpenPayees(nav.payees === true ? 'needs' : 'all', nav.payees === true ? '' : nav.payees), 300);
    if (nav.accounts && !document.getElementById('acct-body').classList.contains('open')) toggleAcctPanel();
    return;
  }
  if (nav.section === 'cashflow') {
    if (!nav.account) { showSection('cashflow'); return; }
    openRegisterAccount(nav.account).then(() => { if (nav.reconcile && !cfReconciling) toggleReconcile(); });
    return;
  }
  if (nav.section === 'cards') {
    CD.pendingAccount = nav.card ? String(nav.card) : '';
    const period = document.getElementById('cd-period');
    if (period) period.value = 'all';
    if (nav.tab === 'transactions') {
      CD.tf = {q: nav.q || '', l1: nav.l1 || '', l2: nav.l2 || '', type: 'spend', source: '', recurring: false, merchant: ''};
      CD.page = 1;
    }
    CD.tab = nav.tab || 'overview';
    showSection('cards');
  }
}

function askRenderItem(m) {
  if (m.role === 'user') return askEl('div', {class: 'ask-msg user'}, m.text,
    m.file ? askEl('div', {class: 'ask-msg-file'}, '📎 ' + m.file) : null);
  if (m.kind === 'declined') return askEl('div', {class: 'ask-note'}, m.text);
  const box = askEl('div', {class: 'ask-msg assistant'}, askFormat(m.text));
  if (m.unverified && m.unverified.length) {
    box.append(askEl('div', {class: 'ask-unverified'},
      `Couldn't find ${m.unverified.join(', ')} in the data. Check against the pages below.`));
  }
  if (m.sources && m.sources.length) {
    const row = askEl('div', {class: 'ask-sources'}, 'From:');
    m.sources.forEach(s => row.append(askEl('button', {onclick: () => askNavigate(s.nav)}, s.label)));
    box.append(row);
  }
  if (m.proposals && m.proposals.length) {
    m.proposals.forEach(p => box.append(askProposalCard(m, p)));
    const open = m.proposals.filter(p => !p.state && !p.action);
    if (open.length > 1) box.append(askEl('div', {class: 'ask-prop-all'},
      askEl('button', {class: 'btn-primary btn-sm', onclick: () => askApplyAll(m)}, `Apply all ${open.length}`)));
  }
  return box;
}

// ── Proposals ─────────────────────────────────────────────────────────────────
function askProposalCard(m, p) {
  const card = askEl('div', {class: 'ask-prop' + (p.state ? ' ' + p.state : '')},
    askEl('div', {class: 'ask-prop-title'}, p.title));
  if (p.reason) card.append(askEl('div', {class: 'ask-prop-reason'}, p.reason));
  const ul = askEl('ul');
  p.lines.forEach(l => ul.append(askEl('li', {}, l)));
  card.append(ul);
  if (p.error) card.append(askEl('div', {class: 'ask-note error'}, p.error));
  const row = askEl('div', {class: 'ask-prop-actions'});
  if (p.state === 'applied') row.append(askEl('span', {class: 'ask-prop-done'}, '✓ Applied'));
  else if (p.state === 'dismissed') row.append(askEl('span', {class: 'ask-prop-done'}, 'Dismissed'));
  else if (p.action === 'open_import_editor') {
    row.append(askEl('button', {class: 'btn-primary btn-sm', onclick: () => askOpenImportEditor(m, p)}, 'Open in the format editor'),
               askEl('button', {class: 'btn-sm', onclick: () => { p.state = 'dismissed'; askSave(); askRenderLog(); }}, 'Dismiss'));
  } else {
    row.append(askEl('button', {class: 'btn-primary btn-sm', onclick: () => askApply(m, p)}, 'Apply'),
               askEl('button', {class: 'btn-sm', onclick: () => { p.state = 'dismissed'; askSave(); askRenderLog(); }}, 'Dismiss'));
  }
  if (p.open && p.state !== 'dismissed') row.append(askEl('button', {class: 'btn-sm', onclick: () => askOpenProposal(p)},
    p.state === 'applied' ? 'View' : 'Edit in Flow'));
  card.append(row);
  return card;
}

async function askApply(m, p, quiet = false) {
  p.error = '';
  try {
    const res = await fetch(p.apply.url, {method: p.apply.method, body: JSON.stringify(p.apply.body),
      headers: {'Content-Type': 'application/json', 'X-Flow-Via': 'assistant'}});   // the change log says who did it
    if (!res.ok) {
      let detail = 'Could not apply (' + res.status + ').';
      try { const j = await res.json(); detail = typeof j.detail === 'string' ? j.detail : detail; } catch (e) { /* not JSON */ }
      throw new Error(detail);
    }
    p.state = 'applied';
  } catch (e) { p.error = e.message; }
  askSave();
  if (!quiet) { askRenderLog(); askAfterChange(); }
  return !p.error;
}

async function askApplyAll(m) {
  for (const p of m.proposals.filter(x => !x.state && !x.action)) {     // in order: a new subcategory before what uses it
    if (!(await askApply(m, p, true))) break;
  }
  askRenderLog();
  askAfterChange();
}

function askAfterChange() {
  // The Cards page may show what just changed: reload its category list and the open tab.
  if (typeof cdReloadTaxonomy === 'function' && typeof CD !== 'undefined' && CD.ready) {
    cdReloadTaxonomy().then(() => { if (!document.getElementById('cards-app').classList.contains('hidden')) cdRefresh(); });
  }
}

function askOpenProposal(p) {
  const o = p.open || {};
  if (o.kind === 'merchant') { showSection('cards'); setTimeout(() => cdShowMerchant(o.name), 50); }
  else if (o.kind === 'category') { showSection('cards'); setTimeout(() => cdShowCategory(o.name), 50); }
  else if (o.kind === 'subscriptions') askNavigate({section: 'cards', tab: 'subscriptions'});
  else if (o.kind === 'settings') askNavigate({section: 'cards', tab: 'settings'});
  else if (o.kind === 'payees') askNavigate({section: 'dashboard', tab: 'cashflow', payees: o.name || true});
}

function askRenderLog() {
  const log = document.getElementById('ask-log');
  log.replaceChildren();
  if (!ASK.items.length) {
    log.append(askEl('div', {class: 'ask-empty'},
      'Ask about your card spending and cash accounts, or ask it to organize things ("Maple Street Studio is my ' +
      'pottery class, not shopping"); changes come back as proposals you apply. Answers come only from Flow\'s data; Claude sees summaries ' +
      'with cards shown as "Card A", and every request is in the AI privacy log (Settings → AI in the top bar).'));
    const sug = askEl('div', {class: 'ask-suggest'}, askEl('div', {class: 'lbl'}, 'Try'));
    askSuggestions().forEach(q => sug.append(askEl('button', {onclick: () => askSend(q)}, q)));
    log.append(sug);
  }
  ASK.items.forEach(m => log.append(askRenderItem(m)));
  log.scrollTop = log.scrollHeight;
}

// ── An attached statement file (import formats) ───────────────────────────────
// The file is read on Flow's server (POST /api/ask/attach) into an outline (column shapes, cleaned
// headers) and a draft format. Only the outline and the draft's summary (enums and column numbers)
// go with questions; the file itself and the full draft stay in this page's memory, for the editor.
async function askAttach(file) {
  const box = document.getElementById('ask-attached');
  box.classList.remove('hidden');
  box.replaceChildren(askEl('span', {class: 'ask-note'}, `Reading ${file.name}…`));
  if (file.size > 10 * 1024 * 1024) { box.replaceChildren(askEl('span', {class: 'ask-note error'}, 'That file is over 10 MB.')); return; }
  const fd = new FormData(); fd.append('file', file);
  try {
    const res = await fetch(ASK_URL + '/attach', {method: 'POST', body: fd});
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Could not read the file.');
    ASK.attachment = {file, outline: d.outline, summary: d.summary, draft: d.draft};
  } catch (e) {
    ASK.attachment = null;
    box.replaceChildren(askEl('span', {class: 'ask-note error'}, e.message),
                        askEl('button', {class: 'btn-sm', onclick: askDetach}, '✕'));
    return;
  }
  askRenderAttachment();
  const input = document.getElementById('ask-q');
  if (!input.value.trim()) input.value = 'Set up an import format for this file.';
  input.focus();
}
function askRenderAttachment() {
  const box = document.getElementById('ask-attached');
  const a = ASK.attachment;
  if (!a) { box.classList.add('hidden'); box.replaceChildren(); return; }
  const n = a.outline.columns.length;
  box.classList.remove('hidden');
  box.replaceChildren(askEl('span', {class: 'grow'}, `📎 ${a.file.name} · ${n} column${n === 1 ? '' : 's'}`),
    askEl('span', {class: 'ask-note'}, 'Claude sees only column shapes and names, never the values.'),
    askEl('button', {class: 'btn-sm', 'aria-label': 'Remove the attached file', onclick: askDetach}, '✕'));
}
function askDetach() { ASK.attachment = null; askRenderAttachment(); }

// The proposal's columns (by number) on top of the full local draft, opened in the format editor.
function askImportSpec(prop, draft) {
  const base = JSON.parse(JSON.stringify((draft && draft.spec) || {has_header: false, columns: {}}));
  const grid = (draft && draft.grid) || [];
  const header = base.has_header && base.header_row != null ? (grid[base.header_row] || []) : null;
  base.kind = prop.kind;
  base.columns = {};
  for (const [role, idx] of Object.entries(prop.columns))
    base.columns[role] = idx.map(i => header && header[i] ? {header: header[i], index: i} : {index: i});
  Object.assign(base, {amount_mode: prop.amount_mode, sign: prop.sign, date_format: prop.date_format,
                       decimal: prop.decimal, thousands: prop.thousands, skip_footer_rows: prop.skip_footer_rows});
  return base;
}
async function askOpenImportEditor(m, p) {
  const a = ASK.attachment;
  if (!a || a.file.name !== p.file) {
    p.error = `Attach ${p.file || 'the file'} again (📎) to open this format with it.`;
    askSave(); askRenderLog(); return;
  }
  p.error = '';
  const prop = p.import_profile;
  const spec = askImportSpec(prop, a.draft);
  await ipOpenEditor({file: a.file, kind: prop.kind, spec, draft: a.draft ? {...a.draft, spec} : null,
    name: prop.name, institution: prop.institution,
    onSaved: () => { p.state = 'applied'; askSave(); askRenderLog(); }});
}

// ── Sending ───────────────────────────────────────────────────────────────────
async function askSend(preset) {
  if (ASK.busy) return;
  const input = document.getElementById('ask-q');
  // A pasted account summary goes to the balances dialog, which reads it locally:
  // its amounts and account numbers are never sent to the chat model.
  if (!preset && typeof balLooksLikeSummary === 'function' && balLooksLikeSummary(input.value)) {
    const text = input.value;
    input.value = '';
    balOpen('paste', text);
    return;
  }
  const att = ASK.attachment;
  const question = (preset || input.value).trim().slice(0, 1000) || (att ? 'Set up an import format for this file.' : '');
  if (!question) return;
  input.value = '';
  ASK.busy = true;
  document.getElementById('ask-send').disabled = true;

  const userItem = {role: 'user', text: question, keep: true, file: att ? att.file.name : undefined};
  ASK.items.push(userItem);
  askRenderLog();
  const log = document.getElementById('ask-log');
  const pending = askEl('div', {class: 'ask-msg assistant'});
  const status = askEl('div', {class: 'ask-status'}, 'Thinking…');
  const draft = askEl('div', {class: 'draft'});
  pending.append(status, draft);
  log.append(pending);
  log.scrollTop = log.scrollHeight;

  let done = null;
  const fail = message => { done = {type: 'error', message}; };
  try {
    const res = await fetch(ASK_URL, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({messages: askHistory(), page: askPage(),
                            attachment: att ? {outline: att.outline, summary: att.summary} : undefined})});
    if (!res.ok) {
      let detail = 'Request failed (' + res.status + ').';
      try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
      fail(detail);
    } else {
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (!done) {
        const {value, done: eof} = await reader.read();
        if (value) buf += dec.decode(value, {stream: true});
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line); } catch (e) { continue; }
          if (ev.type === 'status') status.textContent = ev.text + '…';
          else if (ev.type === 'text') { draft.textContent += ev.text; status.textContent = ''; }
          else if (ev.type === 'reset') draft.textContent = '';
          else if (['final', 'declined', 'error'].includes(ev.type)) done = ev;
          log.scrollTop = log.scrollHeight;
        }
        if (eof) break;
      }
      if (!done) fail('The answer stopped before it finished.');
    }
  } catch (e) {
    fail('Could not reach Flow.');
  }

  if (done.meter) askSetMeter(done.meter);
  if (done.type === 'final') {
    const props = (done.proposals || []).map(p => p.action && att ? {...p, file: att.file.name} : p);
    ASK.items.push({role: 'assistant', text: done.text, sources: done.sources, unverified: done.unverified,
                    proposals: props, keep: true});
  } else if (done.type === 'declined') {
    userItem.keep = false;              // declined questions are not sent back as history
    ASK.items.push({role: 'assistant', kind: 'declined', text: done.message, keep: false});
  } else {
    ASK.items.pop();                    // failed: drop the question and put it back in the box
    input.value = question;
  }
  askSave();
  askRenderLog();
  if (done.type === 'error') {
    document.getElementById('ask-log').append(askEl('div', {class: 'ask-note error'}, done.message));
    if (done.paused) askRefreshStatus();
  }
  ASK.busy = false;
  document.getElementById('ask-send').disabled = false;
  input.focus();
}

function askKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); askSend(); }
}

// ── Wiring ────────────────────────────────────────────────────────────────────
(function askInit() {
  document.getElementById('ask-btn').addEventListener('click', askToggle);
  document.getElementById('ask-close').addEventListener('click', askClose);
  document.getElementById('ask-new').addEventListener('click', askNewChat);
  document.getElementById('ask-balances').addEventListener('click', () => balOpen('paste'));
  document.getElementById('ask-send').addEventListener('click', () => askSend());
  document.getElementById('ask-q').addEventListener('keydown', askKey);
  document.getElementById('ask-file').addEventListener('change', e => { const f = e.target.files[0]; e.target.value = ''; if (f) askAttach(f); });
  const panel = document.getElementById('ask-panel');
  panel.addEventListener('dragover', e => { if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); panel.classList.add('ask-drop'); } });
  panel.addEventListener('dragleave', e => { if (e.target === panel) panel.classList.remove('ask-drop'); });
  panel.addEventListener('drop', e => {
    panel.classList.remove('ask-drop');
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) { e.preventDefault(); askAttach(f); }
  });
  // Suggestions follow the page on screen.
  const origShow = showSection;
  showSection = function (section) {
    origShow(section);
    if (!document.getElementById('ask-panel').classList.contains('hidden') && !ASK.items.length) askRenderLog();
  };
  // The status is fetched after sign-in too (loadAll runs then).
  const origLoadAll = loadAll;
  loadAll = function (...args) { askRefreshStatus(); return origLoadAll(...args); };
  askRefreshStatus();
})();
