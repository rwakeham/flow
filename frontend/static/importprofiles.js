// Import formats (backend/import_profile.py): the format editor, the formats list, the
// "Import files" section of the account dialogs, and the import dialog's format problems.
//
// A format is a JSON spec saved on the server and linked to the accounts that use it.
// The editor works on a copy of the spec next to a sample file: the file's first rows on
// the left (click a row number to make it the header, pick each column's role above it),
// the settings on the right, and a live preview below (POST /api/import-profiles/preview)
// with the sign check. Nothing is saved until Save. Everything shown from a file or a
// format goes through escHtml.

const IP_ROLES = {
  cash: ['date', 'post_date', 'description', 'memo', 'amount', 'debit', 'credit', 'direction', 'running_balance',
         'check_number', 'status', 'reconciled'],
  card: ['date', 'post_date', 'description', 'memo', 'amount', 'debit', 'credit', 'direction', 'running_balance',
         'status', 'card_number', 'cardholder', 'issuer_category', 'issuer_type', 'reference', 'extended',
         'statement_as', 'city_state', 'country'],
  balance: ['account_ref', 'value', 'as_of'],
};
const IP_ROLE_LABELS = {
  date: 'Date', post_date: 'Posted date', description: 'Description', memo: 'Memo', amount: 'Amount',
  debit: 'Money out', credit: 'Money in', direction: 'In/out word', running_balance: 'Running balance',
  check_number: 'Check number', status: 'Status', reconciled: 'Reconciled', card_number: 'Card number',
  cardholder: 'Cardholder', issuer_category: 'Issuer category', issuer_type: 'Type', reference: 'Reference',
  extended: 'Extended details', statement_as: 'Appears on statement as', city_state: 'City/state', country: 'Country',
  account_ref: 'Account', value: 'Value', as_of: 'As of date',
};
const IP_KIND_LABELS = {cash: 'Bank statements', card: 'Card statements', balance: 'Balance files'};
const IP_DATE_LABELS = {MDY: 'Month first (03/04/2026 = Mar 4)', DMY: 'Day first (03/04/2026 = 3 Apr)', YMD: 'Year first (2026-03-04)'};

const ipNorm = s => String(s == null ? '' : s).toLowerCase().replace(/\s+/g, ' ').trim();
const ipList = s => String(s || '').split(',').map(x => x.trim()).filter(Boolean);
async function ipJson(url, opts) {
  const res = await fetch(url, opts);
  const d = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof d.detail === 'string' ? d.detail : (d.detail && d.detail.message) || 'Request failed';
    const err = new Error(msg); err.detail = d.detail; err.status = res.status; throw err;
  }
  return d;
}
const ipPost = (url, body) => ipJson(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const ipPut = (url, body) => ipJson(url, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

async function ipAccountsOf(kind) {
  const url = {cash: '/api/register/accounts', card: '/api/cards/accounts', balance: '/api/accounts'}[kind];
  const list = await ipJson(url).catch(() => []);
  return list.map(a => ({id: a.id, name: a.name}));
}

function ipWhat(p) {
  if (p.kind === 'card') return 'card statements';
  if (p.kind === 'balance') return 'balance files';
  return p.spec && p.spec.row_source === 'manual' ? 'ledger entries' : 'bank statements';
}
function ipWhen(iso) { return iso ? new Date(iso).toLocaleDateString() : 'never'; }

// ── The editor ───────────────────────────────────────────────────────────────

let IPE = null;   // the open editor's state

function _ipInjectEditor() {
  if (document.getElementById('ip-editor')) return;
  document.body.insertAdjacentHTML('beforeend', `
<div id="ip-editor" class="ip-overlay hidden"><div class="modal-box ip-ed" role="dialog" aria-labelledby="ip-ed-title">
  <div class="ip-ed-head"><h2 id="ip-ed-title">Import format</h2><button class="btn-sm" onclick="ipCloseEditor()" aria-label="Close">✕</button></div>
  <div id="ip-ed-msg" class="ip-note"></div>
  <div class="ip-ed-file"><span id="ip-ed-filename" class="ip-note"></span>
    <label class="btn-sm ip-filebtn">Choose a sample file…<input type="file" id="ip-ed-fileinput" accept=".csv,.tsv,.txt,.xlsx,.xlsm" onchange="ipEditorFile(this.files[0])"></label>
    <span id="ip-ed-kindwrap"></span>
    <button class="btn-sm hidden" id="ip-ed-ask" onclick="ipAskAssistant()" title="The Assistant sees the file's column shapes and names, never its values">Ask the Assistant</button></div>
  <div class="ip-ed-body">
    <div class="ip-ed-gridwrap"><div class="ip-sub">The file (click a row number to make it the header row; pick what each column holds)</div>
      <div id="ip-ed-grid" class="ip-ed-grid"></div></div>
    <div id="ip-ed-settings" class="ip-ed-settings"></div>
  </div>
  <div class="ip-sub">Preview</div>
  <div id="ip-ed-preview" class="ip-ed-preview"></div>
  <div class="ip-ed-save">
    <div class="ip-ed-names">
      <label>Name<input type="text" id="ip-ed-name" maxlength="120"></label>
      <label>Institution (optional)<input type="text" id="ip-ed-inst" maxlength="120"></label>
    </div>
    <div><div class="ip-sub">Use for</div><div id="ip-ed-accounts" class="ip-ed-accounts"></div></div>
  </div>
  <div id="ip-ed-err" class="ip-err"></div>
  <div class="modal-footer"><button onclick="ipCloseEditor()">Cancel</button>
    <button class="btn-primary" id="ip-ed-savebtn" onclick="ipSaveEditor()">Save format</button></div>
</div></div>`);
}

// opts: {file, kind, spec, draft, profile (saved format JSON), diff, account: {kind, id}, onSaved(profile), title}
async function ipOpenEditor(opts) {
  _ipInjectEditor();
  const profile = opts.profile || null;
  const kind = (opts.spec && opts.spec.kind) || (profile && profile.kind) || opts.kind || 'cash';
  IPE = {
    file: opts.file || null, kind, profile, diff: opts.diff || null, account: opts.account || null,
    onSaved: opts.onSaved || null, draft: opts.draft || null, grid: [], sheets: [],
    // A saved format's sign and date order were chosen when it was made; a new one must answer
    // the sign question and confirm an ambiguous date order before it can be saved.
    answered: !!opts.profile, datesConfirmed: !!opts.profile,
    spec: JSON.parse(JSON.stringify(opts.spec || (opts.draft && opts.draft.spec) || (profile && profile.spec) || ipBlankSpec(kind))),
    links: new Set(((profile && profile.links) || []).map(l => l.account_id)),
  };
  if (IPE.account && IPE.account.id) IPE.links.add(Number(IPE.account.id));
  IPE.originalLinks = new Set(IPE.links);
  document.getElementById('ip-ed-title').textContent = opts.title
    || (profile ? (opts.diff ? `Update import format: ${profile.name}` : `Edit import format: ${profile.name}`) : 'New import format');
  document.getElementById('ip-ed-name').value = profile ? profile.name
    : (opts.name || (IPE.file ? IPE.file.name.replace(/\.[^.]+$/, '') : ''));
  document.getElementById('ip-ed-inst').value = profile ? (profile.institution || '') : (opts.institution || '');
  document.getElementById('ip-ed-err').textContent = '';
  const notes = [...((IPE.draft && IPE.draft.notes) || [])];
  if (IPE.diff) {
    const d = IPE.diff, parts = [];
    if (d.removed && d.removed.length) parts.push('gone: ' + d.removed.join(', '));
    if (d.added && d.added.length) parts.push('new: ' + d.added.join(', '));
    if (d.moved && d.moved.length) parts.push('moved: ' + d.moved.map(m => m.header).join(', '));
    if (parts.length) notes.unshift('The file\'s columns changed (' + parts.join('; ') + '). New columns are highlighted.');
  }
  if (profile && profile.needs_preview) notes.unshift('This format was imported from a file and hasn\'t read a statement yet: check the preview.');
  document.getElementById('ip-ed-msg').innerHTML = notes.length ? `<ul class="ip-notes">${notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul>` : '';
  document.getElementById('ip-ed-kindwrap').innerHTML = profile ? `<span class="ip-chip">${escHtml(IP_KIND_LABELS[kind])}</span>`
    : `<label class="ip-inline">This is a <select id="ip-ed-kind" onchange="ipEditorKind(this.value)">${Object.entries(IP_KIND_LABELS).map(([k, v]) =>
        `<option value="${k}"${k === kind ? ' selected' : ''}>${v.toLowerCase().replace(/s$/, '')}</option>`).join('')}</select> format</label>`;
  document.getElementById('ip-editor').classList.remove('hidden');
  await _ipRenderAccounts();
  if (IPE.file) {
    if (IPE.draft && IPE.draft.grid) { IPE.grid = IPE.draft.grid; await _ipFetchGrid(true); }
    else await _ipFetchGrid();
    _ipDefaultHeader();
  } else if (IPE.spec.header_cells && IPE.spec.header_cells.length) {
    IPE.grid = [IPE.spec.header_cells];
    if (IPE.spec.has_header) IPE.spec.header_row = 0;
  }
  _ipRenderAll();
}

function ipBlankSpec(kind) {
  return kind === 'balance'
    ? {kind, has_header: true, header_row: 0, header_cells: [], columns: {}, sign: null, aggregate: 'per_row'}
    : {kind, has_header: true, header_row: 0, header_cells: [], columns: {}, amount_mode: 'single',
       sign: 'negative_is_out', date_format: 'MDY'};
}

// Hand the sample file to the Assistant, which proposes a format from its column shapes.
async function ipAskAssistant() {
  const file = IPE && IPE.file;
  if (!file) return;
  ipCloseEditor();
  askOpen();
  await askAttach(file);
}

function ipCloseEditor() {
  document.getElementById('ip-editor').classList.add('hidden');
  IPE = null;
}

async function ipEditorFile(file) {
  if (!file || !IPE) return;
  IPE.file = file; IPE.answered = !!IPE.profile; IPE.datesConfirmed = !!IPE.profile;
  if (!IPE.profile && !Object.keys(IPE.spec.columns || {}).length) {
    try {
      const fd = new FormData(); fd.append('file', file); fd.append('kind', IPE.kind);
      IPE.draft = await ipJson('/api/import-profiles/draft', {method: 'POST', body: fd});
      IPE.spec = IPE.draft.spec; IPE.grid = IPE.draft.grid;
      if (!document.getElementById('ip-ed-name').value) document.getElementById('ip-ed-name').value = file.name.replace(/\.[^.]+$/, '');
    } catch (e) { /* no draft: map the columns by hand */ }
  }
  await _ipFetchGrid();
  _ipDefaultHeader();
  _ipRenderAll();
}

// A format with nothing mapped yet takes the file's first row as its header.
function _ipDefaultHeader() {
  const s = IPE.spec;
  if (s.has_header && !(s.header_cells || []).length && !Object.keys(s.columns || {}).length && IPE.grid.length) {
    const row = IPE.grid[0];
    s.header_row = 0;
    s.header_cells = row.slice(0, row.reduce((n, v, k) => (v ? k + 1 : n), 0));
  }
}

async function ipEditorKind(kind) {
  if (!IPE || IPE.profile) return;
  IPE.kind = kind; IPE.links = new Set(); IPE.originalLinks = new Set();
  if (IPE.account && IPE.account.kind === kind) IPE.links.add(Number(IPE.account.id));
  if (IPE.file) {
    try {
      const fd = new FormData(); fd.append('file', IPE.file); fd.append('kind', kind);
      IPE.draft = await ipJson('/api/import-profiles/draft', {method: 'POST', body: fd});
      IPE.spec = IPE.draft.spec;
    } catch (e) { IPE.spec = ipBlankSpec(kind); }
  } else IPE.spec = ipBlankSpec(kind);
  _ipDefaultHeader();
  await _ipRenderAccounts();
  _ipRenderAll();
}

async function _ipFetchGrid(sheetsOnly) {
  if (!IPE.file) return;
  const s = IPE.spec;
  const fd = new FormData();
  fd.append('file', IPE.file);
  fd.append('delimiter', s.delimiter || 'auto'); fd.append('encoding', s.encoding || 'auto');
  fd.append('quote_char', s.quote_char == null ? 'auto' : s.quote_char);
  if (typeof s.sheet === 'string') fd.append('sheet', s.sheet);
  try {
    const g = await ipJson('/api/import-profiles/grid', {method: 'POST', body: fd});
    IPE.sheets = g.sheets; IPE.fileType = g.file_type;
    if (!sheetsOnly || !IPE.grid.length) IPE.grid = g.grid;
    if (g.file_type !== s.file_type) s.file_type = g.file_type;
  } catch (e) { IPE.grid = []; document.getElementById('ip-ed-err').textContent = e.message; }
}

// Where the header is: the saved hint, else the first row holding every mapped header.
function _ipHeaderIndex() {
  const s = IPE.spec;
  if (!s.has_header) return -1;
  const need = Object.values(s.columns || {}).flat().filter(r => r.header != null && !r.optional).map(r => ipNorm(r.header));
  if (need.length) {
    for (let i = 0; i < Math.min(IPE.grid.length, 40); i++) {
      const cells = new Set(IPE.grid[i].map(ipNorm));
      if (need.every(h => cells.has(h))) return i;
    }
  }
  return s.header_row != null && s.header_row < IPE.grid.length ? s.header_row : 0;
}

function _ipColumnOf(ref, header) {
  if (ref.header != null && header) {
    const i = header.findIndex(c => ipNorm(c) === ipNorm(ref.header));
    if (i >= 0) return i;
  }
  return ref.index != null ? ref.index : -1;
}

// column index -> [roles]
function _ipRolesByColumn() {
  const hi = _ipHeaderIndex();
  const header = hi >= 0 ? IPE.grid[hi] : null;
  const out = {};
  for (const [role, refs] of Object.entries(IPE.spec.columns || {}))
    for (const r of refs) {
      const i = _ipColumnOf(r, header);
      if (i >= 0) (out[i] = out[i] || []).push(role);
    }
  return out;
}

function _ipRenderAll() {
  if (!IPE) return;
  document.getElementById('ip-ed-filename').textContent = IPE.file ? `Sample file: ${IPE.file.name}` : 'No sample file yet: choose one to see how this format reads it.';
  const aiOn = typeof ASK !== 'undefined' && ASK.status && ASK.status.enabled && !ASK.status.paused;
  document.getElementById('ip-ed-ask').classList.toggle('hidden', !(IPE.file && aiOn));
  _ipRenderGrid();
  _ipRenderSettings();
  ipSchedulePreview();
}

function _ipRenderGrid() {
  const box = document.getElementById('ip-ed-grid');
  if (!IPE.grid.length) { box.innerHTML = '<div class="ip-note">No rows to show.</div>'; return; }
  const s = IPE.spec, hi = _ipHeaderIndex(), byCol = _ipRolesByColumn();
  const width = Math.min(Math.max(...IPE.grid.map(r => r.length)), 60);
  const added = new Set(((IPE.diff && IPE.diff.added) || []).map(ipNorm));
  const roles = IP_ROLES[IPE.kind];
  const footStart = IPE.grid.length - (s.skip_footer_rows || 0);
  let html = '<table class="ip-grid"><thead><tr><th></th>';
  for (let c = 0; c < width; c++) {
    const mine = byCol[c] || [];
    html += `<th class="${mine.length ? 'mapped' : ''}"><select aria-label="Column ${c + 1}" onchange="ipSetColumnRole(${c}, this.value)">
      <option value="">—</option>${roles.map(r => `<option value="${r}"${mine.includes(r) ? ' selected' : ''}>${escHtml(IP_ROLE_LABELS[r])}</option>`).join('')}
      </select>${mine.length > 1 ? `<div class="ip-note">+ ${mine.slice(1).map(r => escHtml(IP_ROLE_LABELS[r])).join(', ')}</div>` : ''}</th>`;
  }
  html += '</tr></thead><tbody>';
  IPE.grid.forEach((row, i) => {
    const cls = i === hi ? 'is-header' : (hi >= 0 && i < hi) ? 'is-pre' : i >= footStart ? 'is-foot' : '';
    html += `<tr class="${cls}"><th><button class="ip-rowbtn" title="Use row ${i + 1} as the header row" onclick="ipSetHeaderRow(${i})">${i + 1}</button></th>`;
    for (let c = 0; c < width; c++) {
      const v = row[c] || '';
      const isNew = i === hi && added.has(ipNorm(v));
      html += `<td class="${byCol[c] ? 'mapped' : ''}${isNew ? ' is-new' : ''}" title="${escHtml(v)}">${escHtml(v.length > 40 ? v.slice(0, 39) + '…' : v)}</td>`;
    }
    html += '</tr>';
  });
  box.innerHTML = html + '</tbody></table>';
}

function _ipRef(c) {
  const hi = _ipHeaderIndex();
  const ref = {index: c};
  if (IPE.spec.has_header && hi >= 0) ref.header = (IPE.grid[hi] || [])[c] || '';
  return ref;
}

function ipSetColumnRole(c, role) {
  const s = IPE.spec, hi = _ipHeaderIndex(), header = hi >= 0 ? IPE.grid[hi] : null;
  s.columns = s.columns || {};
  for (const [r, refs] of Object.entries(s.columns)) {           // this column leaves whatever it was
    const kept = refs.filter(ref => _ipColumnOf(ref, header) !== c);
    if (kept.length) s.columns[r] = kept; else delete s.columns[r];
  }
  if (role) {
    if (s.has_header && hi >= 0 && !((IPE.grid[hi] || [])[c] || '').trim()) {
      document.getElementById('ip-ed-err').textContent = `Column ${c + 1} has no header text: pick a header row with a name for it, or read the file by position.`;
      _ipRenderGrid(); return;
    }
    document.getElementById('ip-ed-err').textContent = '';
    if (role === 'description') s.columns.description = [...(s.columns.description || []), _ipRef(c)];
    else s.columns[role] = [_ipRef(c)];
    if (role === 'debit' || role === 'credit') { s.amount_mode = 'debit_credit'; delete s.columns.amount; }
    if (role === 'direction') s.amount_mode = 'amount_plus_direction';
    if (role === 'amount' && s.amount_mode === 'debit_credit') { s.amount_mode = 'single'; delete s.columns.debit; delete s.columns.credit; }
    if (role === 'direction' && !(s.direction_out_values || []).length) _ipGuessDirection(c);
  }
  if (s.amount_mode === 'single' && !s.sign) s.sign = 'negative_is_out';
  _ipRenderAll();
}

function _ipGuessDirection(c) {
  const hi = _ipHeaderIndex();
  const vals = [...new Set(IPE.grid.slice(hi + 1).map(r => ipNorm(r[c])).filter(Boolean))];
  const OUT = ['debit', 'dr', 'withdrawal', 'withdrawals', 'payment', 'purchase', 'sale', 'out', 'd'];
  const IN = ['credit', 'cr', 'deposit', 'deposits', 'refund', 'in', 'c', 'return'];
  IPE.spec.direction_out_values = vals.filter(v => OUT.includes(v));
  IPE.spec.direction_in_values = vals.filter(v => IN.includes(v));
}

function ipSetHeaderRow(i) {
  const s = IPE.spec, oldHi = _ipHeaderIndex(), oldHeader = oldHi >= 0 ? IPE.grid[oldHi] : null;
  const header = IPE.grid[i] || [];
  s.has_header = true; s.header_row = i;
  s.header_cells = header.slice(0, header.reduce((n, v, k) => (v ? k + 1 : n), 0));
  for (const [role, refs] of Object.entries(s.columns || {})) {
    s.columns[role] = refs.map(r => {
      const c = _ipColumnOf(r, oldHeader);
      return c >= 0 && header[c] ? {...r, index: c, header: header[c]} : null;
    }).filter(Boolean);
    if (!s.columns[role].length) delete s.columns[role];
  }
  _ipRenderAll();
}

function ipSetHeaderless(on) {
  const s = IPE.spec, hi = _ipHeaderIndex(), header = hi >= 0 ? IPE.grid[hi] : null;
  if (on) {
    for (const [role, refs] of Object.entries(s.columns || {}))
      s.columns[role] = refs.map(r => ({index: _ipColumnOf(r, header), ...(r.optional ? {optional: true} : {})})).filter(r => r.index >= 0);
    s.has_header = false; s.header_row = null; s.header_cells = [];
  } else ipSetHeaderRow(0);
  _ipRenderAll();
}

function ipSet(key, value) {
  if (!IPE) return;
  IPE.spec[key] = value;
  if (['delimiter', 'encoding', 'sheet'].includes(key)) { _ipFetchGrid().then(_ipRenderAll); return; }
  if (key === 'amount_mode') {
    const c = IPE.spec.columns;
    if (value === 'single') { IPE.spec.sign = IPE.spec.sign || 'negative_is_out'; delete c.debit; delete c.credit; delete c.direction; }
    else IPE.spec.sign = null;
    if (value === 'debit_credit') { delete c.amount; delete c.direction; }
    if (value === 'amount_plus_direction') { delete c.debit; delete c.credit; }
  }
  if (key === 'sign') IPE.answered = true;
  if (key === 'date_format') IPE.datesConfirmed = true;
  _ipRenderAll();
}

function ipSetPreamble(key, value) {
  IPE.spec.preamble = {...(IPE.spec.preamble || {})};
  if (value === '' || value === false) delete IPE.spec.preamble[key]; else IPE.spec.preamble[key] = value;
  ipSchedulePreview();
}

function ipAddSkipRule() {
  IPE.spec.skip_rows_containing = [...(IPE.spec.skip_rows_containing || []), {text: '', column: null}];
  _ipRenderSettings();
}
function ipSetSkipRule(i, key, value) {
  IPE.spec.skip_rows_containing[i][key] = value || (key === 'column' ? null : '');
  ipSchedulePreview();
}
function ipRemoveSkipRule(i) { IPE.spec.skip_rows_containing.splice(i, 1); _ipRenderSettings(); ipSchedulePreview(); }

function _ipDateHint() {
  const s = IPE.spec, hi = _ipHeaderIndex(), byCol = _ipRolesByColumn();
  const c = Object.keys(byCol).find(k => byCol[k].includes('date'));
  if (c == null) return '';
  let first = 0, second = 0, iso = 0;
  for (const r of IPE.grid.slice(hi + 1)) {
    const m = String(r[c] || '').match(/^\s*(\d{1,4})[/.-](\d{1,2})[/.-](\d{1,4})/);
    if (!m) continue;
    if (m[1].length === 4) iso++; else { if (+m[1] > 12) first++; if (+m[2] > 12) second++; }
  }
  if (iso) return 'Dates start with the year.';
  if (first && !second) return 'Some dates have a first part above 12: they are day first.';
  if (second && !first) return 'Some dates have a second part above 12: they are month first.';
  return 'Every date sample has both parts 12 or under: check one you know.';
}

function _ipSel(onchange, options, value) {
  return `<select onchange="${onchange}">${options.map(([v, l]) => `<option value="${escHtml(String(v))}"${String(v) === String(value) ? ' selected' : ''}>${escHtml(l)}</option>`).join('')}</select>`;
}

// Settings sections keep the open/closed state the user gave them across re-renders.
const IP_SECTION_DEFAULTS = {file: false, rows: true, values: true, amounts: true, dates: true, text: false, rowsare: true, preamble: false};
function _ipOpen(key, force) {
  const st = (IPE.sectionState = IPE.sectionState || {});
  const open = key in st ? st[key] : (force || IP_SECTION_DEFAULTS[key]);
  return `data-k="${key}"${open ? ' open' : ''} ontoggle="IPE && ((IPE.sectionState = IPE.sectionState || {})['${key}'] = this.open)"`;
}

function _ipRenderSettings() {
  const s = IPE.spec, k = IPE.kind, x = IPE.fileType || s.file_type;
  const roles = IP_ROLES[k].filter(r => (s.columns || {})[r]);
  const rules = (s.skip_rows_containing || []).map((r, i) => `<div class="ip-rule">
      <input type="text" value="${escHtml(r.text)}" placeholder="text, e.g. Beginning balance" oninput="ipSetSkipRule(${i}, 'text', this.value)">
      ${_ipSel(`ipSetSkipRule(${i}, 'column', this.value)`, [['', 'in any column'], ...roles.map(r2 => [r2, 'in ' + IP_ROLE_LABELS[r2]])], r.column || '')}
      <button class="btn-sm" onclick="ipRemoveSkipRule(${i})" aria-label="Remove">✕</button></div>`).join('');
  const hi = _ipHeaderIndex();
  let html = `
    <details ${_ipOpen('file')}><summary>File</summary>
      ${x === 'xlsx' ? `<label>Sheet ${_ipSel("ipSet('sheet', this.value || null)", [['', 'whichever sheet has the header'], ...(IPE.sheets || []).map(n => [n, n])], s.sheet || '')}</label>`
        : `<label>Columns separated by ${_ipSel("ipSet('delimiter', this.value)", [['auto', 'detect'], [',', 'commas'], ['\t', 'tabs'], [';', 'semicolons'], ['|', 'pipes']], s.delimiter || 'auto')}</label>
           <label>Text encoding ${_ipSel("ipSet('encoding', this.value)", [['auto', 'detect'], ['utf-8', 'UTF-8'], ['utf-8-sig', 'UTF-8 with BOM'], ['latin-1', 'Latin-1']], s.encoding || 'auto')}</label>`}
    </details>
    <details ${_ipOpen('rows')}><summary>Rows</summary>
      <div class="ip-note">${s.has_header ? `Header: row ${hi + 1} (click a row number to change it). Rows above it are skipped.` : 'Read by position: rows before the first date are skipped.'}</div>
      <label class="ip-check"><input type="checkbox" ${s.has_header ? '' : 'checked'} onchange="ipSetHeaderless(this.checked)"> The file has no header row</label>
      <label>Rows to skip at the end <input type="number" min="0" max="999" value="${s.skip_footer_rows || 0}" onchange="ipSet('skip_footer_rows', Math.max(0, parseInt(this.value) || 0))"></label>
      ${k !== 'balance' ? `<label>A row whose date can't be read ${_ipSel("ipSet('on_bad_date', this.value)", [['error', 'is listed as an error'], ['skip', 'is skipped quietly']], s.on_bad_date || 'error')}</label>` : ''}
      <div class="ip-note">Skip rows containing</div>${rules}<button class="btn-sm" onclick="ipAddSkipRule()">+ Add a rule</button>
    </details>`;
  if (k === 'balance') {
    html += `<details ${_ipOpen('values')}><summary>Values</summary>
      <label>Several rows for one account ${_ipSel("ipSet('aggregate', this.value)", [['per_row', 'are separate accounts'], ['sum_by_account', 'are added up']], s.aggregate || 'per_row')}</label>
      ${_ipNumberControls(s)}</details>`;
  } else {
    const dir = s.amount_mode === 'amount_plus_direction';
    html += `<details ${_ipOpen('amounts')}><summary>Amounts</summary>
      <label>Amounts are in ${_ipSel("ipSet('amount_mode', this.value)", [['single', 'one column, signed'], ['debit_credit', 'separate money-out and money-in columns'], ['amount_plus_direction', 'one column, with a column saying in or out']], s.amount_mode || 'single')}</label>
      ${s.amount_mode === 'single' ? `<label>Money out is written as ${_ipSel("ipSet('sign', this.value)", [['negative_is_out', 'a negative number (−12.00)'], ['positive_is_out', 'a positive number (12.00)']], s.sign || 'negative_is_out')}</label>` : ''}
      ${dir ? `<label>Words meaning money out <input type="text" value="${escHtml((s.direction_out_values || []).join(', '))}" onchange="ipSet('direction_out_values', ipList(this.value))"></label>
               <label>Words meaning money in <input type="text" value="${escHtml((s.direction_in_values || []).join(', '))}" onchange="ipSet('direction_in_values', ipList(this.value))"></label>` : ''}
      ${_ipNumberControls(s)}
    </details>
    <details ${_ipOpen('dates')}><summary>Dates</summary>
      <label>Dates are ${_ipSel("ipSet('date_format', this.value)", Object.entries(IP_DATE_LABELS), s.date_format || 'MDY')}</label>
      <div class="ip-note">${escHtml(_ipDateHint())}</div>
    </details>
    <details ${_ipOpen('text')}><summary>Statuses and text</summary>
      <label>Skip rows whose status is <input type="text" value="${escHtml((s.skip_status_values || []).join(', '))}" placeholder="e.g. pending" onchange="ipSet('skip_status_values', ipList(this.value))"></label>
      <label class="ip-check"><input type="checkbox" ${s.collapse_whitespace !== false ? 'checked' : ''} onchange="ipSet('collapse_whitespace', this.checked)"> Collapse runs of spaces in descriptions</label>
    </details>`;
    if (k === 'cash') html += `<details ${_ipOpen('rowsare')}><summary>What the rows are</summary>
      <label>Import rows as ${_ipSel("ipSet('row_source', this.value)", [['bank', 'bank statement lines (matched against the ledger)'], ['manual', 'manual ledger entries (a tracking spreadsheet)']], s.row_source || 'bank')}</label>
      ${s.row_source === 'manual' || (s.columns || {}).reconciled ? `<label>A row is reconciled when that column says <input type="text" value="${escHtml((s.reconciled_values || ['x']).join(', '))}" onchange="ipSet('reconciled_values', ipList(this.value))"></label>` : ''}
    </details>`;
    if (k === 'card') {
      const pre = s.preamble || {};
      html += `<details ${_ipOpen('preamble', !!(pre.period || pre.cardholder_label || pre.account_label))}><summary>Rows above the header</summary>
        <label class="ip-check"><input type="checkbox" ${pre.period ? 'checked' : ''} onchange="ipSetPreamble('period', this.checked)"> Read the statement period ("Sep 22, 2026 to Oct 22, 2026")</label>
        <label>Cardholder name follows the label <input type="text" value="${escHtml(pre.cardholder_label || '')}" placeholder="e.g. Prepared for" onchange="ipSetPreamble('cardholder_label', this.value.trim())"></label>
        <label>Account number follows the label <input type="text" value="${escHtml(pre.account_label || '')}" placeholder="e.g. Account Number" onchange="ipSetPreamble('account_label', this.value.trim())"></label>
      </details>`;
    }
  }
  document.getElementById('ip-ed-settings').innerHTML = html;
}

function _ipNumberControls(s) {
  return `<label>Decimal mark ${_ipSel("ipSet('decimal', this.value)", [['.', 'point (1,234.56)'], [',', 'comma (1.234,56)']], s.decimal || '.')}</label>
    <label>Thousands separator ${_ipSel("ipSet('thousands', this.value)", [[',', 'comma'], ['.', 'point'], [' ', 'space'], ["'", 'apostrophe'], ['', 'none']], s.thousands == null ? ',' : s.thousands)}</label>
    <label class="ip-check"><input type="checkbox" ${s.parens_negative !== false ? 'checked' : ''} onchange="ipSet('parens_negative', this.checked)"> (12.00) means negative</label>`;
}

let _ipPreviewTimer = null;
function ipSchedulePreview() {
  clearTimeout(_ipPreviewTimer);
  _ipPreviewTimer = setTimeout(_ipPreview, 350);
}

async function _ipPreview() {
  if (!IPE) return;
  const box = document.getElementById('ip-ed-preview');
  if (!IPE.file) { box.innerHTML = '<div class="ip-note">Choose a sample file to preview what this format reads.</div>'; return; }
  const fd = new FormData();
  fd.append('file', IPE.file); fd.append('spec', JSON.stringify(IPE.spec));
  if (IPE.profile) fd.append('profile_id', IPE.profile.id);
  if (IPE.account && IPE.account.kind === IPE.kind) { fd.append('account_kind', IPE.account.kind); fd.append('account_id', IPE.account.id); }
  let d;
  try { d = await ipJson('/api/import-profiles/preview', {method: 'POST', body: fd}); }
  catch (e) {
    IPE.lastPreview = null;
    box.innerHTML = `<div class="ip-bad">✗ ${escHtml(e.message)}</div>`;
    return;
  }
  IPE.lastPreview = d;
  const sc = d.sign_check, bal = IPE.kind === 'balance';
  const badge = bal ? '' : `<span class="ip-badge ip-${sc.status}">sign ${escHtml(sc.status)}</span> <span class="ip-note">${escHtml(sc.reason)}</span>`;
  const skipped = Object.entries(d.skipped || {}).map(([why, n]) => `${n} ${({status: 'by status', matched_text: 'by a skip rule', no_date: 'without a date', no_amount: 'without an amount', bad_date: 'with an unreadable date', blank: 'blank'})[why] || why}`).join(', ');
  let question = '';
  if (_ipNeedsSignAnswer(d)) {
    const r = d.rows.find(x => x.amount && x.description);
    if (r) question = `<div class="ip-question">Nothing in the file shows which way money moved. Did money <b>leave</b> the account for “${escHtml(r.description)}” on ${escHtml(cfFmtDate(r.date))}?
      <button class="btn-sm" onclick="ipAnswerSign(${r.amount < 0}, true)">Yes, money out</button>
      <button class="btn-sm" onclick="ipAnswerSign(${r.amount < 0}, false)">No, money in</button></div>`;
  }
  if (_ipNeedsDateAnswer(d)) {
    const r = d.rows.find(x => x.date);
    const read = r ? new Date(r.date + 'T00:00:00').toLocaleDateString('en-US', {day: 'numeric', month: 'long', year: 'numeric'}) : '';
    question += `<div class="ip-question">Every date in this file could be month first or day first.${r ? ` Flow reads the first one${r.description ? ` (“${escHtml(r.description)}”)` : ''} as <b>${escHtml(read)}</b>.` : ''} Which order does the file use?
      <button class="btn-sm" onclick="ipAnswerDates('MDY')">Month first (03/04 = Mar 4)</button>
      <button class="btn-sm" onclick="ipAnswerDates('DMY')">Day first (03/04 = 3 Apr)</button></div>`;
  }
  const head = bal ? '<th>Account</th><th style="text-align:right">Value</th>' : '<th>Date</th><th>Description</th><th style="text-align:right">Amount</th>';
  const rows = d.rows.slice(0, 15).map(r => bal
    ? `<tr><td>${escHtml(r.account_ref)}</td><td class="num">${fmtFull(r.value)}</td></tr>`
    : `<tr><td>${r.date ? escHtml(cfFmtDate(r.date)) : ''}</td><td>${escHtml(r.description)}</td>
       <td class="num"><span class="${r.amount < 0 ? 'amount-debit' : 'amount-credit'}">${fmtFull(r.amount)}</span></td></tr>`).join('');
  box.innerHTML = `<div>${d.row_count} ${bal ? 'value' : 'transaction'}${d.row_count === 1 ? '' : 's'}${bal ? '' : ` · money in ${fmtFull(d.money_in)} · money out ${fmtFull(d.money_out)}`}${skipped ? ` · skipped: ${escHtml(skipped)}` : ''} ${badge}</div>
    ${d.header_changed ? '<div class="ip-warn">The file\'s header differs from the one saved with this format: saving updates it.</div>' : ''}
    ${question}
    ${d.error_count ? `<div class="ip-bad">${d.error_count} row${d.error_count === 1 ? '' : 's'} can't be read:</div><ul class="ip-errors">${d.errors.slice(0, 20).map(e => `<li>Row ${e.line}: ${escHtml(e.message)}</li>`).join('')}</ul>` : ''}
    ${d.rows.length ? `<table class="uimp-preview"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>` : ''}
    ${bal ? '' : '<div class="ip-note">Money you spent should show as negative (red), money you received as positive.</div>'}`;
  if (d.header_changed && IPE.spec.has_header) {         // saving keeps the header this file has
    const hi = _ipHeaderIndex();
    if (hi >= 0) IPE.spec.header_cells = (IPE.grid[hi] || []).slice(0, (IPE.grid[hi] || []).reduce((n, v, k) => (v ? k + 1 : n), 0));
  }
}

// Whether the sign still needs the user's answer: nothing in the file confirmed it, for any amount
// layout (one amount column, separate money-out / money-in columns, or an amount plus a direction).
function _ipNeedsSignAnswer(d) {
  return IPE.kind !== 'balance' && !IPE.answered && d && d.sign_check && d.sign_check.status !== 'confirmed'
    && d.rows.some(x => x.amount && x.description);
}
function _ipNeedsDateAnswer(d) { return !IPE.datesConfirmed && !!(d && d.date_order_ambiguous); }

function ipAnswerSign(shownNegative, isOut) {
  // The row is shown negative or positive as the format reads it now; reverse the format if that
  // contradicts the answer: the sign for one amount column, the two columns' roles for money out /
  // money in, the direction words for an amount plus a direction column.
  if (shownNegative !== isOut) {
    const s = IPE.spec;
    if (s.amount_mode === 'debit_credit') [s.columns.debit, s.columns.credit] = [s.columns.credit, s.columns.debit];
    else if (s.amount_mode === 'amount_plus_direction')
      [s.direction_out_values, s.direction_in_values] = [s.direction_in_values || [], s.direction_out_values || []];
    else s.sign = s.sign === 'negative_is_out' ? 'positive_is_out' : 'negative_is_out';
  }
  IPE.answered = true;
  _ipRenderAll();
}

function ipAnswerDates(order) {
  IPE.spec.date_format = order;
  IPE.datesConfirmed = true;
  _ipRenderAll();
}

async function _ipRenderAccounts() {
  const box = document.getElementById('ip-ed-accounts');
  const accts = await ipAccountsOf(IPE.kind);
  IPE.accounts = accts;
  box.innerHTML = accts.length ? accts.map(a => `<label class="ip-check"><input type="checkbox" value="${a.id}" ${IPE.links.has(a.id) ? 'checked' : ''}
      onchange="ipToggleLink(${a.id}, this.checked)"> ${escHtml(a.name)}</label>`).join('')
    : '<span class="ip-note">No accounts of this kind yet.</span>';
}
function ipToggleLink(id, on) { if (on) IPE.links.add(id); else IPE.links.delete(id); }

async function ipSaveEditor() {
  const err = document.getElementById('ip-ed-err');
  const name = document.getElementById('ip-ed-name').value.trim();
  if (!name) { err.textContent = 'Give the format a name.'; return; }
  if (IPE.lastPreview && IPE.lastPreview.sign_check.status === 'reversed') {
    err.textContent = 'The preview says money in and out are reversed: fix the sign first.'; return;
  }
  if (_ipNeedsSignAnswer(IPE.lastPreview)) {
    err.textContent = 'Answer the question in the preview first: Flow can\'t tell from this file whether money left the account.'; return;
  }
  if (_ipNeedsDateAnswer(IPE.lastPreview)) {
    err.textContent = 'Answer the question in the preview first: choose whether the dates are month first or day first.'; return;
  }
  const btn = document.getElementById('ip-ed-savebtn');
  btn.disabled = true; err.textContent = '';
  const body = {name, institution: document.getElementById('ip-ed-inst').value.trim(),
                notes: IPE.profile ? (IPE.profile.notes || '') : '', spec: IPE.spec,
                link: [...IPE.links].map(id => ({kind: IPE.kind, account_id: id}))};
  try {
    let saved = IPE.profile ? await ipPut(`/api/import-profiles/${IPE.profile.id}`, body)
                            : await ipPost('/api/import-profiles', body);
    for (const id of IPE.originalLinks)
      if (IPE.profile && !IPE.links.has(id))
        saved = await ipJson(`/api/import-profiles/${saved.id}/links?kind=${IPE.kind}&account_id=${id}`, {method: 'DELETE'});
    const done = IPE.onSaved;
    ipCloseEditor();
    if (done) await done(saved);
  } catch (e) { err.textContent = e.message; }
  finally { btn.disabled = false; }
}

// ── The formats list ─────────────────────────────────────────────────────────

let _ipListOpts = null;
async function ipOpenList(opts = {}) {
  _ipListOpts = opts;
  if (!document.getElementById('ip-list')) {
    document.body.insertAdjacentHTML('beforeend', `
<div id="ip-list" class="ip-overlay hidden"><div class="modal-box ip-listbox" role="dialog" aria-labelledby="ip-list-title">
  <div class="ip-ed-head"><h2 id="ip-list-title">Import formats</h2><button class="btn-sm" onclick="ipCloseList()" aria-label="Close">✕</button></div>
  <p>How each account's statement files are read. A format holds column names and settings only, never your data, so an exported one is safe to share.</p>
  <div id="ip-list-body"></div>
  <div class="modal-footer"><label class="btn-sm ip-filebtn">Import a format file…<input type="file" accept=".json" onchange="ipImportFormat(this.files[0], this)"></label>
    <button onclick="ipCloseList()">Close</button></div>
</div></div>`);
  }
  document.getElementById('ip-list').classList.remove('hidden');
  await _ipRenderList();
}
function ipCloseList() {
  document.getElementById('ip-list').classList.add('hidden');
  if (_ipListOpts && _ipListOpts.onClose) _ipListOpts.onClose();
}
async function _ipRenderList() {
  const box = document.getElementById('ip-list-body');
  const list = await ipJson('/api/import-profiles').catch(e => { box.textContent = e.message; return null; });
  if (!list) return;
  IPE_LIST = list;
  box.innerHTML = list.length ? `<table class="ip-table"><thead><tr><th>Format</th><th>Reads</th><th>Used by</th><th>Last used</th><th></th></tr></thead><tbody>
    ${list.map(p => `<tr><td><b>${escHtml(p.name)}</b>${p.institution ? `<div class="ip-note">${escHtml(p.institution)}</div>` : ''}${p.needs_preview ? '<div class="ip-warn">Not tried on a file yet</div>' : ''}</td>
      <td>${escHtml(ipWhat(p))}</td><td>${p.links.length ? p.links.map(l => escHtml(l.account)).join(', ') : '<span class="ip-note">no account</span>'}</td>
      <td>${escHtml(ipWhen(p.last_used_at))}</td>
      <td class="ip-actions-cell"><button class="btn-sm" onclick="ipEditSaved(${p.id})">Edit</button>
        <button class="btn-sm" onclick="ipDuplicate(${p.id})">Duplicate</button>
        <a class="btn-sm" href="/api/import-profiles/${p.id}/export">Export</a>
        <button class="btn-sm ip-danger" onclick="ipDeleteFormat(${p.id})">Delete</button></td></tr>`).join('')}</tbody></table>`
    : '<div class="ip-note">No formats yet. One is set up the first time you import a file into an account.</div>';
}
let IPE_LIST = [];

async function ipEditSaved(id, extra = {}) {
  const p = await ipJson(`/api/import-profiles/${id}`);
  await ipOpenEditor({profile: p, ...extra, onSaved: extra.onSaved || (async () => {
    if (!document.getElementById('ip-list').classList.contains('hidden')) await _ipRenderList();
  })});
}
async function ipDuplicate(id) {
  const p = await ipJson(`/api/import-profiles/${id}`);
  try { await ipPost('/api/import-profiles', {name: 'Copy of ' + p.name, institution: p.institution, notes: p.notes, spec: p.spec}); }
  catch (e) { alert(e.message); }
  await _ipRenderList();
}
async function ipDeleteFormat(id) {
  const p = IPE_LIST.find(x => x.id === id);
  const users = p && p.links.length ? ` ${p.links.map(l => l.account).join(', ')} will ask for a format at their next import.` : '';
  if (!confirm(`Delete the format "${p ? p.name : ''}"?${users}`)) return;
  await fetch(`/api/import-profiles/${id}`, {method: 'DELETE'});
  await _ipRenderList();
}
async function ipImportFormat(file, input, account) {
  if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const p = await ipJson('/api/import-profiles/import', {method: 'POST', body: fd});
    if (account) await ipPost(`/api/import-profiles/${p.id}/links`, {kind: account.kind, account_id: Number(account.id)});
  } catch (e) { alert(e.message); }
  if (input) input.value = '';
  if (document.getElementById('ip-list') && !document.getElementById('ip-list').classList.contains('hidden')) await _ipRenderList();
}

// ── "Import files" section of the account dialogs ────────────────────────────

// Renders into `box` (a .acct-dlg-section) for account `id` of `kind` (cash | card | balance);
// id null means the account isn't saved yet.
async function ipAccountSection(box, kind, id) {
  box.dataset.kind = kind; box.dataset.id = id || '';
  const head = `<div class="acct-dlg-h">Import files</div>
    <div class="acct-dlg-help">${kind === 'balance'
      ? 'Formats for balance files (holdings or accounts exports) that list this account. Update balances → File reads a file with any saved balance format and matches this account by its last 4 digits, a paste marker or its name.'
      : 'How this account\'s statement files are read. A file whose layout matches none of these asks you to set up a format when you import it.'}</div>`;
  if (!id) { box.innerHTML = head + '<div class="acct-dlg-help">Save the account first to link a format.</div>'; return; }
  const all = await ipJson(`/api/import-profiles?kind=${kind}`).catch(() => []);
  const mine = all.filter(p => p.links.some(l => l.kind === kind && l.account_id === Number(id)));
  const others = all.filter(p => !mine.includes(p));
  box.innerHTML = head + (mine.length ? `<div class="ip-acct-list">${mine.map(p => `<div class="ip-acct-row">
      <div><b>${escHtml(p.name)}</b> <span class="ip-note">${escHtml(ipWhat(p))} · last used ${escHtml(ipWhen(p.last_used_at))}${p.needs_preview ? ' · not tried on a file yet' : ''}</span></div>
      <div class="ip-actions"><button class="btn-sm" onclick="ipSectionEdit(this, ${p.id})">Edit</button>
        <button class="btn-sm" onclick="ipSectionUnlink(this, ${p.id})">Unlink</button>
        <a class="btn-sm" href="/api/import-profiles/${p.id}/export">Export</a></div></div>`).join('')}</div>`
      : '<div class="acct-dlg-help">No format linked yet.</div>')
    + `<div class="ip-actions">
      <label class="btn-sm ip-filebtn">Set up from a sample file…<input type="file" accept=".csv,.tsv,.txt,.xlsx,.xlsm" onchange="ipSectionSample(this)"></label>
      ${others.length ? `<select class="ip-linksel" aria-label="Link an existing format" onchange="ipSectionLink(this)">
        <option value="">Link an existing format…</option>${others.map(p => `<option value="${p.id}">${escHtml(p.name)}</option>`).join('')}</select>` : ''}
      <label class="btn-sm ip-filebtn">Import format file…<input type="file" accept=".json" onchange="ipSectionImport(this)"></label>
      <button class="btn-sm" onclick="ipOpenList({onClose: () => ipSectionRefresh(this)})">All formats…</button></div>`;
}
function _ipSectionOf(el) { return el.closest('[data-kind]'); }
function ipSectionRefresh(el) { const box = _ipSectionOf(el); if (box) ipAccountSection(box, box.dataset.kind, box.dataset.id); }
async function ipSectionEdit(el, pid) {
  const box = _ipSectionOf(el);
  await ipEditSaved(pid, {account: {kind: box.dataset.kind, id: box.dataset.id}, onSaved: () => ipAccountSection(box, box.dataset.kind, box.dataset.id)});
}
async function ipSectionUnlink(el, pid) {
  const box = _ipSectionOf(el);
  await fetch(`/api/import-profiles/${pid}/links?kind=${box.dataset.kind}&account_id=${box.dataset.id}`, {method: 'DELETE'});
  ipAccountSection(box, box.dataset.kind, box.dataset.id);
}
async function ipSectionLink(sel) {
  const box = _ipSectionOf(sel);
  if (!sel.value) return;
  try { await ipPost(`/api/import-profiles/${sel.value}/links`, {kind: box.dataset.kind, account_id: Number(box.dataset.id)}); }
  catch (e) { alert(e.message); }
  ipAccountSection(box, box.dataset.kind, box.dataset.id);
}
async function ipSectionImport(input) {
  const box = _ipSectionOf(input);
  await ipImportFormat(input.files[0], input, {kind: box.dataset.kind, id: box.dataset.id});
  ipAccountSection(box, box.dataset.kind, box.dataset.id);
}
async function ipSectionSample(input) {
  const box = _ipSectionOf(input), file = input.files[0];
  input.value = '';
  if (!file) return;
  let draft = null;
  try {
    const fd = new FormData(); fd.append('file', file); fd.append('kind', box.dataset.kind);
    draft = await ipJson('/api/import-profiles/draft', {method: 'POST', body: fd});
  } catch (e) { /* no draft: the editor starts blank */ }
  await ipOpenEditor({file, kind: box.dataset.kind, draft, account: {kind: box.dataset.kind, id: box.dataset.id},
                      onSaved: () => ipAccountSection(box, box.dataset.kind, box.dataset.id)});
}

// ── The import dialog's format problems ──────────────────────────────────────

function ipKind(target) { return target.kind === 'c' ? 'card' : 'cash'; }

function _ipErrors(d) {
  const list = (d.errors || []).slice(0, 20).map(e => `<li>Row ${e.line}: ${escHtml(e.message)}</li>`).join('');
  return list ? `<ul class="ip-errors">${list}</ul>${d.error_count > 20 ? `<div class="ip-note">…and ${d.error_count - 20} more.</div>` : ''}` : '';
}

let _ipCtx = null;
// A structured problem from an import endpoint, shown with its fix. ctx: {box, file, accountKind,
// accountId, retry(), allowErrors()}. Returns true when it handled the response.
async function ipProblem(detail, ctx) {
  if (!detail || typeof detail !== 'object' || !detail.code) return false;
  _ipCtx = ctx;
  const box = ctx.box;
  box.style.display = 'block'; box.style.color = 'var(--text)';
  const account = {kind: ctx.accountKind, id: ctx.accountId};
  _ipCtx.editorOpts = null;
  if (detail.code === 'row_errors') {
    box.innerHTML = `<div class="ip-box"><strong>⚠ ${escHtml(detail.message)}</strong>${_ipErrors(detail)}
      <div class="ip-actions"><button class="btn-sm" onclick="ipImportRest()">Import the rest</button></div></div>`;
    return true;
  }
  if (detail.code === 'sign_reversed') {
    const prof = await ipJson(`/api/import-profiles/${detail.profile.id}`);
    _ipCtx.editorOpts = {profile: prof, file: ctx.file, account};
    box.innerHTML = `<div class="ip-box"><strong>⚠ ${escHtml(detail.message)}</strong><div class="ip-actions">
      ${prof.spec.amount_mode === 'single' ? '<button class="btn-sm btn-primary" onclick="ipFlipSign()">Flip the sign and check again</button>' : ''}
      <button class="btn-sm" onclick="ipOpenFromProblem()">Open the format…</button></div></div>`;
    _ipCtx.profile = prof;
    return true;
  }
  if (detail.code === 'profile_drift') {
    const prof = await ipJson(`/api/import-profiles/${detail.profile.id}`);
    _ipCtx.editorOpts = {profile: prof, spec: detail.draft, diff: detail.diff, file: ctx.file, account};
    const d = detail.diff || {};
    const parts = [d.removed && d.removed.length ? 'gone: ' + d.removed.map(escHtml).join(', ') : '',
                   d.added && d.added.length ? 'new: ' + d.added.map(escHtml).join(', ') : ''].filter(Boolean).join(' · ');
    box.innerHTML = `<div class="ip-box"><strong>${escHtml(detail.message)}</strong>${parts ? `<div class="ip-note">Columns ${parts}</div>` : ''}
      <div class="ip-actions"><button class="btn-sm btn-primary" onclick="ipOpenFromProblem()">Update the format…</button></div></div>`;
    return true;
  }
  if (detail.code === 'no_profile') {
    _ipCtx.editorOpts = {file: ctx.file, kind: ctx.accountKind, draft: detail.draft, account};
    const matches = (detail.matches || []).map(m => `<button class="btn-sm" onclick="ipUseExisting(${m.id})">Use “${escHtml(m.name)}”</button>`).join(' ');
    box.innerHTML = `<div class="ip-box"><strong>${escHtml(detail.message)}</strong>
      ${detail.draft && detail.draft.notes && detail.draft.notes.length ? `<ul class="ip-notes">${detail.draft.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul>` : ''}
      <div class="ip-actions">${matches}<button class="btn-sm btn-primary" onclick="ipOpenFromProblem()">Set up the format…</button></div></div>`;
    return true;
  }
  return false;
}

async function ipOpenFromProblem() {
  const ctx = _ipCtx;
  await ipOpenEditor({...ctx.editorOpts, onSaved: () => ctx.retry()});
}
async function ipUseExisting(id) {
  try { await ipPost(`/api/import-profiles/${id}/links`, {kind: _ipCtx.accountKind, account_id: Number(_ipCtx.accountId)}); }
  catch (e) { alert(e.message); return; }
  _ipCtx.retry();
}
async function ipFlipSign() {
  const p = _ipCtx.profile;
  const spec = {...p.spec, sign: p.spec.sign === 'negative_is_out' ? 'positive_is_out' : 'negative_is_out'};
  try { await ipPut(`/api/import-profiles/${p.id}`, {name: p.name, institution: p.institution || '', notes: p.notes || '', spec}); }
  catch (e) { alert(e.message); return; }
  _ipCtx.retry();
}
function ipImportRest() { _ipCtx.allowErrors(); }
