// ═══════════════════════════════════════════════════════════════════════════════
// BALANCES: balance-sheet accounts typed or pasted from a bank's account summary.
// Paste → /api/balances/read (local reading, nothing sent) → /preview (checks) → the user
// ticks rows → /apply. The AI is used only when asked, on a scrubbed outline the dialog shows.
// Built with DOM nodes; pasted text never goes through innerHTML.
// ═══════════════════════════════════════════════════════════════════════════════

const BAL = {mode: 'paste', settings: null, items: null, outline: '', needsAi: false, plan: null,
             picks: {}, checked: {}, typed: {}, busy: false, lines: [], remember: {}, teach: null};

function balEl(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'checked' || k === 'disabled' || k === 'selected') el[k] = !!v;
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
}

// Today in the user's own time zone. The server runs on UTC, where the evening of Oct 31 in the
// US is already Nov 1; periods are worked out from this date instead (balance_sheet.client_today).
function balToday() {
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

async function balApi(url, body, method) {
  const today = {'X-Flow-Today': balToday()};
  const res = await fetch(url, body === undefined ? {method: method || 'GET', headers: today}
    : {method: method || 'POST', headers: {...today, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Request failed (' + res.status + ')');
  return d;
}

const balMoney = v => v === null || v === undefined ? '—' : fmtFull(v);

// Periods are the 1st of a month; the picker is a month input.
function balPeriod() {
  const m = document.getElementById('bal-period').value;
  return m ? m + '-01' : '';
}
function balMonthLabel(p) {
  const [y, m] = p.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString('en-US', {month: 'long', year: 'numeric'});
}

// ── Open / close ──────────────────────────────────────────────────────────────
async function balOpen(mode, text) {
  document.getElementById('bal-modal').classList.remove('hidden');
  BAL.settings = await balApi('/api/balances/settings').catch(() => null);
  if (BAL.settings) {
    document.getElementById('bal-period').value = BAL.settings.suggested_period.slice(0, 7);
    document.getElementById('bal-th-pct').value = BAL.settings.alert_pct;
    document.getElementById('bal-th-abs').value = BAL.settings.alert_abs;
  }
  balReset();
  if (text) document.getElementById('bal-text').value = text;
  balMode(mode || 'paste');
  if (text) balRead();
}
function balClose() {
  document.getElementById('bal-modal').classList.add('hidden');
}
function balReset() {
  Object.assign(BAL, {items: null, outline: '', needsAi: false, plan: null, picks: {}, checked: {}, typed: {},
                      lines: [], remember: {}, teach: null});
  document.getElementById('bal-preview').replaceChildren();
  document.getElementById('bal-msg').replaceChildren();
  document.getElementById('bal-apply').disabled = true;
}
function balMode(mode) {
  BAL.mode = mode;
  BAL.checked = {};
  document.querySelectorAll('#bal-modal .bal-tab').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  document.getElementById('bal-paste-pane').classList.toggle('hidden', mode !== 'paste');
  document.getElementById('bal-type-pane').classList.toggle('hidden', mode !== 'type');
  document.getElementById('bal-file-pane').classList.toggle('hidden', mode !== 'file');
  if (mode === 'file') { BAL.items = null; document.getElementById('bal-file').value = ''; }
  document.getElementById('bal-preview').replaceChildren();
  document.getElementById('bal-msg').replaceChildren();
  BAL.plan = null;
  document.getElementById('bal-apply').disabled = true;
  if (mode === 'type') balRenderTyping();
}
function balMsg(text, kind) {
  document.getElementById('bal-msg').replaceChildren(text ? balEl('div', {class: 'bal-note ' + (kind || '')}, text) : '');
}

// ── Paste ─────────────────────────────────────────────────────────────────────
async function balRead() {
  const text = document.getElementById('bal-text').value;
  if (!text.trim()) { balMsg('Paste the account summary page first.', 'error'); return; }
  BAL.picks = {}; BAL.checked = {}; BAL.remember = {}; BAL.teach = null;
  try {
    const d = await balApi('/api/balances/read', {text});
    BAL.items = d.items; BAL.outline = d.outline; BAL.needsAi = d.needs_ai; BAL.lines = d.lines || [];
  } catch (e) { balMsg(e.message, 'error'); return; }
  await balPreview();
}

async function balReadAi() {
  const text = document.getElementById('bal-text').value;
  const btn = document.getElementById('bal-ai-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Asking…'; }
  try {
    const d = await balApi('/api/balances/read-ai', {text});
    BAL.items = d.items; BAL.outline = d.outline; BAL.needsAi = false; BAL.lines = d.lines || BAL.lines;
  } catch (e) { balMsg('AI reading failed: ' + e.message, 'error'); if (btn) { btn.disabled = false; btn.textContent = 'Read with AI'; } return; }
  await balPreview();
}

function balEntries() {
  if (BAL.mode === 'type') {
    return Object.entries(BAL.typed).filter(([, v]) => v !== '' && v !== null).map(([id, v]) => {
      const a = accounts.find(x => x.id === +id);
      return {account_id: +id, amount: parseFloat(String(v).replace(/[$,\s]/g, '')),
              kind: a && a.account_type === 'liability' ? 'owed' : 'balance', source: 'manual'};
    }).filter(e => !isNaN(e.amount));
  }
  // A paste marker already knows the account; a pick in the preview overrides it.
  return (BAL.items || []).map((it, i) => ({...it, account_id: BAL.picks[i] || it.account_id || null}));
}

async function balPreview() {
  const period = balPeriod();
  if (!period) { balMsg('Pick the period these balances belong to.', 'error'); return; }
  const entries = balEntries();
  if (!entries.length) { balMsg({type: 'Type at least one balance.', file: 'No accounts found in that file.'}[BAL.mode] || 'No accounts found in that paste.', 'error'); return; }
  try {
    BAL.plan = await balApi('/api/balances/preview', {period, entries});
  } catch (e) { balMsg(e.message, 'error'); return; }
  for (const r of BAL.plan.rows) if (!(r.index in BAL.checked)) BAL.checked[r.index] = r.checked;
  balMsg('');
  balRenderPreview();
}

function balRenderPreview() {
  const P = BAL.plan, box = document.getElementById('bal-preview');
  box.replaceChildren();
  if (!P) return;
  for (const e of P.period_errors) box.append(balEl('div', {class: 'bal-note error'}, '✗ ' + e));
  for (const w of P.period_warnings) box.append(balEl('div', {class: 'bal-note warn'}, '⚠ ' + w));

  if (BAL.mode === 'paste' && BAL.needsAi) {
    const ai = BAL.settings && BAL.settings.ai;
    const note = balEl('div', {class: 'bal-note warn'},
      'Some accounts on this page could not be read locally (the layout may have changed). ');
    if (ai && ai.enabled && !ai.paused) {
      note.append(balEl('button', {id: 'bal-ai-btn', class: 'btn-sm', onclick: balReadAi}, 'Read with AI'),
        ' sends only the outline below: amounts and account numbers become tokens, names become "~".');
    } else {
      note.append(ai && ai.paused ? 'AI is paused: ' + ai.paused : (ai && ai.reason) || 'AI is not available.');
    }
    box.append(note);
  }
  if (BAL.mode === 'paste' && BAL.outline) {
    box.append(balEl('details', {class: 'bal-outline'}, balEl('summary', {}, 'What the AI would see (nothing is sent unless you ask)'),
      balEl('pre', {}, BAL.outline)));
  }

  const opts = [{id: '', name: '— not on my balance sheet —'}].concat(
    (P.accounts || []).filter(a => !a.archived).map(a => ({id: a.id, name: a.name})));
  const table = balEl('table', {class: 'bal-table'});
  table.append(balEl('thead', {}, balEl('tr', {},
    balEl('th', {}, ''), balEl('th', {}, {paste: 'On the page', file: 'In the file'}[BAL.mode] || 'Account'),
    balEl('th', {}, 'Flow account'), balEl('th', {class: 'num'}, 'New value'),
    balEl('th', {class: 'num'}, 'Previous'), balEl('th', {}, 'Checks'))));
  const tbody = balEl('tbody');
  const rows = P.rows.slice().sort((a, b) => (b.value !== null) - (a.value !== null));
  for (const r of rows) {
    const cb = balEl('input', {type: 'checkbox', checked: BAL.checked[r.index] && r.applicable,
      disabled: !r.applicable, onchange: e => { BAL.checked[r.index] = e.target.checked; balCount(); }});
    const page = BAL.mode !== 'type'
      ? balEl('div', {}, balEl('div', {class: 'lbl'}, r.label || '(no name)'),
          balEl('div', {class: 'sub'}, (r.last4 ? '…' + r.last4 : '') + (r.shown !== null ? ' · ' + balMoney(r.shown) : '')
            + (r.source === 'ai' ? ' · read by AI' : r.source === 'marker' ? ' · read where you showed Flow' : '')),
          balRememberBox(r))
      : balEl('div', {class: 'lbl'}, r.account_name || '');
    let acctCell;
    if (BAL.mode !== 'type' && r.kind !== 'rewards') {
      const sel = balEl('select', {onchange: e => { BAL.picks[r.index] = e.target.value ? +e.target.value : null; delete BAL.checked[r.index]; balPreview(); }});
      for (const o of opts) sel.append(balEl('option', {value: o.id, selected: String(r.account_id || '') === String(o.id)}, o.name));
      acctCell = balEl('div', {}, sel, r.link ? balEl('div', {class: 'sub'}, `…${r.last4} will be saved on this account`) : null);
    } else {
      acctCell = balEl('div', {class: 'sub'}, r.account_name || '—');
    }
    const prev = r.previous ? balEl('div', {}, balMoney(r.previous.amount),
      balEl('div', {class: 'sub'}, balMonthLabel(r.previous.period))) : '—';
    const checks = balEl('div', {class: 'checks'});
    for (const e of r.errors) checks.append(balEl('div', {class: 'err'}, '✗ ' + e));
    for (const w of r.warnings) checks.append(balEl('div', {class: 'warn'}, '⚠ ' + w));
    if (r.skip) checks.append(balEl('div', {class: 'skip'}, r.skip));
    if (BAL.mode === 'paste' && r.kind !== 'rewards' && r.kind !== 'closed' && r.shown === null)
      checks.append(balEl('button', {class: 'btn-sm', onclick: () => balTeachStart(r.account_id)}, 'Show Flow where it is'));
    if (!r.errors.length && !r.warnings.length && !r.skip && r.value !== null) checks.append(balEl('div', {class: 'ok'}, '✓'));
    tbody.append(balEl('tr', {class: r.value === null ? 'dim' : ''},
      balEl('td', {}, cb), balEl('td', {}, page), balEl('td', {}, acctCell),
      balEl('td', {class: 'num'}, r.value !== null ? balMoney(r.value) : '—'),
      balEl('td', {class: 'num'}, prev), balEl('td', {}, checks)));
  }
  table.append(tbody);
  box.append(balEl('div', {class: 'bal-table-wrap'}, table));
  if (P.not_in_paste && P.not_in_paste.length && BAL.mode !== 'type') {
    box.append(balEl('div', {class: 'bal-note'}, `Not in this ${BAL.mode === 'file' ? 'file' : 'paste'} (they keep their last value until updated): `
      + P.not_in_paste.map(m => m.name).join(', ') + '. '));
  }
  if (BAL.mode === 'paste' && BAL.lines.length) {
    box.append(balEl('div', {class: 'bal-row'}, balEl('button', {class: 'btn-sm', onclick: () => balTeachStart(null)},
      'Show Flow where an account is…'), balEl('span', {class: 'sub'},
      'An account missing or read wrong? Click its name, then its balance; Flow reads it there in every paste after this, without the AI.')));
    if (BAL.teach) box.append(balTeachView());
  }
  balCount();
}

function balCount() {
  const n = BAL.plan ? BAL.plan.rows.filter(r => BAL.checked[r.index] && r.applicable).length : 0;
  const btn = document.getElementById('bal-apply');
  btn.disabled = !n || BAL.busy;
  btn.textContent = n ? `Apply ${n} for ${balMonthLabel(BAL.plan.period)}` : 'Apply';
}

async function balApply() {
  const P = BAL.plan;
  if (!P) return;
  const picked = P.rows.filter(r => BAL.checked[r.index] && r.applicable);
  const rows = picked.map(r => ({account_id: r.account_id, value: r.value, last4: r.link ? r.last4 : ''}));
  if (!rows.length) return;
  const remember = BAL.mode === 'paste' ? picked.filter(r => balRemembers(r))
    .map(r => ({account_id: r.account_id, last4: r.last4, amount: r.shown})) : [];
  BAL.busy = true; balCount();
  try {
    const d = await balApi('/api/balances/apply', {period: P.period, rows, source: {type: 'manual', file: 'file'}[BAL.mode] || 'paste',
      ...(remember.length ? {remember, text: document.getElementById('bal-text').value} : {})});
    balMsg(`✓ ${d.written} balance${d.written === 1 ? '' : 's'} saved for ${balMonthLabel(d.period)}`
      + (d.linked ? `; ${d.linked} account number${d.linked === 1 ? '' : 's'} remembered` : '')
      + (d.remembered ? `; Flow will read ${d.remembered} account${d.remembered === 1 ? '' : 's'} the AI found without it next time` : '') + '.', 'good');
    document.getElementById('bal-preview').replaceChildren();
    BAL.plan = null; BAL.typed = {};
    if (BAL.mode === 'type') balRenderTyping();
    loadAll();
  } catch (e) {
    balMsg(e.message, 'error');
  } finally {
    BAL.busy = false; balCount();
  }
}

// ── File: a balance export read with a balance import format ─────────────────
// POST /api/balances/import/read returns rows shaped like a paste reading; the preview and
// apply are the same. A layout without a format (or with changed columns) opens the format editor.
async function balReadFile(allowErrors) {
  const file = document.getElementById('bal-file').files[0];
  if (!file) return;
  BAL.picks = {}; BAL.checked = {}; BAL.plan = null;
  document.getElementById('bal-preview').replaceChildren();
  balMsg(`Reading ${file.name}…`);
  const fd = new FormData();
  fd.append('file', file);
  if (allowErrors) fd.append('allow_errors', 'true');
  const res = await fetch('/api/balances/import/read', {method: 'POST', body: fd, headers: {'X-Flow-Today': balToday()}});
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { balFileProblem(file, d.detail); return; }
  BAL.items = d.items; BAL.fileFormat = d.profile;
  if (d.suggested_period) document.getElementById('bal-period').value = d.suggested_period.slice(0, 7);
  await balPreview();
  const line = balEl('div', {class: 'bal-row'}, `Read with the format “${d.profile.name}”`
      + (d.as_of ? `, values as of ${d.as_of} (period set to that month)` : '') + '. ',
    balEl('button', {class: 'btn-sm', onclick: () => ipEditSaved(d.profile.id, {file, onSaved: () => balReadFile()})}, 'Edit the format'));
  const msg = document.getElementById('bal-msg');
  msg.replaceChildren(line);
  if (d.error_count) msg.append(balEl('div', {class: 'bal-note warn'}, `${d.error_count} row(s) couldn't be read and were left out.`));
}

function balFileProblem(file, detail) {
  if (!detail || typeof detail !== 'object' || !detail.code) { balMsg(typeof detail === 'string' ? detail : 'Could not read the file.', 'error'); return; }
  const note = balEl('div', {class: 'bal-note warn'}, detail.message + ' ');
  if (detail.code === 'no_profile') {
    note.append(balEl('button', {class: 'btn-sm btn-primary', onclick: () =>
      ipOpenEditor({file, kind: 'balance', draft: detail.draft, onSaved: () => balReadFile()})}, 'Set up the format…'));
  } else if (detail.code === 'profile_drift') {
    note.append(balEl('button', {class: 'btn-sm btn-primary', onclick: () =>
      ipEditSaved(detail.profile.id, {file, spec: detail.draft, diff: detail.diff, onSaved: () => balReadFile()})}, 'Update the format…'));
  } else if (detail.code === 'row_errors') {
    note.append(balEl('ul', {}, ...(detail.errors || []).slice(0, 20).map(e => balEl('li', {}, `Row ${e.line}: ${e.message}`))),
      balEl('button', {class: 'btn-sm', onclick: () => balReadFile(true)}, 'Read the rest'));
  }
  document.getElementById('bal-msg').replaceChildren(note);
}

// ── Paste markers: teaching Flow where an account's balance is ────────────────
// A row the AI read can be remembered (a marker that finds the same amount next time).
function balRemembers(r) {
  return r.source === 'ai' && r.account_id && r.last4 && r.shown !== null && BAL.remember[r.index] !== false;
}
function balRememberBox(r) {
  if (BAL.mode !== 'paste' || r.source !== 'ai' || !r.account_id || !r.last4 || r.shown === null) return null;
  return balEl('label', {class: 'bal-remember'}, balEl('input', {type: 'checkbox', checked: BAL.remember[r.index] !== false,
    onchange: e => { BAL.remember[r.index] = e.target.checked; }}), ' Remember where this is (no AI next time)');
}

// Two clicks on the pasted lines: the account's name, then its balance. The server keeps the
// marker only if it finds that same amount again (balance_sheet.marker_from_example).
function balTeachStart(accountId) {
  BAL.teach = {account: accountId || null, label: null, error: ''};
  balRenderPreview();
  setTimeout(() => { const v = document.getElementById('bal-teach'); if (v) v.scrollIntoView({block: 'nearest'}); }, 30);
}
function balTeachView() {
  const T = BAL.teach;
  const accts = (BAL.plan.accounts || []).filter(a => !a.archived);
  const sel = balEl('select', {onchange: e => { T.account = e.target.value ? +e.target.value : null; }},
    balEl('option', {value: ''}, '— which account? —'),
    ...accts.map(a => balEl('option', {value: a.id, selected: a.id === T.account}, a.name)));
  const step = T.label === null ? 'Click the line with the account\'s name.' : 'Now click its balance (an amount at most 6 lines below).';
  const view = balEl('div', {id: 'bal-teach', class: 'bal-teach'},
    balEl('div', {class: 'bal-row'}, balEl('b', {}, 'Show Flow where an account is'), sel,
      balEl('button', {class: 'btn-sm', onclick: () => { BAL.teach = null; balRenderPreview(); }}, 'Cancel')),
    balEl('div', {class: 'sub'}, step),
    T.error ? balEl('div', {class: 'bal-note error'}, T.error) : null);
  const list = balEl('div', {class: 'bal-teach-lines'});
  BAL.lines.forEach((l, i) => {
    if (!l.text) return;
    const row = balEl('div', {class: 'bal-teach-line' + (i === T.label ? ' chosen' : '')});
    if (T.label === null) {
      row.classList.add('pickable');
      row.addEventListener('click', () => { T.label = i; T.error = ''; balRenderPreview(); });
      row.append(l.text);
    } else {
      let at = 0;
      const near = i >= T.label && i <= T.label + 6;
      for (const a of l.amounts) {
        row.append(l.text.slice(at, a.start));
        row.append(near ? balEl('button', {class: 'bal-amt', onclick: () => balTeachSave(i, a.start)}, l.text.slice(a.start, a.end))
                        : l.text.slice(a.start, a.end));
        at = a.end;
      }
      row.append(l.text.slice(at));
    }
    list.append(row);
  });
  view.append(list);
  if (T.label !== null) view.append(balEl('button', {class: 'btn-sm', onclick: () => { T.label = null; balRenderPreview(); }}, 'Pick a different name line'));
  return view;
}
async function balTeachSave(amountLine, amountStart) {
  const T = BAL.teach;
  if (!T.account) { T.error = 'Pick the account first.'; balRenderPreview(); return; }
  try {
    await balApi('/api/balances/markers/from-example', {text: document.getElementById('bal-text').value,
      account_id: T.account, label_line: T.label, amount_line: amountLine, amount_start: amountStart});
  } catch (e) { T.error = e.message; balRenderPreview(); return; }
  const name = ((BAL.plan.accounts || []).find(a => a.id === T.account) || {}).name || 'that account';
  await balRead();
  balMsg(`✓ Flow will read ${name} there in this and every later paste.`, 'good');
}

// The account editor's list of where Flow reads this account on pasted summaries.
async function balLoadMarkers() {
  const box = document.getElementById('bal-markers');
  if (!balAcctId) { box.replaceChildren(); return; }
  let list;
  try { list = await balApi(`/api/balances/markers?account_id=${balAcctId}`); } catch (e) { box.replaceChildren(e.message); return; }
  box.replaceChildren(list.length ? balEl('div', {class: 'bal-marker-list'}, ...list.map(m => balEl('div', {class: 'bal-marker'},
      balEl('div', {}, balEl('b', {}, m.label_text), m.last4 ? ` …${m.last4}` : '',
        balEl('div', {class: 'sub'}, (m.amount_rule === 'on_line_with' ? `the first amount after “${m.amount_label}”` : 'the first amount after the name')
          + (m.source === 'ai' ? ' · from an AI reading' : ' · you showed Flow'))),
      balEl('button', {class: 'btn-sm', onclick: async () => {
        await fetch(`/api/balances/markers/${m.id}`, {method: 'DELETE'}); balLoadMarkers(); }}, 'Delete'))))
    : balEl('div', {class: 'acct-dlg-help'}, 'None yet. In Update balances → Paste, "Show Flow where an account is…" adds one.'));
}

// ── Type ──────────────────────────────────────────────────────────────────────
function balLatest(a) {
  // The account's last value (real or carried) and whether it was carried.
  const s = (tsData && tsData.accounts || []).find(x => x.id === a.id);
  if (!s) return null;
  for (let i = s.values.length - 1; i >= 0; i--) if (s.values[i] !== null) return {v: s.values[i], carried: s.carried && s.carried[i], p: tsData.periods[i]};
  return null;
}
function balRenderTyping() {
  const box = document.getElementById('bal-type-list');
  box.replaceChildren();
  const live = accounts.filter(a => !a.archived);
  if (!live.length) { box.append(balEl('div', {class: 'bal-note'}, 'No balance-sheet accounts yet: add one under Manage Accounts.')); return; }
  for (const a of live) {
    const last = balLatest(a);
    const input = balEl('input', {type: 'text', inputmode: 'decimal', placeholder: last ? Math.abs(last.v).toFixed(2) : '0.00',
      value: BAL.typed[a.id] || '', oninput: e => { BAL.typed[a.id] = e.target.value; }});
    box.append(balEl('div', {class: 'bal-type-row'},
      balEl('div', {class: 'lbl'}, a.name, balEl('span', {class: 'badge badge-' + a.account_type}, a.account_type === 'liability' ? 'owed' : 'asset')),
      balEl('div', {class: 'sub'}, last ? `${balMoney(last.v)} in ${balMonthLabel(last.p)}${last.carried ? ' (carried)' : ''}` : 'no history'),
      input));
  }
}

// ── Thresholds ────────────────────────────────────────────────────────────────
async function balSaveThresholds() {
  const pct = parseFloat(document.getElementById('bal-th-pct').value);
  const abs = parseFloat(document.getElementById('bal-th-abs').value);
  try {
    BAL.settings = await balApi('/api/balances/settings', {alert_pct: pct, alert_abs: abs}, 'PUT');
    balMsg('✓ Alert thresholds saved.', 'good');
    if (BAL.plan) balPreview();
  } catch (e) { balMsg(e.message, 'error'); }
}

// ── Account editor ────────────────────────────────────────────────────────────
let balAcctId = null;
async function balOpenAccount(id) {
  balAcctId = id || null;
  const m = document.getElementById('bal-acct-modal');
  m.classList.remove('hidden');
  document.getElementById('bal-acct-title').textContent = id ? 'Edit balance account' : 'New balance account';
  document.getElementById('bal-acct-err').textContent = '';
  document.getElementById('bal-acct-del').classList.toggle('hidden', !id);
  document.getElementById('bal-hist').replaceChildren();
  const a = id ? accounts.find(x => x.id === id) : null;
  document.getElementById('bal-a-name').value = a ? a.name : '';
  document.getElementById('bal-a-type').value = a ? a.account_type : 'asset';
  document.getElementById('bal-a-inst').value = a && a.institution || '';
  document.getElementById('bal-a-last4').value = a ? (a.last4 || []).join(', ') : '';
  document.getElementById('bal-a-pct').value = a && a.alert_pct !== null ? a.alert_pct : '';
  document.getElementById('bal-a-abs').value = a && a.alert_abs !== null ? a.alert_abs : '';
  document.getElementById('bal-a-archived').checked = !!(a && a.archived);
  document.getElementById('bal-a-options').classList.toggle('hidden', !id);
  document.getElementById('bal-hist-wrap').classList.toggle('hidden', !id);
  document.getElementById('bal-markers-wrap').classList.toggle('hidden', !id);
  ipAccountSection(document.getElementById('bal-formats'), 'balance', id || null);
  if (id) { balLoadHistory(); balLoadMarkers(); }
}
function balCloseAccount() { document.getElementById('bal-acct-modal').classList.add('hidden'); }

async function balSaveAccount() {
  const num = v => v.trim() === '' ? null : parseFloat(v);
  const body = {
    name: document.getElementById('bal-a-name').value,
    account_type: document.getElementById('bal-a-type').value,
    institution: document.getElementById('bal-a-inst').value,
    last4: document.getElementById('bal-a-last4').value.split(/[,\s]+/).filter(Boolean),
    alert_pct: num(document.getElementById('bal-a-pct').value),
    alert_abs: num(document.getElementById('bal-a-abs').value),
  };
  try {
    if (balAcctId) await balApi('/api/accounts/' + balAcctId, {...body, archived: document.getElementById('bal-a-archived').checked}, 'PATCH');
    else await balApi('/api/accounts', body);
    balCloseAccount();
    loadAll();
  } catch (e) { document.getElementById('bal-acct-err').textContent = e.message; }
}

async function balDeleteAccount() {
  const a = accounts.find(x => x.id === balAcctId);
  if (!a || !confirm(`Delete ${a.name} and all of its balance history? Archive it instead to keep the history.`)) return;
  try { await balApi('/api/accounts/' + a.id, undefined, 'DELETE'); balCloseAccount(); loadAll(); }
  catch (e) { document.getElementById('bal-acct-err').textContent = e.message; }
}

async function balLoadHistory() {
  const box = document.getElementById('bal-hist');
  let d;
  try { d = await balApi(`/api/accounts/${balAcctId}/balances`); } catch (e) { box.replaceChildren(e.message); return; }
  const liab = d.account.account_type === 'liability';
  const rows = d.balances.map(b => {
    const input = balEl('input', {type: 'text', value: (liab ? -b.amount : b.amount).toFixed(2)});
    return balEl('tr', {},
      balEl('td', {}, balMonthLabel(b.period)),
      balEl('td', {class: 'num'}, input),
      balEl('td', {class: 'sub'}, b.source),
      balEl('td', {}, balEl('button', {class: 'btn-sm', onclick: () => balSaveOne(b.period, input.value, liab)}, 'Save'),
        balEl('button', {class: 'btn-sm', style: 'color:var(--red)', onclick: () => balDeleteOne(b.period)}, '✕')));
  });
  const addP = balEl('input', {type: 'month', id: 'bal-h-p'});
  const addV = balEl('input', {type: 'text', placeholder: liab ? 'amount owed' : 'balance'});
  box.replaceChildren(balEl('table', {class: 'bal-table'},
    balEl('thead', {}, balEl('tr', {}, balEl('th', {}, 'Period'), balEl('th', {class: 'num'}, liab ? 'Owed' : 'Balance'), balEl('th', {}, 'Source'), balEl('th', {}, ''))),
    balEl('tbody', {}, ...rows,
      balEl('tr', {}, balEl('td', {}, addP), balEl('td', {class: 'num'}, addV), balEl('td', {}, ''),
        balEl('td', {}, balEl('button', {class: 'btn-sm', onclick: () => addP.value && balSaveOne(addP.value + '-01', addV.value, liab)}, 'Add'))))));
}

async function balSaveOne(period, text, liab) {
  const v = parseFloat(String(text).replace(/[$,\s]/g, ''));
  if (isNaN(v)) { document.getElementById('bal-acct-err').textContent = 'Enter a number.'; return; }
  try {
    await balApi('/api/balances', {account_id: balAcctId, period, amount: liab ? -v : v}, 'PUT');
    document.getElementById('bal-acct-err').textContent = '';
    balLoadHistory(); loadAll();
  } catch (e) { document.getElementById('bal-acct-err').textContent = e.message; }
}
async function balDeleteOne(period) {
  if (!confirm(`Delete the ${balMonthLabel(period)} value? The month before carries forward instead.`)) return;
  try {
    await balApi(`/api/balances?account_id=${balAcctId}&period=${period}`, undefined, 'DELETE');
    balLoadHistory(); loadAll();
  } catch (e) { document.getElementById('bal-acct-err').textContent = e.message; }
}

// A paste that looks like an account summary (several "ending in …1234" with $ amounts).
function balLooksLikeSummary(text) {
  const nums = (text.match(/(ending\s*in|\.{3}|…|\*{2,}|\bx)\s*\d{4}\b/gi) || []).length;
  return nums >= 2 && /\$\s?\d/.test(text);
}
