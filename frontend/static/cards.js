// ═══════════════════════════════════════════════════════════════════════════════
// CARDS: credit card uploads, categories, subscriptions, spend analytics, insights
// Loaded after the main inline script; reuses fmt, fmtFull, escHtml, cfFmtDate,
// destroyChart, charts, GRID and TICK from it.
// ═══════════════════════════════════════════════════════════════════════════════

const CD_L1_COLORS = {
  'Travel': '#378ADD', 'Food & Beverage': '#D85A30', 'Shopping': '#7F77DD', 'Home': '#63A02A',
  'Transportation': '#BA7517', 'Health & Personal': '#D4537E', 'Entertainment': '#5DCAA5',
  'Bills & Utilities': '#0EA5E9', 'Digital & Media': '#4338CA', 'Education / Family': '#A16207', 'Financial / Investment': '#64748B',
  'Pets': '#C2410C', 'Giving': '#16A34A', 'Government & Taxes': '#6B7280', 'Services': '#0D9488',
  'Transfers / P2P': '#9333EA', 'Card Credits & Adjustments': '#22C55E', 'Uncategorized': '#F59E0B',
  'Excluded (non-spend)': '#9CA3AF',
};
const CD_CARD_COLORS = ['#378ADD', '#D85A30', '#5DCAA5', '#7F77DD', '#BA7517', '#D4537E', '#63A02A'];
const CD_TABS = [
  ['transactions', 'Transactions'], ['overview', 'Overview'], ['categories', 'Categories'], ['monthly', 'Monthly'],
  ['merchants', 'Merchants'], ['subscriptions', 'Subscriptions'], ['review', 'Review'],
  ['insights', 'Insights'], ['settings', 'Settings'],
];
const CD_CHOOSABLE_L1 = ['Travel', 'Food & Beverage', 'Shopping', 'Home', 'Transportation', 'Health & Personal',
  'Entertainment', 'Bills & Utilities', 'Digital & Media', 'Education / Family', 'Financial / Investment', 'Pets', 'Giving',
  'Government & Taxes', 'Services', 'Transfers / P2P'];
const CD_ROLES = {none: 'Neither', bill: 'A bill (amount varies)', subscription: 'A subscription (fixed price)', habit: 'An everyday habit (frequent, small)'};
const CD_FIXEDNESS = {discretionary: 'Discretionary', essential: 'Essential, variable', fixed: 'Relatively fixed'};
const CD_REWARDS = {general: 'General', dining: 'Dining', grocery: 'Groceries', gas: 'Gas', travel: 'Travel', streaming: 'Streaming',
  drugstore: 'Drugstore', home_improvement: 'Home improvement', utilities: 'Utilities', phone_plan: 'Phone plan', transit: 'Transit'};
const CD_TXN_TYPES = ['purchase', 'refund', 'benefit_credit', 'card_fee', 'interest', 'cash_advance', 'payment',
  'payment_reversal', 'carry_in', 'balance_transfer', 'adjustment'];

const CD = {
  ready: false, accounts: [], taxonomy: [], tab: 'transactions', page: 1, perPage: 50,
  tf: {q: '', l1: '', l2: '', type: 'spend', source: '', recurring: false, merchant: ''},
  catLevel: 'l1', showHabitual: false, aiTimer: null, aiStatus: null, catTxn: null, editAcct: null,
};

const h = v => escHtml(v === null || v === undefined ? '' : String(v));
const js = v => h(JSON.stringify(v));   // a JS literal safe inside a double-quoted attribute
const pct = v => (v === null || v === undefined) ? '—' : (v * 100).toFixed(1) + '%';
const money0 = v => (v === null || v === undefined) ? '—' : fmtFull(v).replace(/\.\d\d$/, '');

async function cdApi(path, opts = {}) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || ('Request failed (' + res.status + ')'));
  return body;
}
function cdJson(method, body) {
  return {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
}

// ── Shell ─────────────────────────────────────────────────────────────────────
function cdInjectModals() {
  if (document.getElementById('cd-cat-modal')) return;
  document.body.insertAdjacentHTML('beforeend', `
<div id="cd-cat-modal" class="cd-modal hidden"><div class="modal-box">
  <h2 id="cd-cat-title">Edit merchant</h2>
  <p id="cd-cat-raw" class="cd-note"></p>
  <div><label class="f">Merchant name</label><input type="text" id="cd-cat-name" maxlength="120"></div>
  <div><label class="f">Parent brand <span class="cd-muted">(optional: groups brands, e.g. Hampton Inn under Hilton)</span></label>
    <input type="text" id="cd-cat-family" maxlength="120"></div>
  ${cdCatFields('cd-cat')}
  <div id="cd-cat-scope">
    <label class="f">Apply to</label>
    <label class="radio"><input type="radio" name="cd-scope" value="this" checked onchange="cdCatScopeChanged()"> <span>Just this transaction</span></label>
    <label class="radio" id="cd-scope-merchant-row"><input type="radio" name="cd-scope" value="merchant" onchange="cdCatScopeChanged()"> <span id="cd-scope-merchant-label"></span></label>
    <label class="radio" id="cd-overwrite-row" style="display:none;padding-left:22px"><input type="checkbox" id="cd-cat-overwrite"> <span id="cd-overwrite-label"></span></label>
    <label class="radio" id="cd-remember-row"><input type="checkbox" id="cd-cat-remember"> <span>Also use this name and category for future imports of this merchant</span></label>
  </div>
  <div id="cd-cat-err" class="err"></div>
  <div class="modal-footer"><button class="btn-sm" onclick="cdCloseModal('cd-cat-modal');cdGoMerchant(CD.catSiblings.name)">Merchant page →</button>
    <button class="btn-sm" style="margin-right:auto" title="Add it as a recurring charge, matched by merchant and amount" onclick="cdTrackRecurring()">Track as recurring…</button>
    <button onclick="cdCloseModal('cd-cat-modal')">Cancel</button>
    <button class="btn-primary" onclick="cdApplyCategory()">Save</button></div>
</div></div>

<div id="cd-cc-modal" class="cd-modal hidden"><div class="modal-box">
  <h2 id="cd-cc-title">New subcategory</h2>
  <p class="cd-note" id="cd-cc-intro">Your own subcategory under one of Flow's categories. It works everywhere categories do: charts, rules, the Assistant.</p>
  <div id="cd-cc-kind-row" style="display:flex;gap:14px;font-size:13px">
    <label><input type="radio" name="cd-cc-kind" value="sub" checked onchange="cdCcKind()"> Subcategory</label>
    <label><input type="radio" name="cd-cc-kind" value="detail" onchange="cdCcKind()"> Detail under a subcategory</label></div>
  <div><label class="f">Category</label><select id="cd-cc-l1" onchange="cdCcKind()"></select></div>
  <div id="cd-cc-parent-row" style="display:none"><label class="f">Subcategory</label><select id="cd-cc-parent"></select></div>
  <div><label class="f">Name</label><input type="text" id="cd-cc-name" maxlength="60" placeholder="Pottery classes"></div>
  <div id="cd-cc-attrs">
    <div><label class="f">Counts as</label><select id="cd-cc-fixed">${Object.entries(CD_FIXEDNESS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></div>
    <div><label class="f">For subscription and bill detection, these charges are usually</label><select id="cd-cc-role">${Object.entries(CD_ROLES).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></div>
    <div><label class="f">Card rewards bucket</label><select id="cd-cc-rewards">${Object.entries(CD_REWARDS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></div>
  </div>
  <label class="radio"><input type="checkbox" id="cd-cc-ai" checked> <span>The AI may see this name (untick for anything personal; it is then sent as “(private)”)</span></label>
  <div id="cd-cc-err" class="err"></div>
  <div class="modal-footer"><button onclick="cdCloseModal('cd-cc-modal')">Cancel</button>
    <button class="btn-primary" onclick="cdSaveCustomCategory()">Save</button></div>
</div></div>

<div id="cd-dc-modal" class="cd-modal hidden"><div class="modal-box">
  <h2 id="cd-dc-title">Delete</h2>
  <p class="cd-note" id="cd-dc-intro"></p>
  <div id="cd-dc-move">${cdCatFields('cd-dc')}</div>
  <div id="cd-dc-err" class="err"></div>
  <div class="modal-footer"><button onclick="cdCloseModal('cd-dc-modal')">Cancel</button>
    <button class="btn-primary" style="background:var(--red);border-color:var(--red)" onclick="cdConfirmDeleteCategory()">Delete</button></div>
</div></div>

<div id="cd-man-modal" class="cd-modal hidden"><div class="modal-box">
  <h2 id="cd-man-title">Add a recurring charge</h2>
  <p class="cd-note">For a subscription Flow can't separate on its own, such as Kindle Unlimited billed as "Amazon". Charges from the merchant within the amount range become this series, and leave the merchant's other patterns.</p>
  <div><label class="f">Name</label><input type="text" id="cd-man-name" maxlength="80" placeholder="Kindle Unlimited"></div>
  <div><label class="f">Billed as (merchant or parent brand)</label><input type="text" id="cd-man-merchant" list="cd-man-merchants" placeholder="Amazon"><datalist id="cd-man-merchants"></datalist></div>
  <div><label class="f">Card</label><select id="cd-man-card"></select></div>
  <div style="display:flex;gap:8px"><div style="flex:1"><label class="f">Amount from</label><input type="text" id="cd-man-min" inputmode="decimal" placeholder="11.00"></div>
    <div style="flex:1"><label class="f">to</label><input type="text" id="cd-man-max" inputmode="decimal" placeholder="13.00"></div></div>
  <div class="cd-note">Leave the amounts empty to include every charge from the merchant.</div>
  <div style="display:flex;gap:8px"><div style="flex:1"><label class="f">Every</label><select id="cd-man-cadence">
      ${[['weekly', 'week'], ['biweekly', '2 weeks'], ['monthly', 'month'], ['bimonthly', '2 months'], ['quarterly', 'quarter'], ['semiannual', '6 months'], ['annual', 'year']]
        .map(([v, l]) => `<option value="${v}"${v === 'monthly' ? ' selected' : ''}>${l}</option>`).join('')}</select></div>
    <div style="flex:1"><label class="f">Kind</label><select id="cd-man-kind"><option value="subscription">Subscription</option>
      <option value="recurring bill">Bill</option><option value="recurring service">Service</option></select></div></div>
  <div id="cd-man-err" class="err"></div>
  <div class="modal-footer"><button onclick="cdCloseModal('cd-man-modal')">Cancel</button>
    <button class="btn-primary" onclick="cdSaveManual()">Save</button></div>
</div></div>

<div id="cd-txn-modal" class="cd-modal hidden"><div class="modal-box">
  <h2>Transaction</h2>
  <p id="cd-txn-raw" class="cd-note"></p>
  <div><label class="f">Type</label><select id="cd-txn-type">${CD_TXN_TYPES.map(t => `<option value="${t}">${t.replace(/_/g, ' ')}</option>`).join('')}</select>
    <div class="cd-note" style="margin-top:4px">Purchases, refunds, benefit credits and card fees count as spend; everything else is excluded from spend totals.</div></div>
  <div><label class="f">Notes</label><input type="text" id="cd-txn-notes"></div>
  <div id="cd-txn-err" class="err"></div>
  <div class="modal-footer"><button onclick="cdCloseModal('cd-txn-modal')">Cancel</button>
    <button class="btn-primary" onclick="cdSaveTxn()">Save</button></div>
</div></div>

<div id="cd-acct-modal" class="cd-modal hidden"><div class="modal-box acct-dlg">
  <h2 id="cd-acct-title">New card account</h2>
  <div class="acct-field"><label for="cd-acct-name">Name</label>
    <input type="text" id="cd-acct-name" maxlength="80" placeholder="e.g. Travel rewards card"></div>
  <div class="acct-field"><label for="cd-acct-issuer">Institution (optional)</label>
    <input type="text" id="cd-acct-issuer" maxlength="120">
    <div class="acct-dlg-help">Filled in from the import format on first import.</div></div>
  <div class="acct-field"><label for="cd-acct-notes">Notes</label><textarea id="cd-acct-notes" rows="2"></textarea></div>
  <div class="acct-dlg-section">
    <div class="acct-dlg-h">Card details</div>
    <div class="acct-field"><label for="cd-acct-last4">Card numbers, last 4 digits (optional)</label>
      <input type="text" id="cd-acct-last4" placeholder="e.g. 1234, 5678">
      <div class="acct-dlg-help">One per card on the account (authorized users, replacements). Numbers in imported files are added automatically; bulk import uses them to find a file's card.</div></div>
    <div class="acct-field"><label for="cd-acct-names">Cardholder names, one per line</label><textarea id="cd-acct-names" rows="2"></textarea>
      <div class="acct-dlg-help">Removed from merchant names before anything is sent to the AI.</div></div>
  </div>
  <div class="acct-dlg-section" id="cd-acct-formats"></div>
  <div class="acct-dlg-section" id="cd-acct-options">
    <div class="acct-dlg-h">Options</div>
    <label class="acct-dlg-check"><input type="checkbox" id="cd-acct-archived"> Archived (hidden from "All cards")</label>
  </div>
  <div id="cd-acct-err" class="acct-dlg-err"></div>
  <div class="modal-footer">
    <button id="cd-acct-delete" class="acct-dlg-delete" onclick="cdDeleteAccount()">Delete</button>
    <button onclick="cdCloseModal('cd-acct-modal')">Cancel</button>
    <button class="btn-primary" onclick="cdSaveAccount()">Save</button></div>
</div></div>`);
}

function cdCloseModal(id) { document.getElementById(id).classList.add('hidden'); }
function cdGoMerchant(name) {
  // From the Dashboard (Cash Flow Analysis) the Cards section isn't on screen yet: open it first.
  if (document.getElementById('cards-app').classList.contains('hidden')) {
    showSection('cards');
    setTimeout(() => cdShowMerchant(name), 80);
  } else cdShowMerchant(name);
}

async function cdShow() {
  cdInjectModals();
  if (!CD.ready) {
    document.getElementById('cd-tabs').innerHTML = CD_TABS.map(([k, label]) =>
      `<button class="cd-tab${k === CD.tab ? ' active' : ''}" data-tab="${k}" onclick="cdTab('${k}')">${label}</button>`).join('');
    document.getElementById('cd-panels').innerHTML = CD_TABS.map(([k]) =>
      `<div class="cd-panel${k === CD.tab ? ' active' : ''}" id="cd-panel-${k}"></div>`).join('');
    try { CD.taxonomy = (await cdApi('/api/cards/taxonomy')).tree; } catch (e) { CD.taxonomy = []; }
    CD.ready = true;
  }
  await cdLoadAccounts();
  if (CD.pendingAccount !== null && CD.pendingAccount !== undefined) {
    document.getElementById('cd-card-filter').value = CD.pendingAccount;
    CD.pendingAccount = null;
  }
  cdTab(CD.tab);
  cdPollAi(true);
}

async function cdLoadAccounts() {
  try { CD.accounts = await cdApi('/api/cards/accounts'); } catch (e) { CD.accounts = []; }
  const sel = document.getElementById('cd-card-filter');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All cards</option>' + CD.accounts.map(a =>
    `<option value="${a.id}">${h(a.name)}${a.archived ? ' (archived)' : ''}</option>`).join('');
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function cdTab(name) {
  if (name === 'categories' && CD.tab === 'categories') CD.catDetail = null;   // clicking the open tab goes back to the list
  if (name === 'merchants' && CD.tab === 'merchants') CD.merchantDetail = null;
  CD.tab = name;
  document.querySelectorAll('#cd-tabs .cd-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('#cd-panels .cd-panel').forEach(p => p.classList.toggle('active', p.id === 'cd-panel-' + name));
  cdRefresh();
}

function cdFilterChanged() {
  const custom = document.getElementById('cd-period').value === 'custom';
  document.getElementById('cd-custom-range').style.display = custom ? 'flex' : 'none';
  CD.page = 1;
  cdSyncClear();
  cdRefresh();
}

// The card / period bar: "Clear filters" shows while anything differs from all cards, this year.
const CD_DEFAULT_PERIOD = 'ytd';
function cdSyncClear() {
  const g = id => document.getElementById(id);
  g('cd-clear-filters').hidden = !(g('cd-card-filter').value || g('cd-period').value !== CD_DEFAULT_PERIOD || g('cd-common').checked);
}
function cdClearFilters() {
  const g = id => document.getElementById(id);
  g('cd-card-filter').value = ''; g('cd-period').value = CD_DEFAULT_PERIOD; g('cd-common').checked = false;
  g('cd-from').value = ''; g('cd-to').value = '';
  cdFilterChanged();
}

const CD_TF_DEFAULT = {q: '', l1: '', l2: '', type: 'spend', source: '', recurring: false, merchant: ''};
function cdTfIsDefault() { return Object.entries(CD_TF_DEFAULT).every(([k, v]) => CD.tf[k] === v); }
function cdTfClear() {
  CD.tf = {...CD_TF_DEFAULT};
  CD.page = 1;
  cdRenderTransactions(cdPanel('transactions'));
}

function cdPeriod() {
  const v = document.getElementById('cd-period').value;
  const iso = d => d.toISOString().slice(0, 10);
  const today = new Date();
  const back = days => { const d = new Date(); d.setDate(d.getDate() - days); return iso(d); };
  if (v === '30d') return {from: back(30), to: iso(today)};
  if (v === '90d') return {from: back(90), to: iso(today)};
  if (v === '12m') return {from: back(365), to: iso(today)};
  if (v === 'ytd') return {from: today.getFullYear() + '-01-01', to: iso(today)};
  if (v === 'lastyear') { const y = today.getFullYear() - 1; return {from: y + '-01-01', to: y + '-12-31'}; }
  if (v === 'custom') return {from: document.getElementById('cd-from').value, to: document.getElementById('cd-to').value};
  return {from: '', to: ''};
}

function cdQS(extra = {}, opts = {}) {
  const p = new URLSearchParams();
  const acct = document.getElementById('cd-card-filter').value;
  if (acct) p.set('accounts', acct);
  if (!opts.noDates) {
    const {from, to} = cdPeriod();
    if (from) p.set('from', from);
    if (to) p.set('to', to);
    if (document.getElementById('cd-common').checked && !opts.noCommon) p.set('common_period', 'true');
  }
  for (const [k, v] of Object.entries(extra)) if (v !== '' && v !== null && v !== undefined && v !== false) p.set(k, v);
  return p.toString();
}

function cdPanel(name) { return document.getElementById('cd-panel-' + name); }

async function cdRefresh() {
  cdSyncClear();                 // filters may be set from elsewhere (a link, the Assistant)
  const panel = cdPanel(CD.tab);
  if (!panel) return;            // Cards hasn't been opened yet (an edit made from the Dashboard)
  if (!CD.accounts.length && CD.tab !== 'settings') {
    panel.innerHTML = `<div class="empty"><h3>No credit cards yet</h3>
      <p>Create a card, then upload its statement export (CSV or XLSX) with ↑ Import.</p>
      <p style="margin-top:12px"><button class="btn-primary" onclick="cdOpenAccount()">+ New card account</button></p></div>`;
    return;
  }
  const fn = {
    transactions: cdRenderTransactions, overview: cdRenderOverview, categories: cdRenderCategories,
    monthly: cdRenderMonthly, merchants: cdRenderMerchants, subscriptions: cdRenderSubscriptions,
    review: cdRenderReview, insights: cdRenderInsights, settings: cdRenderSettings,
  }[CD.tab];
  try { await fn(panel); }
  catch (e) { panel.innerHTML = `<div class="empty"><h3>Could not load</h3><p>${h(e.message)}</p></div>`; }
}

// ── Category chip ─────────────────────────────────────────────────────────────
function cdChip(t, clickable = true) {
  const color = CD_L1_COLORS[t.cat_l1] || '#9CA3AF';
  const parts = [t.cat_l1, t.cat_l2];
  if (t.cat_l3 && t.cat_l3 !== t.cat_l2) parts.push(t.cat_l3);
  const excluded = t.cat_l1 === 'Excluded (non-spend)';
  const review = t.cat_l1 === 'Uncategorized' || (t.ai_confidence !== null && t.ai_confidence !== undefined && t.ai_confidence < 0.5);
  const title = parts.join(' › ') + (t.category_source ? ` (${cdSourceLabel(t.category_source)})` : '');
  const click = excluded ? `cdOpenTxn(${t.id || t.txn_id})` : (clickable ? `cdOpenCategory(${t.id || t.txn_id})` : '');
  return `<button class="cat-chip${excluded ? ' excluded' : ''}${review ? ' review' : ''}" title="${h(title)}"
    ${click ? `onclick="event.stopPropagation();${click}"` : ''}><span class="dot" style="background:${color}"></span>${review ? '⚠ ' : ''}${h(parts.join(' › '))}</button>`;
}
function cdSourceLabel(s) {
  return {rule: 'rule', issuer: "issuer's category", keyword: 'keyword guess', unclassified: 'not recognised',
    ai: 'AI', merchant: 'your merchant rule', user: 'set by you', amount_rule: 'your amount rule'}[s] || s;
}
function cdMarks(t) {
  let m = '';
  if (t.category_source === 'ai') m += `<span class="cd-mark ai" title="Categorized by AI${t.ai_confidence != null ? ' (confidence ' + Math.round(t.ai_confidence * 100) + '%)' : ''}">✦AI</span>`;
  if (t.category_source === 'user' || t.category_source === 'merchant' || t.category_source === 'amount_rule') m += `<span class="cd-mark user" title="${h(cdSourceLabel(t.category_source))}">✎</span>`;
  if (t.recurring_kind) m += `<span class="cd-mark rec" title="${h(t.recurring_kind)}">⟳</span>`;
  if (t.foreign_flag) m += `<span class="cd-mark fx" title="Foreign transaction">FX</span>`;
  return m;
}
function cdAmt(v) {
  return `<span class="${v < 0 ? 'amount-debit' : 'amount-credit'}">${fmtFull(v)}</span>`;
}

// ── Transactions ──────────────────────────────────────────────────────────────
async function cdRenderTransactions(panel) {
  const tf = CD.tf;
  const l1opts = CD.taxonomy.map(t => `<option${t.l1 === tf.l1 ? ' selected' : ''}>${h(t.l1)}</option>`).join('');
  const l2s = tf.l1 ? (CD.taxonomy.find(t => t.l1 === tf.l1) || {l2: []}).l2 : [];
  if (!panel.querySelector('#cd-tf-q')) {
    panel.innerHTML = `<div class="cd-card">
      <div class="cd-filter">
        <input type="text" id="cd-tf-q" placeholder="Search merchant, description, amount…" oninput="cdTfChanged(300)">
        <select id="cd-tf-l1" onchange="CD.tf.l2='';cdTfChanged()"><option value="">Any category</option></select>
        <select id="cd-tf-l2" onchange="cdTfChanged()"></select>
        <select id="cd-tf-type" onchange="cdTfChanged()">
          <option value="spend">Spend only</option><option value="all">All rows</option>
          <option value="excluded">Payments &amp; other non-spend</option><option value="purchase">Purchases</option>
          <option value="refund">Refunds</option><option value="card_fee">Card fees</option></select>
        <select id="cd-tf-source" onchange="cdTfChanged()"><option value="">Any source</option>
          <option value="needs_review">⚠ Needs review</option><option value="ai">✦ AI</option>
          <option value="user">✎ Set by you</option><option value="rule">Rules</option></select>
        <label class="cd-note" style="display:flex;gap:4px;align-items:center"><input type="checkbox" id="cd-tf-rec" onchange="cdTfChanged()"> ⟳ Recurring</label>
        <span id="cd-tf-merchant"></span>
        <button class="btn-sm clear-filters" id="cd-tf-clear" onclick="cdTfClear()" hidden>✕ Clear filters</button>
        <span class="count" id="cd-tf-count"></span>
      </div>
      <div class="cd-scroll"><table class="cd-table stack"><thead><tr>
        <th style="width:90px">Date</th><th>Merchant</th><th>Category</th><th class="num" style="width:110px">Amount</th>
        <th style="width:150px">Card</th><th style="width:40px"></th></tr></thead><tbody id="cd-tf-body"></tbody></table></div>
      <div class="cd-pager"><button onclick="cdPage(-1)" id="cd-prev">‹ Prev</button><span class="info" id="cd-page-info"></span>
        <button onclick="cdPage(1)" id="cd-next">Next ›</button></div></div>`;
  }
  document.getElementById('cd-tf-q').value = tf.q;
  document.getElementById('cd-tf-l1').innerHTML = '<option value="">Any category</option>' + l1opts;
  document.getElementById('cd-tf-l2').innerHTML = '<option value="">Any subcategory</option>' +
    l2s.map(x => `<option${x.name === tf.l2 ? ' selected' : ''}>${h(x.name)}</option>`).join('');
  document.getElementById('cd-tf-l2').style.display = tf.l1 ? '' : 'none';
  document.getElementById('cd-tf-type').value = tf.type;
  document.getElementById('cd-tf-source').value = tf.source;
  document.getElementById('cd-tf-rec').checked = tf.recurring;
  document.getElementById('cd-tf-clear').hidden = cdTfIsDefault();
  document.getElementById('cd-tf-merchant').innerHTML = tf.merchant
    ? `<button class="btn-sm" onclick="CD.tf.merchant='';cdTfChanged()">✕ ${h(tf.merchant)}</button>` : '';

  const d = await cdApi('/api/cards/transactions?' + cdQS({
    q: tf.q, l1: tf.l1, l2: tf.l2, type: tf.type, source: tf.source, recurring: tf.recurring ? 'true' : '',
    merchant: tf.merchant, page: CD.page, per_page: CD.perPage}));
  const body = document.getElementById('cd-tf-body');
  body.innerHTML = d.transactions.length ? d.transactions.map(t => `<tr>
      <td data-col="date">${cfFmtDate(t.date)}</td>
      <td data-col="merchant">${cdMerchantLink(t.canonical_merchant)}${cdMarks(t)}<div class="raw">${h(t.raw_description)}</div></td>
      <td data-col="category">${cdChip(t)}</td>
      <td data-col="amount" class="num">${cdAmt(t.amount)}</td>
      <td data-col="card" class="cd-muted">${h(t.card)}</td>
      <td data-col="actions"><button class="btn-sm" title="Type and notes" onclick="cdOpenTxn(${t.id})">⋯</button></td></tr>`).join('')
    : `<tr><td colspan="6" style="padding:30px;text-align:center" class="cd-muted">No transactions match.</td></tr>`;
  const pages = Math.max(1, Math.ceil(d.total / CD.perPage));
  document.getElementById('cd-page-info').textContent = `Page ${CD.page} of ${pages}`;
  document.getElementById('cd-prev').disabled = CD.page <= 1;
  document.getElementById('cd-next').disabled = CD.page >= pages;
  document.getElementById('cd-tf-count').textContent =
    `${d.total.toLocaleString()} row${d.total === 1 ? '' : 's'} · spend ${fmtFull(d.sum_spend)}`;
  CD.txnCache = Object.fromEntries(d.transactions.map(t => [t.id, t]));
}

let _cdTfTimer = null;
function cdTfChanged(delay = 0) {
  clearTimeout(_cdTfTimer);
  _cdTfTimer = setTimeout(() => {
    const g = id => document.getElementById(id);
    CD.tf.q = g('cd-tf-q').value.trim();
    CD.tf.l1 = g('cd-tf-l1').value;
    CD.tf.l2 = CD.tf.l1 ? g('cd-tf-l2').value : '';
    CD.tf.type = g('cd-tf-type').value;
    CD.tf.source = g('cd-tf-source').value;
    CD.tf.recurring = g('cd-tf-rec').checked;
    CD.page = 1;
    cdRenderTransactions(cdPanel('transactions'));
  }, delay);
}
function cdPage(delta) { CD.page = Math.max(1, CD.page + delta); cdRenderTransactions(cdPanel('transactions')); }

function cdDrill(filters) {
  CD.tf = {...CD_TF_DEFAULT, ...filters};
  CD.page = 1;
  cdTab('transactions');
}

// ── Merchant and category editing ─────────────────────────────────────────────
function cdCatFields(p) {
  return `<div><label class="f">Category</label><select id="${p}-l1" onchange="cdCatL1Changed(null, null, '${p}')"></select></div>
  <div><label class="f">Subcategory</label><select id="${p}-l2" onchange="cdCatL2Pick('${p}')"></select></div>
  <div><label class="f">Detail</label><select id="${p}-l3" onchange="cdCatL3Changed('${p}')"></select>
    <input type="text" id="${p}-l3-custom" placeholder="Custom detail" style="display:none;margin-top:6px"></div>`;
}
function cdCatFill(p, l1v, l2v, l3v) {
  document.getElementById(p + '-l1').innerHTML = CD.taxonomy.map(t => `<option${t.l1 === l1v ? ' selected' : ''}>${h(t.l1)}</option>`).join('');
  cdCatL1Changed(l2v, l3v, p);
}
function cdCatValue(p) {
  const l3sel = document.getElementById(p + '-l3').value;
  return {cat_l1: document.getElementById(p + '-l1').value, cat_l2: document.getElementById(p + '-l2').value,
          cat_l3: l3sel === '__custom__' ? document.getElementById(p + '-l3-custom').value.trim() : l3sel};
}
async function cdOpenCategory(txnId, preset) {
  const t = preset || (CD.txnCache && CD.txnCache[txnId]);
  let row = t;
  if (!row) {
    const d = await cdApi('/api/cards/transactions?' + cdQS({type: 'all', per_page: 500}, {noDates: true}));
    row = d.transactions.find(x => x.id === txnId);
    if (!row) return;
  }
  CD.catTxn = row;
  const sib = await cdApi(`/api/cards/transactions/${txnId}/merchant-scope`)
    .catch(() => ({count: 1, user_set: 0, keys: [], name: row.canonical_merchant, family: row.merchant_family}));
  document.getElementById('cd-cat-title').textContent = 'Edit ' + (row.canonical_merchant || 'merchant');
  document.getElementById('cd-cat-raw').textContent = 'On the statement: ' + (row.raw_description || '');
  document.getElementById('cd-cat-name').value = row.canonical_merchant || '';
  document.getElementById('cd-cat-family').value = row.merchant_family && row.merchant_family !== row.canonical_merchant ? row.merchant_family : '';
  cdCatFill('cd-cat', row.cat_l1, row.cat_l2, row.cat_l3);
  const others = Math.max(0, (sib.count || 1) - 1);
  document.querySelector(`input[name=cd-scope][value=${others > 0 ? 'merchant' : 'this'}]`).checked = true;
  document.getElementById('cd-scope-merchant-row').style.display = others > 0 ? '' : 'none';
  document.getElementById('cd-scope-merchant-label').textContent =
    `All ${sib.count} transactions from ${row.canonical_merchant}, and future imports` + (sib.user_set ? ` (keeps the ${sib.user_set} you changed on their own)` : '');
  document.getElementById('cd-overwrite-label').textContent = `Also change the ${sib.user_set} you changed on their own`;
  CD.catSiblings = sib;
  document.getElementById('cd-cat-overwrite').checked = false;
  document.getElementById('cd-cat-remember').checked = !!sib.remembered;
  document.getElementById('cd-cat-err').textContent = '';
  cdCatScopeChanged();
  document.getElementById('cd-cat-modal').classList.remove('hidden');
}
function cdCatL1Changed(l2pref, l3pref, p = 'cd-cat') {
  const l1 = document.getElementById(p + '-l1').value;
  const node = CD.taxonomy.find(t => t.l1 === l1) || {l2: []};
  const l2 = document.getElementById(p + '-l2');
  l2.innerHTML = node.l2.map(x => `<option${x.name === l2pref ? ' selected' : ''}>${h(x.name)}</option>`).join('') +
    (CD_CHOOSABLE_L1.includes(l1) ? '<option value="__new__">＋ New subcategory…</option>' : '');
  cdCatL2Changed(l3pref, p);
}
function cdCatL2Pick(p) {
  const sel = document.getElementById(p + '-l2');
  if (sel.value !== '__new__') return cdCatL2Changed(null, p);
  sel.selectedIndex = 0;
  cdCatL2Changed(null, p);
  cdOpenCustomCategory(null, {l1: document.getElementById(p + '-l1').value, picker: p});
}
function cdCatL2Changed(l3pref, p = 'cd-cat') {
  const l1 = document.getElementById(p + '-l1').value;
  const l2 = document.getElementById(p + '-l2').value;
  const node = (CD.taxonomy.find(t => t.l1 === l1) || {l2: []}).l2.find(x => x.name === l2) || {l3: []};
  const opts = node.l3.length ? node.l3 : [l2];
  const known = !l3pref || opts.includes(l3pref);
  document.getElementById(p + '-l3').innerHTML = opts.map(x => `<option${x === l3pref ? ' selected' : ''}>${h(x)}</option>`).join('')
    + `<option value="__custom__"${known ? '' : ' selected'}>Other…</option>`;
  document.getElementById(p + '-l3-custom').value = known ? '' : (l3pref || '');
  cdCatL3Changed(p);
}
function cdCatL3Changed(p = 'cd-cat') {
  document.getElementById(p + '-l3-custom').style.display =
    document.getElementById(p + '-l3').value === '__custom__' ? '' : 'none';
}
function cdCatScopeChanged() {
  const merchant = document.querySelector('input[name=cd-scope]:checked').value === 'merchant';
  document.getElementById('cd-overwrite-row').style.display = merchant && CD.catSiblings && CD.catSiblings.user_set ? '' : 'none';
  document.getElementById('cd-remember-row').style.display = !merchant && CD.catSiblings && CD.catSiblings.keys.length ? '' : 'none';
}
async function cdApplyCategory() {
  const body = {
    name: document.getElementById('cd-cat-name').value.trim(),
    family: document.getElementById('cd-cat-family').value.trim(),
    ...cdCatValue('cd-cat'),
    scope: document.querySelector('input[name=cd-scope]:checked').value,
    remember: document.getElementById('cd-cat-remember').checked,
    overwrite_user: document.getElementById('cd-cat-overwrite').checked,
  };
  try {
    if (!body.name) throw new Error('Give the merchant a name.');
    await cdApi(`/api/cards/transactions/${CD.catTxn.id || CD.catTxn.txn_id}/edit`, cdJson('POST', body));
    if (CD.merchantDetail && body.scope === 'merchant') CD.merchantDetail = body.name;
    cdCloseModal('cd-cat-modal');
    cdRefresh();
    if (typeof CD.onEdited === 'function') CD.onEdited();      // opened from Cash Flow Analysis
  } catch (e) { document.getElementById('cd-cat-err').textContent = e.message; }
}

function cdOpenTxn(id) {
  const t = CD.txnCache && CD.txnCache[id];
  if (!t) return;
  CD.editTxn = t;
  document.getElementById('cd-txn-raw').textContent = `${cfFmtDate(t.date)} · ${t.raw_description} · ${fmtFull(t.amount)}`;
  document.getElementById('cd-txn-type').value = t.txn_type;
  document.getElementById('cd-txn-notes').value = t.notes || '';
  document.getElementById('cd-txn-err').textContent = '';
  document.getElementById('cd-txn-modal').classList.remove('hidden');
}
async function cdSaveTxn() {
  const t = CD.editTxn;
  const body = {notes: document.getElementById('cd-txn-notes').value};
  const ty = document.getElementById('cd-txn-type').value;
  if (ty !== t.txn_type) body.txn_type = ty;
  try {
    await cdApi(`/api/cards/transactions/${t.id}`, cdJson('PATCH', body));
    cdCloseModal('cd-txn-modal');
    cdRefresh();
  } catch (e) { document.getElementById('cd-txn-err').textContent = e.message; }
}

// ── Overview ──────────────────────────────────────────────────────────────────
function cdTile(label, value, sub = '') {
  return `<div class="cd-tile"><div class="lbl">${label}</div><div class="val">${value}</div><div class="sub">${sub}</div></div>`;
}

async function cdRenderOverview(panel) {
  const [s, c] = await Promise.all([
    cdApi('/api/cards/analytics/summary?' + cdQS()),
    cdApi('/api/cards/analytics/categories?' + cdQS({level: 'l1'})),
  ]);
  const o = s.overall;
  const cp = s.common_period;
  panel.innerHTML = `
    <div class="cd-tiles">
      ${cdTile('Net spend', money0(o.net_spend), `gross ${money0(o.gross_purchases)} · refunds ${money0(o.refunds)}`)}
      ${cdTile('Monthly equivalent', money0(o.monthly_equiv_30_4d), '30.4-day month, extrapolated')}
      ${cdTile('Purchases', (o.purchase_count || 0).toLocaleString(), `median ${fmtFull(o.median_purchase)} · ${o.canonical_merchants} merchants`)}
      ${cdTile('Core recurring / mo', money0(s.recurring.core_monthly_recurring), 'subscriptions, bills, services')}
      ${cdTile('Needs review', pct(o.unclassified_share), `${money0(o.unclassified_spend)} uncategorized`)}
    </div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">Spend by month and category</div>
        <div class="cd-card-b"><div class="chart-wrap" style="height:260px"><canvas id="cd-ch-ov-month"></canvas></div></div></div>
      <div class="cd-card"><div class="cd-card-h">Categories</div>
        <div class="cd-card-b" style="display:flex;gap:12px;align-items:center">
          <div class="chart-wrap" style="height:240px;flex:1"><canvas id="cd-ch-ov-l1"></canvas></div>
          <div id="cd-ov-legend" style="flex:1;font-size:12px"></div></div></div>
    </div>
    <div class="cd-card"><div class="cd-card-h"><span class="grow">Coverage</span>
      <span style="text-transform:none;letter-spacing:0;font-weight:400">${cp.exists ? `Common period ${cfFmtDate(cp.start)} → ${cfFmtDate(cp.end)} (${cp.days} days)` : 'No common period across the selected cards'}${s.common_period_applied ? ' · applied' : ''}</span></div>
      <div class="cd-scroll"><table class="cd-table"><thead><tr><th>Card</th><th>First</th><th>Last</th><th class="num">Days</th>
        <th class="num">Net spend</th><th class="num">Monthly equiv.</th><th class="num">Common-period share</th></tr></thead><tbody>
        ${s.coverage.map(r => `<tr><td>${h(r.card)}</td><td>${cfFmtDate(r.first)}</td><td>${cfFmtDate(r.last)}</td>
          <td class="num">${r.covered_days}</td><td class="num">${fmtFull(r.net_spend)}</td>
          <td class="num">${fmtFull(r.monthly_equiv_30_4d)}</td><td class="num">${pct(r.common_period_share)}</td></tr>`).join('')}
      </tbody></table></div></div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">Sensitivity (run rates are extrapolations)</div><div class="cd-scroll">
        <table class="cd-table"><thead><tr><th>Perspective</th><th>Excluding</th><th class="num">Net</th><th class="num">Monthly equiv.</th></tr></thead><tbody>
        ${s.sensitivity.map(r => `<tr><td>${h(r.perspective)}</td><td>${h(r.exclusion)}</td><td class="num">${fmtFull(r.net_spend)}</td><td class="num">${fmtFull(r.monthly_equiv_30_4d)}</td></tr>`).join('')}
        </tbody></table></div></div>
      <div class="cd-card"><div class="cd-card-h">Purchase sizes (full period)</div><div class="cd-scroll">
        <table class="cd-table"><thead><tr><th>Size</th><th class="num">Count</th><th class="num">% count</th><th class="num">Gross</th><th class="num">% gross</th></tr></thead><tbody>
        ${s.size_buckets.filter(b => b.perspective === 'Full history').map(b => `<tr><td>${h(b.bucket)}</td><td class="num">${b.txns}</td><td class="num">${pct(b.pct_txns)}</td><td class="num">${fmtFull(b.gross)}</td><td class="num">${pct(b.pct_gross)}</td></tr>`).join('')}
        </tbody></table></div></div>
    </div>
    <div class="cd-card"><div class="cd-card-h">Not counted as spend</div><div class="cd-card-b cd-note">
      ${Object.keys(o.excluded_by_type).length ? Object.entries(o.excluded_by_type).map(([k, v]) =>
        `${h(k.replace(/_/g, ' '))}: ${v.count} row${v.count === 1 ? '' : 's'}, ${fmtFull(v.amount)}`).join(' · ')
        : 'Nothing excluded in this period.'}
      ${o.card_fees ? ` · Card fees counted in spend: ${fmtFull(o.card_fees)}` : ''}</div></div>`;

  const months = c.months;
  destroyChart('cd-ch-ov-month');
  charts['cd-ch-ov-month'] = new Chart(document.getElementById('cd-ch-ov-month'), {
    type: 'bar',
    data: {labels: months.map(cdMonthLabel), datasets: c.l1_by_month.map(r => ({
      label: r.key[0], data: r.values, backgroundColor: CD_L1_COLORS[r.key[0]] || '#9CA3AF', stack: 's'}))},
    options: cdStackOpts(),
  });
  const rows = c.rows.filter(r => r.net_spend > 0);
  destroyChart('cd-ch-ov-l1');
  charts['cd-ch-ov-l1'] = new Chart(document.getElementById('cd-ch-ov-l1'), {
    type: 'doughnut',
    data: {labels: rows.map(r => r.key[0]), datasets: [{data: rows.map(r => r.net_spend),
      backgroundColor: rows.map(r => CD_L1_COLORS[r.key[0]] || '#9CA3AF'), borderWidth: 0}]},
    options: {responsive: true, maintainAspectRatio: false, cutout: '60%', plugins: {legend: {display: false},
      tooltip: {callbacks: {label: ctx => ' ' + ctx.label + ': ' + fmtFull(ctx.parsed)}}},
      onClick: (_e, els) => { if (els.length) cdShowCategory(rows[els[0].index].key[0]); }},
  });
  document.getElementById('cd-ov-legend').innerHTML = rows.slice(0, 10).map(r => `<div class="legend-item" style="margin:3px 0;cursor:pointer" onclick="cdShowCategory(${js(r.key[0])})">
    <span class="legend-dot" style="background:${CD_L1_COLORS[r.key[0]] || '#9CA3AF'}"></span>${h(r.key[0])} · ${pct(r.share_of_net)}</div>`).join('');
}

function cdMonthLabel(m) {
  const d = new Date(m + '-01T00:00:00');
  return d.toLocaleDateString('en-US', {month: 'short', year: '2-digit'});
}
function cdStackOpts() {
  return {
    responsive: true, maintainAspectRatio: false,
    plugins: {legend: {display: false}, tooltip: {callbacks: {label: ctx => ' ' + ctx.dataset.label + ': ' + fmtFull(ctx.parsed.y)}}},
    scales: {x: {stacked: true, grid: {color: GRID}, ticks: {color: TICK, maxRotation: 0}},
      y: {stacked: true, grid: {color: GRID}, ticks: {color: TICK, callback: v => fmt(v)}}},
  };
}

// ── Categories ────────────────────────────────────────────────────────────────
async function cdRenderCategories(panel) {
  if (CD.catDetail) return cdRenderCategoryDetail(panel, CD.catDetail);
  const c = await cdApi('/api/cards/analytics/categories?' + cdQS({level: CD.catLevel}));
  const lvl = CD.catLevel;
  const name = r => r.key.join(' › ');
  const top = c.rows.filter(r => r.net_spend > 0).slice(0, 15);
  panel.innerHTML = `
    <div class="cd-toolbar"><div class="toggle-group">
      ${['l1', 'l2', 'l3'].map(l => `<button class="toggle-btn${l === lvl ? ' active' : ''}" onclick="CD.catLevel='${l}';cdRefresh()">${{l1: 'Category', l2: 'Subcategory', l3: 'Detail'}[l]}</button>`).join('')}
    </div><span class="cd-note">Total ${fmtFull(c.total)} · ${lvl === 'l1' ? 'click a category for its breakdown' : 'click a row to see its transactions'}</span></div>
    <div class="cd-card"><div class="cd-card-b"><div class="chart-wrap" style="height:${Math.max(160, top.length * 26)}px"><canvas id="cd-ch-cat"></canvas></div></div></div>
    <div class="cd-card"><div class="cd-scroll"><table class="cd-table"><thead><tr><th>${{l1: 'Category', l2: 'Subcategory', l3: 'Detail'}[lvl]}</th>
      <th class="num">Net spend</th><th class="num">Share</th><th class="num">Purchases</th><th class="num">Average</th><th>By card</th></tr></thead><tbody>
      ${c.rows.map((r, i) => `<tr class="clickable" onclick="cdCatDrill(${i})"><td><span class="legend-dot" style="display:inline-block;margin-right:6px;background:${CD_L1_COLORS[r.key[0]] || '#9CA3AF'}"></span>${h(name(r))}</td>
        <td class="num">${fmtFull(r.net_spend)}</td><td class="num">${pct(r.share_of_net)}</td><td class="num">${r.purchases}</td>
        <td class="num">${fmtFull(r.avg_purchase)}</td><td class="cd-note">${h(Object.entries(r.by_card).map(([k, v]) => k + ' ' + money0(v)).join('; '))}</td></tr>`).join('')}
    </tbody></table></div></div>
    <div class="cd-card"><div class="cd-card-h">Fixed vs discretionary (inferred)</div><div class="cd-scroll"><table class="cd-table">
      <thead><tr><th>Group</th><th class="num">Net spend</th><th class="num">Share</th><th class="num">Rows</th></tr></thead><tbody>
      ${c.fixed_vs_discretionary_INFERRED.map(r => `<tr><td>${h(r.group)}</td><td class="num">${fmtFull(r.net_spend)}</td><td class="num">${pct(r.share)}</td><td class="num">${r.count}</td></tr>`).join('')}
    </tbody></table></div><div class="cd-card-b cd-note">Inferred from the subcategory and whether the charge recurs; treat it as a guide, not a fact.</div></div>`;
  CD.catRows = c.rows;
  destroyChart('cd-ch-cat');
  charts['cd-ch-cat'] = new Chart(document.getElementById('cd-ch-cat'), {
    type: 'bar',
    data: {labels: top.map(name), datasets: [{data: top.map(r => r.net_spend), borderRadius: 4,
      backgroundColor: top.map(r => CD_L1_COLORS[r.key[0]] || '#9CA3AF')}]},
    options: {indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {legend: {display: false}, tooltip: {callbacks: {label: ctx => ' ' + fmtFull(ctx.parsed.x)}}},
      scales: {x: {grid: {color: GRID}, ticks: {color: TICK, callback: v => fmt(v)}}, y: {grid: {display: false}, ticks: {color: TICK}}},
      onClick: (_e, els) => { if (els.length) cdCatDrill(c.rows.indexOf(top[els[0].index])); }},
  });
}
function cdCatDrill(i) {
  const r = CD.catRows[i];
  if (!r) return;
  if (r.key.length === 1) { cdShowCategory(r.key[0]); return; }
  const f = {l1: r.key[0]};
  if (r.key.length > 1) f.l2 = r.key[1];
  if (r.key.length > 2) f.q = r.key[2];
  cdDrill(f);
}

// ── Monthly ───────────────────────────────────────────────────────────────────
async function cdRenderMonthly(panel) {
  const m = await cdApi('/api/cards/analytics/monthly?' + cdQS());
  const cards = [...new Set(m.months.flatMap(x => Object.keys(x.by_card)))];
  const hl = m.highlights;
  // Highest and lowest rank every complete month; medians use only months every card covers.
  const nComp = hl ? hl.comparable_months.length : 0;
  const compNote = nComp ? `of ${nComp} month${nComp === 1 ? '' : 's'} every card covers` : 'needs a month every card covers';
  const ranked = hl ? `of ${hl.ranked_months} complete month${hl.ranked_months === 1 ? '' : 's'}` : '';
  const partial = m.months.filter(x => x.partial_month).length;
  const done = m.months.filter(x => !x.partial_month);
  const span = done.length > 1 ? `${cdMonthLabel(done[0].month)} to ${cdMonthLabel(done[done.length - 1].month)}`
    : done.length ? cdMonthLabel(done[0].month) : '';
  const med = v => v === null ? '—' : money0(v);
  panel.innerHTML = `
    ${hl ? `<div class="cd-tiles">${cdTile('Highest month', cdMonthLabel(hl.highest), `${money0(hl.highest_net)}, ${ranked}`)}
      ${cdTile('Lowest month', cdMonthLabel(hl.lowest), `${money0(hl.lowest_net)}, ${ranked}`)}
      ${cdTile('Median month', med(hl.median_net), compNote)}
      ${cdTile('Median ex-large', med(hl.median_net_ex_large), `purchases under ${money0(m.large_threshold)}`)}
      ${cdTile('Complete months', hl.ranked_months, span)}</div>` :
      `<p class="cd-note" style="margin-bottom:12px">The data does not span a complete month yet, so months are not ranked.</p>`}
    <div class="cd-card"><div class="cd-card-h">Net spend by month and card</div><div class="cd-card-b">
      <div class="chart-wrap" style="height:260px"><canvas id="cd-ch-month"></canvas></div>
      ${partial ? `<p class="cd-note" style="margin-top:8px">Faded months are only partly inside the imported dates (such as the month in progress), so they are not ranked.</p>` : ''}</div></div>
    <div class="cd-card"><div class="cd-scroll"><table class="cd-table"><thead><tr><th>Month</th><th class="num">Gross</th>
      <th class="num">Refunds</th><th class="num">Net</th><th class="num">Ex-large</th><th class="num">Purchases</th>
      <th>Top categories</th><th>Largest</th><th>Flags</th></tr></thead><tbody>
      ${m.months.slice().reverse().map(x => `<tr><td>${cdMonthLabel(x.month)}</td><td class="num">${fmtFull(x.gross)}</td>
        <td class="num">${fmtFull(x.refunds_credits)}</td><td class="num"><strong>${fmtFull(x.net)}</strong></td>
        <td class="num">${fmtFull(x.net_ex_large)}</td><td class="num">${x.purchases}</td>
        <td class="cd-note">${h(x.top_categories.map(t => t.name + ' ' + money0(t.amount)).join('; '))}</td>
        <td class="cd-note">${h(x.largest.map(t => t.name + ' ' + money0(t.amount)).join('; '))}</td>
        <td class="cd-note">${x.partial_month ? 'partial month' : ''}${!x.all_cards_cover && !x.partial_month ? 'not every card covers this month' : ''}</td></tr>`).join('')}
    </tbody></table></div></div>`;
  destroyChart('cd-ch-month');
  charts['cd-ch-month'] = new Chart(document.getElementById('cd-ch-month'), {
    type: 'bar',
    data: {labels: m.months.map(x => cdMonthLabel(x.month)), datasets: cards.map((c, i) => ({
      label: c, data: m.months.map(x => x.by_card[c] || 0), stack: 's',
      backgroundColor: m.months.map(x => CD_CARD_COLORS[i % CD_CARD_COLORS.length] + (x.partial_month ? '4D' : ''))}))},
    options: cdStackOpts(),
  });
}

// ── Merchants ─────────────────────────────────────────────────────────────────
async function cdRenderMerchants(panel) {
  if (CD.merchantDetail) return cdRenderMerchantPage(panel, CD.merchantDetail);
  const m = await cdApi('/api/cards/analytics/merchants?' + cdQS());
  const c = m.concentration;
  CD.merchRows = m.merchants;
  const list = (rows, cols) => rows.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr>${cols.map(c => `<th class="${c[2] || ''}">${c[0]}</th>`).join('')}</tr></thead><tbody>
    ${rows.map(r => `<tr class="clickable" onclick="cdShowMerchant(${js(r.merchant)})">${cols.map(c => `<td class="${c[2] || ''}">${c[1](r)}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div>` : '<div class="cd-card-b cd-note">None in this period.</div>';
  panel.innerHTML = `
    <div class="cd-tiles">${cdTile('Merchants', m.merchant_count, '')}${cdTile('Top 5 share', pct(c.top_5_share), '')}
      ${cdTile('Top 10 share', pct(c.top_10_share), '')}${cdTile('Top 20 share', pct(c.top_20_share), '')}
      ${cdTile('Families', m.families.length, 'brands grouped by parent')}</div>
    <div class="cd-card"><div class="cd-card-h">Top merchants (click one for its page)</div>
      ${list(m.merchants.slice(0, 100), [['#', r => r.rank_by_net, 'num'], ['Merchant', r => h(r.merchant) + (r.family !== r.merchant ? `<div class="raw">${h(r.family)}</div>` : '')],
        ['Category', r => h(r.l1 + ' › ' + r.l2)], ['Net', r => fmtFull(r.net_spend), 'num'], ['Share', r => pct(r.share_of_net), 'num'],
        ['Purchases', r => r.purchases, 'num'], ['Average', r => fmtFull(r.avg_purchase), 'num'], ['Cards', r => h(r.cards.join(', '))],
        ['Refund ratio', r => r.refund_ratio == null ? '' : pct(r.refund_ratio), 'num']])}</div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">Frequent small purchases (8+, under $25 average)</div>
        ${list(m.high_frequency_low_ticket, [['Merchant', r => h(r.merchant)], ['Purchases', r => r.purchases, 'num'], ['Average', r => fmtFull(r.avg_purchase), 'num'], ['Total', r => fmtFull(r.net_spend), 'num']])}</div>
      <div class="cd-card"><div class="cd-card-h">High-refund merchants (20%+ refunded)</div>
        ${list(m.high_refund, [['Merchant', r => h(r.merchant)], ['Gross', r => fmtFull(r.gross_purchases), 'num'], ['Refunds', r => fmtFull(r.refunds_credits), 'num'], ['Ratio', r => pct(r.refund_ratio), 'num']])}</div>
    </div>
    <div class="cd-card"><div class="cd-card-h">Merchant families</div><div class="cd-scroll"><table class="cd-table">
      <thead><tr><th>Family</th><th class="num">Net</th><th class="num">Rows</th></tr></thead><tbody>
      ${m.families.slice(0, 40).map(f => `<tr><td>${h(f.family)}</td><td class="num">${fmtFull(f.net_spend)}</td><td class="num">${f.count}</td></tr>`).join('')}
    </tbody></table></div></div>`;
}

// ── Merchant page ─────────────────────────────────────────────────────────────
function cdMerchantLink(name) {
  return `<a href="#" class="cd-mlink" onclick="event.preventDefault();event.stopPropagation();cdShowMerchant(${js(name)})">${h(name)}</a>`;
}
function cdShowMerchant(name) {
  if (!name) return;
  CD.merchantDetail = name;
  if (CD.tab === 'merchants') cdRefresh(); else cdTab('merchants');
}

async function cdRenderMerchantPage(panel, name) {
  let d;
  try { d = await cdApi('/api/cards/merchant?' + cdQS({name})); }
  catch (e) { CD.merchantDetail = null; return cdRenderMerchants(panel); }
  const color = CD_L1_COLORS[d.cat_l1] || '#9CA3AF';
  CD.txnCache = Object.assign(CD.txnCache || {}, Object.fromEntries(d.transactions.map(t => [t.id, t])));
  const src = s => s === 'user' ? 'your choice' : s === 'ai' ? 'AI' : s ? s : 'rules';
  panel.innerHTML = `
    <div class="cd-toolbar"><button class="btn-sm" onclick="CD.merchantDetail=null;cdRefresh()">← All merchants</button>
      <h3 style="font-size:16px">${h(d.name)}</h3>${d.family !== d.name ? `<span class="cd-note">part of ${h(d.family)}</span>` : ''}
      ${d.cat_l1 ? cdChip({cat_l1: d.cat_l1, cat_l2: d.cat_l2, cat_l3: d.cat_l3}, false) : ''}</div>
    <div class="cd-tiles">${cdTile('Net spend', money0(d.net_spend), pct(d.share_of_all) + ' of all card spend')}
      ${cdTile('Per month', money0(d.monthly_average), `average over ${d.by_month.length} month${d.by_month.length === 1 ? '' : 's'}`)}
      ${cdTile('Purchases', d.purchases, d.purchases ? 'average ' + fmtFull(d.avg_purchase) : '')}
      ${cdTile('First and last', d.first ? cfFmtDate(d.first) : '—', d.last ? 'last ' + cfFmtDate(d.last) : '')}
      ${cdTile('Cards', Object.keys(d.by_card).length, h(Object.keys(d.by_card).join(', ')))}</div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">Edit name and category</div><div class="cd-card-b cd-mp-form">
        <div><label class="f">Merchant name</label><input type="text" id="cd-mp-name" maxlength="120" value="${h(d.name)}"></div>
        <div><label class="f">Parent brand (optional)</label><input type="text" id="cd-mp-family" maxlength="120" value="${d.family !== d.name ? h(d.family) : ''}"></div>
        ${cdCatFields('cd-mp')}
        <div class="cd-note">Applies to all ${d.count} purchases and refunds from ${h(d.name)} and to future imports.
          ${d.user_set ? `<label style="display:flex;gap:6px;margin-top:4px"><input type="checkbox" id="cd-mp-overwrite"> Also change the ${d.user_set} you changed on their own</label>` : ''}</div>
        <div id="cd-mp-err" class="err"></div>
        <div><button class="btn-primary btn-sm" onclick="cdSaveMerchant(${js(d.name)})">Save</button></div>
      </div></div>
      <div class="cd-card"><div class="cd-card-h">By month</div><div class="cd-card-b"><div class="chart-wrap" style="height:220px"><canvas id="cd-ch-merch"></canvas></div></div></div>
    </div>
    ${cdRulesCard('cd-ar', {rules: d.amount_rules, scope: {merchant: d.name}, base: [d.cat_l1, d.cat_l2, d.cat_l3],
      reach: d.category_rules, intro: 'For a merchant that sells more than one kind of thing, such as fuel and snacks: give charges in an amount range their own category. You can add several ranges.'})}
    <div class="cd-card"><div class="cd-card-h"><span class="grow">Recurring</span>
      <button class="btn-sm" onclick="cdOpenManual(null, ${js(d.name)})">+ Add recurring</button></div>
      ${d.recurring.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr>
      <th>Series</th><th>Card</th><th>Kind</th><th>Cadence</th><th class="num">Typical</th><th class="num">Monthly ≈</th><th>Status</th><th>Notes</th><th></th></tr></thead><tbody>
      ${d.recurring.map(it => `<tr><td>${it.manual_id ? h(it.label) + ' <span class="cd-mark user">added</span>' : h(it.merchant)}</td><td class="cd-muted">${h(it.card)}</td><td>${h(it.kind)}${it.verdict === 'confirmed' && !it.manual_id ? ' <span class="cd-mark user">confirmed</span>' : ''}</td>
        <td>${h(it.cadence)}</td><td class="num">${fmtFull(it.typical_amount)}</td><td class="num">${it.status === 'ended' ? '—' : fmtFull(it.monthly_equivalent)}</td>
        <td>${h(it.status === 'expected' ? 'not seen yet' : it.status)}</td><td class="cd-note">${h([it.change_detected, it.notes].filter(Boolean).join('; '))}</td>
        <td style="white-space:nowrap">${cdRecurringActions(it)}</td></tr>`).join('')}
      </tbody></table></div>` : `<div class="cd-card-b cd-note">Nothing recurring found for ${h(d.name)}. If part of it is a subscription (billed under this name), add it here.</div>`}</div>
    <div class="cd-card"><div class="cd-card-h">How it was recognised</div><div class="cd-scroll"><table class="cd-table"><thead><tr>
      <th>Statement spelling</th><th class="num">Rows</th><th>Category from</th><th>Sent to AI</th></tr></thead><tbody>
      ${d.spellings.map(sp => `<tr><td>${h(sp.label)}<div class="raw">${h(sp.example)}</div></td><td class="num">${sp.count}</td>
        <td>${h(src(sp.source))}${sp.source === 'ai' && sp.confidence != null ? ` (${Math.round(sp.confidence * 100)}% sure)` : ''}</td>
        <td class="cd-note">${sp.sent_label ? `“${h(sp.sent_label)}” to ${h(sp.model || 'the AI')}${sp.source === 'user' ? '; your choice now overrides its answer' : ''}. Request and reply are in the AI privacy log (Settings).` : 'Nothing sent'}</td></tr>`).join('')
        || '<tr><td colspan="4" class="cd-muted" style="padding:12px">No statement spelling recorded.</td></tr>'}
      </tbody></table></div></div>
    <div class="cd-card"><div class="cd-card-h"><span class="grow">Transactions${d.transactions_total > d.transactions.length ? ` (newest ${d.transactions.length} of ${d.transactions_total})` : ''}</span>
      <span style="text-transform:none;letter-spacing:0;font-weight:400">Click a category to change one transaction</span></div>
      <div class="cd-scroll"><table class="cd-table stack"><thead><tr><th style="width:90px">Date</th><th>Description</th><th>Category</th>
        <th class="num" style="width:110px">Amount</th><th style="width:150px">Card</th><th style="width:40px"></th></tr></thead><tbody>
      ${d.transactions.map(t => `<tr><td data-col="date">${cfFmtDate(t.date)}</td>
        <td data-col="merchant">${t.canonical_merchant !== d.name ? h(t.canonical_merchant) + ' ' : ''}${cdMarks(t)}<div class="raw">${h(t.raw_description)}</div></td>
        <td data-col="category">${cdChip(t)}</td><td data-col="amount" class="num">${cdAmt(t.amount)}</td>
        <td data-col="card" class="cd-muted">${h(t.card)}</td>
        <td data-col="actions"><button class="btn-sm" title="Type and notes" onclick="cdOpenTxn(${t.id})">⋯</button></td></tr>`).join('')
        || '<tr><td colspan="6" class="cd-muted" style="padding:16px">None in the period selected above.</td></tr>'}
      </tbody></table></div></div>`;
  cdCatFill('cd-mp', d.cat_l1, d.cat_l2, d.cat_l3);
  CD.merchantPage = d;
  destroyChart('cd-ch-merch');
  charts['cd-ch-merch'] = new Chart(document.getElementById('cd-ch-merch'), {
    type: 'bar',
    data: {labels: d.by_month.map(x => cdMonthLabel(x.month)), datasets: [{label: d.name, data: d.by_month.map(x => x.net), backgroundColor: color, borderRadius: 4}]},
    options: {responsive: true, maintainAspectRatio: false,
      plugins: {legend: {display: false}, tooltip: {callbacks: {label: ctx => ' ' + fmtFull(ctx.parsed.y)}}},
      scales: {x: {grid: {display: false}, ticks: {color: TICK, maxRotation: 0}}, y: {grid: {color: GRID}, ticks: {color: TICK, callback: v => fmt(v)}}}},
  });
}

function cdRangeLabel(lo, hi) {
  if (lo == null) return `Under ${fmtFull(hi)}`;
  if (hi == null) return `${fmtFull(lo)} and over`;
  return `${fmtFull(lo)} to under ${fmtFull(hi)}`;
}
// Amount rules: one card used on merchant pages (scope: the merchant) and category pages
// (scope: a subcategory, or the whole category). CD.ruleCtx[prefix] holds each card's context.
function cdRulesCard(p, ctx) {
  CD.ruleCtx = CD.ruleCtx || {};
  CD.ruleCtx[p] = ctx;
  const cat = !!ctx.l1;
  const scopeLabel = r => r.scope_l2 ? r.scope_l2 : `All of ${r.scope_l1}`;
  const reach = (ctx.reach || []).filter(r => r.scope_l1);
  return `<div class="cd-card"><div class="cd-card-h"><span class="grow">Categories by amount${cat ? ' (every merchant)' : ''}</span>
      <button class="btn-sm" onclick="cdOpenAmountRule('${p}', null)">+ Add rule</button></div>
    ${ctx.rules.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr>${cat ? '<th>Applies to</th>' : ''}<th>Amount</th><th>Category</th>
      <th class="num">Charges</th><th class="num">Total</th><th></th></tr></thead><tbody>
      ${ctx.rules.map(r => `<tr>${cat ? `<td>${h(scopeLabel(r))}</td>` : ''}<td>${cdRangeLabel(r.amount_min, r.amount_max)}</td><td>${cdChip(r, false)}</td>
        <td class="num">${r.matches}${cat && r.merchants ? ` <span class="cd-muted">at ${r.merchants} merchant${r.merchants === 1 ? '' : 's'}</span>` : ''}${r.kept_user_overrides ? ` <span class="cd-muted" title="Changed on their own; the rule leaves them">(+${r.kept_user_overrides} kept)</span>` : ''}</td>
        <td class="num">${fmtFull(r.total)}</td>
        <td style="white-space:nowrap"><button class="btn-sm" onclick="cdOpenAmountRule('${p}', ${r.id})">Edit</button>
          <button class="btn-sm" onclick="cdDeleteAmountRule(${r.id})">Delete</button></td></tr>`).join('')}
      </tbody></table></div>` : ''}
    <div class="cd-card-b">
      <div class="cd-note">${ctx.rules.length ? (cat ? 'Merchants keep their own category outside these ranges; a merchant\'s own rules come first.' : 'Charges outside every rule use the category above.') : h(ctx.intro)}
        Charges you changed on their own are kept, and future imports follow the rules.</div>
      ${reach.length ? `<div class="cd-note" style="margin-top:6px">Also applies here, from ${h(reach[0].scope_l1)}: ${reach.map(r =>
        `${h(scopeLabel(r))} ${cdRangeLabel(r.amount_min, r.amount_max).toLowerCase()} → ${h(r.cat_l2 + ' › ' + r.cat_l3)}`).join('; ')}
        (<a href="#" onclick="event.preventDefault();cdShowCategory(${js(reach[0].scope_l1)})">edit</a>). This merchant's own rules come first.</div>` : ''}
      <div id="${p}-form" class="cd-mp-form" style="display:none;margin-top:10px">
        ${cat ? `<div><label class="f">Applies to</label><select id="${p}-scope" onchange="cdPreviewAmountRule('${p}')">
          ${ctx.l2s.map(x => `<option value="${h(x)}">${h(x)}</option>`).join('')}<option value="">All of ${h(ctx.l1)}</option></select></div>` : ''}
        <div style="display:flex;gap:8px"><div style="flex:1"><label class="f">From $ (empty = no minimum)</label><input type="text" id="${p}-min" inputmode="decimal" oninput="cdPreviewAmountRule('${p}')"></div>
          <div style="flex:1"><label class="f">Up to, not including $ (empty = no maximum)</label><input type="text" id="${p}-max" inputmode="decimal" oninput="cdPreviewAmountRule('${p}')"></div></div>
        ${cdCatFields(p)}
        <div id="${p}-preview" class="cd-note"></div>
        <div id="${p}-err" class="err"></div>
        <div style="display:flex;gap:6px"><button class="btn-primary btn-sm" onclick="cdSaveAmountRule('${p}')">Save rule</button>
          <button class="btn-sm" onclick="document.getElementById('${p}-form').style.display='none'">Cancel</button></div>
      </div>
    </div></div>`;
}
function cdArBody(p) {
  const ctx = CD.ruleCtx[p];
  const num = id => { const v = document.getElementById(id).value.replace(/[$,\s]/g, ''); return v === '' ? null : Number(v); };
  const scope = ctx.l1 ? {scope_l1: ctx.l1, scope_l2: document.getElementById(p + '-scope').value} : {merchant: ctx.scope.merchant};
  return {...scope, amount_min: num(p + '-min'), amount_max: num(p + '-max'), ...cdCatValue(p)};
}
function cdOpenAmountRule(p, id) {
  const ctx = CD.ruleCtx[p];
  const r = id ? ctx.rules.find(x => x.id === id) : null;
  ctx.editing = r ? r.id : null;
  document.getElementById(p + '-min').value = r && r.amount_min != null ? r.amount_min.toFixed(2) : '';
  document.getElementById(p + '-max').value = r && r.amount_max != null ? r.amount_max.toFixed(2) : '';
  if (ctx.l1) document.getElementById(p + '-scope').value = r ? r.scope_l2 : (ctx.l2s[0] || '');
  const b = r ? [r.cat_l1, r.cat_l2, r.cat_l3] : ctx.base;
  cdCatFill(p, b[0], b[1], b[2]);
  document.getElementById(p + '-err').textContent = '';
  document.getElementById(p + '-form').style.display = '';
  cdPreviewAmountRule(p);
  document.getElementById(p + '-min').focus();
}
let _cdArTimer = null;
function cdPreviewAmountRule(p) {
  clearTimeout(_cdArTimer);
  _cdArTimer = setTimeout(async () => {
    const ctx = CD.ruleCtx[p];
    const out = document.getElementById(p + '-preview');
    const body = cdArBody(p);
    if (body.amount_min == null && body.amount_max == null) { out.textContent = 'Enter a lower or upper amount, or both.'; return; }
    const q = await cdApi('/api/cards/amount-rules/preview' + (ctx.editing ? '?rule_id=' + ctx.editing : ''), cdJson('POST', body)).catch(e => ({error: e.message}));
    out.textContent = q.error ? q.error : `${cdRangeLabel(body.amount_min, body.amount_max)}: ${q.matches} of ${q.of} charges (${fmtFull(q.total)})` +
      (ctx.l1 ? ` at ${q.merchants} merchant${q.merchants === 1 ? '' : 's'}` : '') +
      (q.kept_user_overrides ? `, plus ${q.kept_user_overrides} you changed on their own, which stay as they are` : '') +
      (q.examples.length ? `. Recent: ${q.examples.map(fmtFull).join(', ')}` : '') + '.';
  }, 250);
}
async function cdSaveAmountRule(p) {
  const ctx = CD.ruleCtx[p];
  const body = cdArBody(p);
  try {
    if ([body.amount_min, body.amount_max].some(v => v !== null && Number.isNaN(v))) throw new Error('Amounts must be numbers.');
    await cdApi('/api/cards/amount-rules' + (ctx.editing ? '/' + ctx.editing : ''), cdJson(ctx.editing ? 'PUT' : 'POST', body));
    cdRefresh();
  } catch (e) { document.getElementById(p + '-err').textContent = e.message; }
}
async function cdDeleteAmountRule(id) {
  if (!confirm('Delete this rule? Its charges go back to their own category.')) return;
  await cdApi('/api/cards/amount-rules/' + id, {method: 'DELETE'});
  cdRefresh();
}

// ── Your own categories ───────────────────────────────────────────────────────
async function cdReloadTaxonomy() {
  try { CD.taxonomy = (await cdApi('/api/cards/taxonomy')).tree; } catch (e) { /* keep the old list */ }
}
function cdCcKind() {
  const detail = document.querySelector('input[name=cd-cc-kind]:checked').value === 'detail';
  document.getElementById('cd-cc-parent-row').style.display = detail ? '' : 'none';
  document.getElementById('cd-cc-attrs').style.display = detail ? 'none' : '';
  if (detail) {
    const l1 = document.getElementById('cd-cc-l1').value;
    const node = CD.taxonomy.find(t => t.l1 === l1) || {l2: []};
    const sel = document.getElementById('cd-cc-parent');
    const keep = sel.value;
    sel.innerHTML = node.l2.map(x => `<option${x.name === keep ? ' selected' : ''}>${h(x.name)}</option>`).join('');
  }
}
async function cdOpenCustomCategory(id, opts = {}) {
  cdInjectModals();
  if (id && !CD.customCats) CD.customCats = (await cdApi('/api/cards/categories')).custom;
  const c = id ? CD.customCats.find(x => x.id === id) : null;
  CD.ccEdit = c ? c.id : null;
  CD.ccPicker = opts.picker || null;
  document.getElementById('cd-cc-title').textContent = c ? 'Edit ' + (c.l3 || c.l2) : 'New subcategory';
  document.getElementById('cd-cc-kind-row').style.display = c || opts.picker ? 'none' : 'flex';
  document.querySelector(`input[name=cd-cc-kind][value=${c && c.l3 ? 'detail' : 'sub'}]`).checked = true;
  const l1 = c ? c.l1 : (opts.l1 || CD_CHOOSABLE_L1[0]);
  const l1sel = document.getElementById('cd-cc-l1');
  l1sel.innerHTML = CD_CHOOSABLE_L1.map(x => `<option${x === l1 ? ' selected' : ''}>${h(x)}</option>`).join('');
  l1sel.disabled = !!c;
  cdCcKind();
  if (c && c.l3) { document.getElementById('cd-cc-parent').value = c.l2; document.getElementById('cd-cc-parent').disabled = true; }
  else document.getElementById('cd-cc-parent').disabled = false;
  document.getElementById('cd-cc-name').value = c ? (c.l3 || c.l2) : (opts.name || '');
  document.getElementById('cd-cc-fixed').value = c ? c.fixedness : (opts.fixedness || 'discretionary');
  document.getElementById('cd-cc-role').value = c ? c.role : (opts.role || 'none');
  document.getElementById('cd-cc-rewards').value = c ? c.rewards : (opts.rewards || 'general');
  document.getElementById('cd-cc-ai').checked = c ? c.ai_visible : opts.ai_visible !== false;
  document.getElementById('cd-cc-err').textContent = '';
  document.getElementById('cd-cc-modal').classList.remove('hidden');
  document.getElementById('cd-cc-name').focus();
}
async function cdSaveCustomCategory() {
  const g = id => document.getElementById(id);
  const detail = document.querySelector('input[name=cd-cc-kind]:checked').value === 'detail';
  const attrs = {fixedness: g('cd-cc-fixed').value, role: g('cd-cc-role').value, rewards: g('cd-cc-rewards').value, ai_visible: g('cd-cc-ai').checked};
  const name = g('cd-cc-name').value.trim();
  try {
    if (!name) throw new Error('Give it a name.');
    let saved;
    if (CD.ccEdit) saved = await cdApi('/api/cards/categories/' + CD.ccEdit, cdJson('PATCH', {name, ...attrs}));
    else saved = await cdApi('/api/cards/categories', cdJson('POST', detail
      ? {l1: g('cd-cc-l1').value, l2: g('cd-cc-parent').value, l3: name, ...attrs}
      : {l1: g('cd-cc-l1').value, l2: name, ...attrs}));
    cdCloseModal('cd-cc-modal');
    await cdReloadTaxonomy();
    if (CD.ccPicker && !saved.l3) cdCatFill(CD.ccPicker, saved.l1, saved.l2, '');
    else cdRefresh();
  } catch (e) { g('cd-cc-err').textContent = e.message; }
}
function cdDeleteCustomCategory(id) {
  cdInjectModals();
  const c = CD.customCats.find(x => x.id === id);
  CD.dcId = id;
  const name = c.l3 ? `${c.l2} › ${c.l3}` : c.l2;
  document.getElementById('cd-dc-title').textContent = 'Delete ' + name;
  document.getElementById('cd-dc-intro').textContent = c.transactions
    ? `${c.transactions} transaction${c.transactions === 1 ? '' : 's'} (and any merchants and amount rules) use it. Choose where they go:`
    : `Nothing uses it yet. Merchants or rules that do will move to the category below.`;
  const others = (CD.taxonomy.find(t => t.l1 === c.l1) || {l2: []}).l2.filter(x => x.name !== c.l2);
  cdCatFill('cd-dc', others.length ? c.l1 : 'Uncategorized', others.length ? others[0].name : 'Needs review', '');
  document.getElementById('cd-dc-err').textContent = '';
  document.getElementById('cd-dc-modal').classList.remove('hidden');
}
async function cdConfirmDeleteCategory() {
  const v = cdCatValue('cd-dc');
  const q = new URLSearchParams({move_l1: v.cat_l1, move_l2: v.cat_l2, move_l3: v.cat_l3});
  try {
    await cdApi(`/api/cards/categories/${CD.dcId}?${q}`, {method: 'DELETE'});
    cdCloseModal('cd-dc-modal');
    await cdReloadTaxonomy();
    cdRefresh();
  } catch (e) { document.getElementById('cd-dc-err').textContent = e.message; }
}

async function cdSaveMerchant(name) {
  const ow = document.getElementById('cd-mp-overwrite');
  const body = {name, new_name: document.getElementById('cd-mp-name').value.trim(),
    family: document.getElementById('cd-mp-family').value.trim(), ...cdCatValue('cd-mp'), overwrite_user: !!(ow && ow.checked)};
  try {
    if (!body.new_name) throw new Error('Give the merchant a name.');
    const r = await cdApi('/api/cards/merchant/edit', cdJson('POST', body));
    CD.merchantDetail = r.name;
    cdRefresh();
  } catch (e) { document.getElementById('cd-mp-err').textContent = e.message; }
}

// ── Subscriptions ─────────────────────────────────────────────────────────────
async function cdRenderSubscriptions(panel) {
  const r = await cdApi('/api/cards/analytics/recurring?' + cdQS({include_habitual: CD.showHabitual ? 'true' : 'false'}, {noDates: true}));
  const k = r.by_kind;
  const running = r.items.filter(it => it.status !== 'ended');
  const ended = r.items.filter(it => it.status === 'ended');
  panel.innerHTML = `
    <div class="cd-tiles">${cdTile('Core recurring / mo', money0(r.core_monthly_recurring), 'subscriptions + bills + services')}
      ${cdTile('Subscriptions', money0(k.subscription), 'per month')}${cdTile('Bills', money0(k['recurring bill']), 'per month')}
      ${cdTile('Services', money0(k['recurring service']), 'per month')}${cdTile('Installments', money0(k.installment), 'per month, not permanent')}</div>
    <div class="cd-toolbar"><button class="btn-sm" onclick="cdOpenManual()">+ Add recurring</button>
      <label><input type="checkbox" ${CD.showHabitual ? 'checked' : ''} onchange="CD.showHabitual=this.checked;cdRefresh()"> Show habitual merchants (coffee, fuel, groceries)</label>
      <span class="cd-note">Detected from the full history of the selected cards. Confirm real ones, mark cancelled ones Ended, and dismiss false matches as Not recurring.</span></div>
    <div class="cd-card"><div class="cd-scroll"><table class="cd-table"><thead><tr><th>Merchant</th><th>Card</th><th>Kind</th><th>Cadence</th>
      <th class="num">Typical</th><th class="num">Monthly ≈</th><th class="num">Charges</th><th>Last</th><th>Changes &amp; notes</th><th></th></tr></thead><tbody>
      ${running.length ? running.map(it => `<tr>
        <td>${cdRecurringName(it)}${it.status === 'overdue' ? ' <span class="cd-mark fx" title="No charge for over 1.6 billing periods; still counted until about two charges are missed">overdue</span>' : ''}${it.status === 'expected' ? ' <span class="cd-mark fx" title="No charge matches it yet; not counted until one does">not seen yet</span>' : ''}</td>
        <td class="cd-muted">${h(it.card)}</td><td>${h(it.kind)}</td><td>${h(it.cadence)}</td>
        <td class="num">${fmtFull(it.typical_amount)}</td><td class="num">${fmtFull(it.monthly_equivalent)}</td>
        <td class="num">${it.occurrences}</td><td>${it.most_recent_charge ? cfFmtDate(it.most_recent_charge) : '—'}</td>
        <td class="cd-note">${h([it.change_detected, it.notes].filter(Boolean).join('; '))}</td>
        <td style="white-space:nowrap">${cdRecurringActions(it)}</td></tr>`).join('')
        : `<tr><td colspan="10" style="padding:24px;text-align:center" class="cd-muted">${ended.length ? 'Nothing recurring is running right now.' : 'Nothing recurring detected yet. Most patterns need two or three charges.'}</td></tr>`}
    </tbody></table></div></div>
    ${ended.length ? `<div class="cd-card"><div class="cd-card-h"><span class="grow">Ended</span>
      <span style="text-transform:none;letter-spacing:0;font-weight:400">Not counted in the totals above. A new charge brings a series back.</span></div>
      <div class="cd-scroll"><table class="cd-table"><thead><tr><th>Merchant</th><th>Card</th><th>Kind</th><th>Cadence</th>
      <th class="num">Typical</th><th class="num">Charges</th><th>Last</th><th>Why</th><th></th></tr></thead><tbody>
      ${ended.map(it => `<tr><td>${cdRecurringName(it)}</td><td class="cd-muted">${h(it.card)}</td><td>${h(it.kind)}</td><td>${h(it.cadence)}</td>
        <td class="num">${fmtFull(it.typical_amount)}</td><td class="num">${it.occurrences}</td><td>${it.most_recent_charge ? cfFmtDate(it.most_recent_charge) : '—'}</td>
        <td class="cd-note">${it.ended_by === 'you' ? 'you marked it ended' : `no charge in ${it.days_since_last_charge} days (about two missed charges)`}</td>
        <td style="white-space:nowrap">${cdRecurringActions(it)}</td></tr>`).join('')}
      </tbody></table></div></div>` : ''}
    ${r.dismissed.length ? `<div class="cd-card"><div class="cd-card-h">Dismissed</div><div class="cd-card-b">${r.dismissed.map(d =>
      `<button class="btn-sm" style="margin:2px" onclick="cdVerdict(${js(d.merchant)}, ${d.card_account_id}, null)">↺ ${h(d.merchant)} · ${h(d.card)}</button>`).join('')}</div></div>` : ''}`;
}
function cdRecurringName(it) {
  if (it.manual_id) return `${h(it.label)} <span class="cd-mark user" title="Added by you">added</span><div class="raw">billed as ${cdMerchantLink(it.merchant)}${
    it.amount_min != null || it.amount_max != null ? ` · ${it.amount_min != null ? fmtFull(it.amount_min) : 'any'} to ${it.amount_max != null ? fmtFull(it.amount_max) : 'any'}` : ''}</div>`;
  return cdMerchantLink(it.merchant) +
    (it.verdict === 'confirmed' ? ' <span class="cd-mark user">confirmed</span>' : '') +
    (it.moved_from && it.moved_from.length ? ` <span class="cd-mark rec" title="Followed across cards">moved</span>` : '');
}
function cdRecurringActions(it) {
  const m = js(it.merchant), c = it.card_account_id;
  if (it.manual_id) {
    return `<button class="btn-sm" onclick="cdOpenManual(${it.manual_id})">Edit</button>` +
      (it.status === 'ended' && it.ended_by === 'you' ? ` <button class="btn-sm" onclick="cdManualEnded(${it.manual_id}, false)">Still running</button>`
        : it.status !== 'ended' && it.occurrences ? ` <button class="btn-sm" onclick="cdManualEnded(${it.manual_id}, true)">Ended</button>` : '') +
      ` <button class="btn-sm" onclick="cdDeleteManual(${it.manual_id}, ${js(it.label)})">Delete</button>`;
  }
  let out = it.verdict !== 'confirmed' && it.status !== 'ended' ? `<button class="btn-sm" onclick="cdVerdict(${m}, ${c}, 'confirmed')">Confirm</button> ` : '';
  if (it.kind !== 'habitual') {
    out += it.status === 'ended' ? (it.ended_by === 'you' ? `<button class="btn-sm" onclick="cdEnded(${m}, ${c}, false)">Still running</button> ` : '')
      : `<button class="btn-sm" title="Cancelled: stop counting it now. A new charge brings it back." onclick="cdEnded(${m}, ${c}, true)">Ended</button> `;
  }
  return out + `<button class="btn-sm" title="Never was recurring (this card only)" onclick="cdVerdict(${m}, ${c}, 'dismissed')">Not recurring</button>`;
}
function cdTrackRecurring() {
  const t = CD.catTxn;
  cdCloseModal('cd-cat-modal');
  const amt = Math.abs(t.amount);
  // Amount range: the charge give or take 5%, so tax or a small price change still matches.
  cdOpenManual(null, t.canonical_merchant, {name: t.canonical_merchant, card_account_id: t.card_account_id,
    amount_min: Math.floor(amt * 0.95 * 100) / 100, amount_max: Math.ceil(amt * 1.05 * 100) / 100});
}
async function cdOpenManual(id, merchant, preset) {
  cdInjectModals();
  const list = await cdApi('/api/cards/recurring/manual');
  const m = id ? list.find(x => x.id === id) : (preset ? {...preset, merchant, cadence: 'monthly', kind: 'subscription', _new: true} : null);
  CD.manualEdit = m && !m._new ? m.id : null;
  if (!CD.merchantList) { try { CD.merchantList = await cdApi('/api/cards/merchants'); } catch (e) { CD.merchantList = []; } }
  document.getElementById('cd-man-merchants').innerHTML = [...new Set(CD.merchantList.map(x => x.canonical_merchant))]
    .map(n => `<option value="${h(n)}">`).join('');
  document.getElementById('cd-man-title').textContent = CD.manualEdit ? 'Edit recurring charge' : 'Add a recurring charge';
  document.getElementById('cd-man-name').value = m ? m.name : '';
  document.getElementById('cd-man-merchant').value = m ? m.merchant : (merchant || '');
  document.getElementById('cd-man-card').innerHTML = '<option value="">Any card</option>' +
    CD.accounts.map(a => `<option value="${a.id}"${m && m.card_account_id === a.id ? ' selected' : ''}>${h(a.name)}</option>`).join('');
  document.getElementById('cd-man-min').value = m && m.amount_min != null ? m.amount_min.toFixed(2) : '';
  document.getElementById('cd-man-max').value = m && m.amount_max != null ? m.amount_max.toFixed(2) : '';
  document.getElementById('cd-man-cadence').value = m ? m.cadence : 'monthly';
  document.getElementById('cd-man-kind').value = m ? m.kind : 'subscription';
  document.getElementById('cd-man-err').textContent = '';
  document.getElementById('cd-man-modal').classList.remove('hidden');
}
async function cdSaveManual() {
  const num = id => { const v = document.getElementById(id).value.replace(/[$,\s]/g, ''); return v === '' ? null : Number(v); };
  const card = document.getElementById('cd-man-card').value;
  const body = {name: document.getElementById('cd-man-name').value.trim(), merchant: document.getElementById('cd-man-merchant').value.trim(),
    card_account_id: card ? parseInt(card, 10) : null, amount_min: num('cd-man-min'), amount_max: num('cd-man-max'),
    cadence: document.getElementById('cd-man-cadence').value, kind: document.getElementById('cd-man-kind').value};
  try {
    if ([body.amount_min, body.amount_max].some(v => v !== null && Number.isNaN(v))) throw new Error('Amounts must be numbers.');
    await cdApi('/api/cards/recurring/manual' + (CD.manualEdit ? '/' + CD.manualEdit : ''), cdJson(CD.manualEdit ? 'PUT' : 'POST', body));
    cdCloseModal('cd-man-modal');
    cdRefresh();
  } catch (e) { document.getElementById('cd-man-err').textContent = e.message; }
}
async function cdManualEnded(id, ended) {
  await cdApi(`/api/cards/recurring/manual/${id}/ended`, cdJson('POST', {ended}));
  cdRefresh();
}
async function cdDeleteManual(id, name) {
  if (!confirm(`Delete "${name}"? Its charges go back to being judged with the merchant's other charges.`)) return;
  await fetch('/api/cards/recurring/manual/' + id, {method: 'DELETE'});
  cdRefresh();
}
async function cdEnded(merchant, cardId, ended) {
  await cdApi('/api/cards/recurring/ended', cdJson('POST', {merchant, card_account_id: cardId, ended}));
  cdRefresh();
}
async function cdVerdict(merchant, cardId, status) {
  await cdApi('/api/cards/recurring/status', cdJson('POST', {merchant, card_account_id: cardId || null, status}));
  cdRefresh();
}

// ── Review ────────────────────────────────────────────────────────────────────
async function cdRenderReview(panel) {
  const r = await cdApi('/api/cards/analytics/review?' + cdQS());
  CD.txnCache = CD.txnCache || {};
  r.items.forEach(i => { CD.txnCache[i.txn_id] = CD.txnCache[i.txn_id] || {...i, id: i.txn_id, canonical_merchant: i.merchant, amount: -i.amount}; });
  CD.review = r;
  panel.innerHTML = `
    <p class="cd-note" style="margin-bottom:12px">Large purchases, repeated same-dollar charges, foreign transactions and anything the categorizer was unsure of. A repeat is worth a look, not an assumed duplicate.
      Subscription charges are left out (the Subscriptions tab covers them). Tick a row once you've looked at it; it comes back only if something new turns up about it (a later repeat, say).</p>
    <div class="cd-card" id="cd-review-card"></div>
    ${r.refund_matches.length ? `<div class="cd-card"><div class="cd-card-h">Refunds matched to purchases</div><div class="cd-scroll"><table class="cd-table">
      <thead><tr><th>Refund date</th><th>Merchant</th><th class="num">Refund</th><th>Purchase date</th><th class="num">Purchase</th><th>Match</th></tr></thead><tbody>
      ${r.refund_matches.map(m => `<tr><td>${cfFmtDate(m.refund_date)}</td><td>${h(m.merchant)}</td><td class="num">${fmtFull(m.refund)}</td>
        <td>${m.purchase_date ? cfFmtDate(m.purchase_date) : ''}</td><td class="num">${m.purchase_amount != null ? fmtFull(m.purchase_amount) : ''}</td><td class="cd-note">${h(m.match)}</td></tr>`).join('')}
    </tbody></table></div></div>` : ''}`;
  cdRenderReviewList();
}
function cdRenderReviewList() {
  const box = document.getElementById('cd-review-card');
  if (!box || !CD.review) return;
  const all = CD.review.items, done = all.filter(i => i.reviewed_at).length, open = all.length - done;
  const shown = CD.reviewShowDone ? all : all.filter(i => !i.reviewed_at);
  box.innerHTML = `<div class="cd-card-h"><span class="grow">${open} to review${done ? ` · ${done} reviewed` : ''}</span>
      ${done ? `<label class="cd-review-toggle"><input type="checkbox" ${CD.reviewShowDone ? 'checked' : ''} onchange="CD.reviewShowDone=this.checked;cdRenderReviewList()"> Show reviewed</label>` : ''}
      ${open ? `<button class="btn-sm" onclick="cdMarkReviewed(null, true)">Mark all ${open} reviewed</button>` : ''}</div>
    <div class="cd-scroll"><table class="cd-table"><thead><tr><th title="Reviewed">✓</th><th>Priority</th><th>Date</th><th>Merchant</th>
      <th class="num">Amount</th><th>Category</th><th>Why</th></tr></thead><tbody>
      ${shown.length ? shown.map(i => `<tr class="${i.reviewed_at ? 'cd-reviewed' : ''}">
        <td><input type="checkbox" class="cd-review-check" title="${i.reviewed_at ? 'Reviewed ' + new Date(i.reviewed_at).toLocaleDateString() + ': untick to put it back' : 'Mark reviewed'}"
          ${i.reviewed_at ? 'checked' : ''} onchange="cdMarkReviewed(${i.txn_id}, this.checked)"></td>
        <td class="prio-${i.priority}">${i.priority}</td><td>${cfFmtDate(i.date)}</td>
        <td>${h(i.merchant)}<div class="raw">${h(i.raw_description)}</div></td><td class="num">${fmtFull(i.amount)}</td>
        <td>${cdChip({...i, id: i.txn_id})}</td><td class="cd-note">${h(i.why_notable)}${i.potential_repeat ? '<br>' + h(i.potential_repeat) : ''}
          ${i.reviewed_at ? `<div class="cd-muted">Reviewed ${new Date(i.reviewed_at).toLocaleDateString()}</div>` : ''}</td></tr>`).join('')
        : `<tr><td colspan="7" style="padding:24px;text-align:center" class="cd-muted">${all.length ? 'All reviewed. Nothing new to look at.' : 'Nothing to review in this period.'}</td></tr>`}
    </tbody></table></div>`;
}
async function cdMarkReviewed(txnId, reviewed) {
  // txnId null: every item still open in the list.
  const items = CD.review.items.filter(i => txnId == null ? !i.reviewed_at : i.txn_id === txnId);
  if (!items.length) return;
  if (txnId == null && !confirm(`Mark all ${items.length} items reviewed?`)) return;
  try {
    await cdApi('/api/cards/review', cdJson('POST', {reviewed, items: items.map(i => ({txn_id: i.txn_id, codes: i.codes}))}));
    const now = new Date().toISOString();
    items.forEach(i => { i.reviewed_at = reviewed ? now : null; });
  } catch (e) { alert(e.message); }
  cdRenderReviewList();
}

// ── Travel & food ─────────────────────────────────────────────────────────────
function cdShowCategory(l1) {
  CD.catDetail = l1;
  if (CD.tab === 'categories') cdRefresh(); else cdTab('categories');
}

async function cdRenderCategoryDetail(panel, l1) {
  const d = await cdApi('/api/cards/analytics/category?' + cdQS({l1}));
  const color = CD_L1_COLORS[l1] || '#9CA3AF';
  CD.catDetailData = d;
  const subRows = d.subcategories.filter(r => r.net_spend !== 0);
  panel.innerHTML = `
    <div class="cd-toolbar"><button class="btn-sm" onclick="CD.catDetail=null;cdRefresh()">← All categories</button>
      <h3 style="font-size:16px;display:flex;align-items:center;gap:8px"><span class="legend-dot" style="display:inline-block;background:${color}"></span>${h(l1)}</h3>
      <button class="btn-sm" onclick="cdDrill({l1: ${js(l1)}})">All ${d.purchases} transactions →</button></div>
    <div class="cd-tiles">${cdTile('Net spend', money0(d.net_spend), pct(d.share_of_all) + ' of all card spend')}
      ${cdTile('Per month', money0(d.monthly_average), `average over ${d.by_month.length} month${d.by_month.length === 1 ? '' : 's'}`)}
      ${cdTile('Purchases', d.purchases, `average ${fmtFull(d.avg_purchase)}`)}
      ${cdTile('Merchants', d.merchant_count, d.merchants[0] ? 'top: ' + h(d.merchants[0].key[0]) : '')}
      ${cdTile('Refunds & credits', money0(-d.refunds_credits), '')}</div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">By month</div><div class="cd-card-b"><div class="chart-wrap" style="height:220px"><canvas id="cd-ch-catm"></canvas></div></div></div>
      <div class="cd-card"><div class="cd-card-h">Subcategories</div>${subRows.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>Subcategory</th><th class="num">Net</th><th class="num">Share</th><th class="num">Purchases</th></tr></thead><tbody>
        ${subRows.map(r => `<tr class="clickable" onclick="cdDrill({l1: ${js(l1)}, l2: ${js(r.key[0])}})"><td>${h(r.key[0])}</td><td class="num">${fmtFull(r.net_spend)}</td>
          <td class="num">${pct(r.share_of_net)}</td><td class="num">${r.purchases}</td></tr>`).join('')}</tbody></table></div>` : '<div class="cd-card-b cd-note">None in this period.</div>'}</div>
    </div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">Top merchants</div>${d.merchants.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>Merchant</th><th class="num">Net</th><th class="num">Share</th><th class="num">Purchases</th><th class="num">Average</th></tr></thead><tbody>
        ${d.merchants.map(r => `<tr class="clickable" onclick="cdShowMerchant(${js(r.key[0])})"><td>${h(r.key[0])}</td><td class="num">${fmtFull(r.net_spend)}</td>
          <td class="num">${pct(r.share_of_net)}</td><td class="num">${r.purchases}</td><td class="num">${fmtFull(r.avg_purchase)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="cd-card-b cd-note">None.</div>'}</div>
      <div class="cd-card"><div class="cd-card-h">Largest purchases</div>${d.largest.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>Date</th><th>Merchant</th><th>Subcategory</th><th class="num">Amount</th><th>Card</th></tr></thead><tbody>
        ${d.largest.map(x => `<tr class="clickable" onclick="cdShowMerchant(${js(x.merchant)})"><td>${cfFmtDate(x.date)}</td><td>${h(x.merchant)}</td>
          <td class="cd-note">${h(x.l2)}</td><td class="num">${fmtFull(x.amount)}</td><td class="cd-muted">${h(x.card)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="cd-card-b cd-note">None.</div>'}</div>
    </div>
    ${cdRulesCard('cd-cr', {rules: d.amount_rules, l1, l2s: subRows.map(r => r.key[0]),
      base: [l1, (subRows[0] || {key: ['']}).key[0], ''],
      intro: `For merchants in ${l1} that sell more than one kind of thing (gas stations: fuel at the pump, snacks inside): give charges in an amount range their own category, at every merchant in a subcategory.`})}
    ${d.travel ? cdTravelExtras(d.travel) : ''}${d.food ? cdFoodExtras(d.food) : ''}`;
  destroyChart('cd-ch-catm');
  charts['cd-ch-catm'] = new Chart(document.getElementById('cd-ch-catm'), {
    type: 'bar',
    data: {labels: d.by_month.map(x => cdMonthLabel(x.month)), datasets: [{label: l1, data: d.by_month.map(x => x.net), backgroundColor: color, borderRadius: 4}]},
    options: {responsive: true, maintainAspectRatio: false,
      plugins: {legend: {display: false}, tooltip: {callbacks: {label: ctx => ' ' + fmtFull(ctx.parsed.y)}}},
      scales: {x: {grid: {display: false}, ticks: {color: TICK, maxRotation: 0}}, y: {grid: {color: GRID}, ticks: {color: TICK, callback: v => fmt(v)}}}},
  });
}

function cdTravelExtras(t) {
  return `
    <div class="cd-tiles" style="grid-template-columns:repeat(2,1fr)">${cdTile('Foreign spend', money0(t.foreign_all_spend), 'all categories, charged abroad or in another currency')}
      ${cdTile('Likely trips', t.windows.length, 'clusters of lodging, ground transport and foreign charges')}</div>
    <div class="cd-card"><div class="cd-card-h">Likely travel windows (inferred from clusters of lodging, ground transport and foreign charges)</div>
      ${t.windows.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>From</th><th>To</th><th class="num">Lodging</th><th class="num">Ground</th><th class="num">Dining nearby</th><th>Merchants</th></tr></thead><tbody>
      ${t.windows.map(w => `<tr><td>${cfFmtDate(w.window_start)}</td><td>${cfFmtDate(w.window_end)}</td><td class="num">${fmtFull(w.lodging)}</td>
        <td class="num">${fmtFull(w.ground)}</td><td class="num">${fmtFull(w.dining_in_window)}</td><td class="cd-note">${h(w.merchants.join(', '))}${w.foreign ? ' · foreign' : ''}</td></tr>`).join('')}
      </tbody></table></div>` : '<div class="cd-card-b cd-note">No clusters that look like trips.</div>'}</div>
    <div class="cd-card"><div class="cd-card-h">Airlines, hotels and other travel brands</div>${t.by_brand.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>Type</th><th>Brand</th><th class="num">Net</th><th class="num">Rows</th></tr></thead><tbody>
      ${t.by_brand.map(b => `<tr><td>${h(b.l2)}</td><td>${h(b.family)}</td><td class="num">${fmtFull(b.net_spend)}</td><td class="num">${b.count}</td></tr>`).join('')}</tbody></table></div>` : '<div class="cd-card-b cd-note">None.</div>'}</div>`;
}

function cdFoodExtras(f) {
  return `
    <div class="cd-tiles" style="grid-template-columns:repeat(4,1fr)">
      ${cdTile('Dining', money0(f.dining), f.delivery_share_of_dining != null ? pct(f.delivery_share_of_dining) + ' of it delivery' : '')}
      ${cdTile('Groceries', money0(f.groceries), '')}
      ${cdTile('Dining : groceries', f.dining_to_grocery != null ? f.dining_to_grocery.toFixed(2) : '—', 'dollars dining out per grocery dollar')}
      ${cdTile('Alcohol & wine', money0(f.alcohol), `winery direct ${money0(f.winery_direct)}`)}</div>
    ${f.alcohol_merchants.length ? `<div class="cd-card"><div class="cd-card-h">Alcohol &amp; wine merchants</div><div class="cd-scroll"><table class="cd-table"><thead><tr><th>Type</th><th>Merchant</th><th class="num">Net</th><th class="num">Purchases</th></tr></thead><tbody>
      ${f.alcohol_merchants.map(r => `<tr><td>${h(r.key[0])}</td><td>${h(r.key[1])}</td><td class="num">${fmtFull(r.net_spend)}</td><td class="num">${r.purchases}</td></tr>`).join('')}</tbody></table></div></div>` : ''}`;
}

// ── Insights ──────────────────────────────────────────────────────────────────
async function cdRenderInsights(panel, reportId) {
  const [status, past] = await Promise.all([cdApi('/api/cards/ai/status'), cdApi('/api/cards/insights')]);
  panel.innerHTML = `
    <div class="cd-toolbar">
      <button class="btn-primary" id="cd-ins-gen" onclick="cdGenerateInsights()" ${status.enabled && !status.problem ? '' : 'disabled'}>Generate insights ▸</button>
      <select id="cd-ins-past" onchange="cdShowInsight(this.value)"><option value="">Past reports…</option>
        ${past.map(p => `<option value="${p.id}">${new Date(p.created_at).toLocaleString()} · ${h((p.headline || '').slice(0, 60))}</option>`).join('')}</select>
      <span class="cd-note"${status.problem ? ' style="color:var(--red)"' : ''}>${status.problem ? `⚠ ${h(status.problem.message)} Update the key under AI in the top bar.` : status.enabled ? `Uses ${h(status.model)} with the cards and period selected above. Only aggregate tables are sent.` : h(status.reason)}</span>
    </div>
    <div id="cd-ins-body">${past.length ? '' : '<div class="empty"><h3>No reports yet</h3><p>Generate a report to get findings and recommended actions from your spending totals.</p></div>'}</div>`;
  const pick = reportId || (past[0] && past[0].id);
  if (pick) { document.getElementById('cd-ins-past').value = pick; cdShowInsight(pick); }
}
async function cdGenerateInsights() {
  const btn = document.getElementById('cd-ins-gen');
  const body = document.getElementById('cd-ins-body');
  btn.disabled = true; btn.textContent = 'Generating…';
  body.innerHTML = '<div class="empty"><p>Writing the report. This can take a minute.</p></div>';
  const {from, to} = cdPeriod();
  const payload = {accounts: document.getElementById('cd-card-filter').value || null,
    date_from: from || null, date_to: to || null, common_period: document.getElementById('cd-common').checked};
  try {
    const r = await cdApi('/api/cards/insights', cdJson('POST', payload));
    cdRenderInsights(cdPanel('insights'), r.id);
  } catch (e) {
    body.innerHTML = `<div class="empty"><h3>Could not generate</h3><p>${h(e.message)}</p></div>`;
    btn.disabled = false; btn.textContent = 'Generate insights ▸';
  }
}
async function cdShowInsight(id) {
  if (!id) return;
  const r = await cdApi('/api/cards/insights/' + id);
  const rep = r.report;
  document.getElementById('cd-ins-body').innerHTML = `
    <div class="cd-card"><div class="cd-card-h">Findings</div><div class="cd-card-b">
      <h3 style="font-size:16px;margin-bottom:4px">${h(rep.headline)}</h3>
      <p class="cd-note">Generated ${new Date(r.created_at).toLocaleString()} by ${h(r.model)} from aggregate tables only${r.date_from || r.date_to ? ` · ${h(r.date_from || '…')} to ${h(r.date_to || '…')}` : ''}${r.common_period ? ' · common period' : ''}.</p>
      ${rep.findings.map(f => `<div class="ins-finding"><h4>${h(f.title)}</h4><div>${h(f.detail)}</div>
        ${f.numbers.length ? `<div class="ins-nums">${f.numbers.map(n => `<span>${h(n)}</span>`).join('')}</div>` : ''}</div>`).join('')}
    </div></div>
    <div class="cd-grid2">
      <div class="cd-card"><div class="cd-card-h">Recommended actions</div><div class="cd-scroll"><table class="cd-table">
        <thead><tr><th>Action</th><th class="num">Est. annual value (modeled)</th><th>Effort</th></tr></thead><tbody>
        ${rep.actions.map(a => `<tr><td>${h(a.action)}</td><td class="num">${a.estimated_annual_value != null ? money0(a.estimated_annual_value) : '—'}</td><td>${h(a.effort)}</td></tr>`).join('')}
        </tbody></table></div></div>
      <div class="cd-card"><div class="cd-card-h">Worth checking</div><div class="cd-card-b"><ul style="padding-left:18px">
        ${rep.review_items.map(x => `<li>${h(x)}</li>`).join('') || '<li class="cd-muted">Nothing flagged.</li>'}</ul></div></div>
    </div>
    <div class="cd-card"><div class="cd-card-h">Assumptions and gaps</div><div class="cd-card-b"><ul style="padding-left:18px">
      ${rep.assumptions.map(x => `<li>${h(x)}</li>`).join('')}</ul>
      ${r.payload ? `<details class="cd-details"><summary>What was sent</summary><div class="cd-pre">${h(JSON.stringify(r.payload, null, 2))}</div></details>` : ''}
    </div></div>`;
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function cdRenderSettings(panel) {
  const [imports, status, merchants, custom, renames] = await Promise.all([
    cdApi('/api/cards/imports'), cdApi('/api/cards/ai/status'), cdApi('/api/cards/merchants'),
    cdApi('/api/cards/categories'), cdApi('/api/cards/categories/renames'),
  ]);
  CD.renames = renames.renames;
  CD.customCats = custom.custom;
  CD.merchantList = merchants;
  const last = status.last_run;
  panel.innerHTML = `
    <div class="cd-card"><div class="cd-card-h"><span class="grow">Cards</span><button class="btn-sm" onclick="cdOpenAccount()">+ New card account</button></div>
      <div class="cd-scroll"><table class="cd-table"><thead><tr><th>Name</th><th>Issuer</th><th>Card numbers</th><th class="num">Transactions</th><th>Range</th><th></th></tr></thead><tbody>
      ${CD.accounts.length ? CD.accounts.map(a => `<tr><td>${h(a.name)}${a.archived ? ' <span class="cd-muted">(archived)</span>' : ''}</td><td>${h(a.issuer || '')}</td>
        <td>${h(a.last4 || '')}</td><td class="num">${a.txn_count}</td><td class="cd-note">${a.first_date ? cfFmtDate(a.first_date) + ' → ' + cfFmtDate(a.last_date) : 'no data'}</td>
        <td><button class="btn-sm" onclick="cdOpenAccount(${a.id})">Edit</button></td></tr>`).join('')
        : '<tr><td colspan="6" class="cd-muted" style="padding:16px">No cards yet.</td></tr>'}
      </tbody></table></div></div>

    <div class="cd-card"><div class="cd-card-h"><span class="grow">AI categorization</span>
      ${status.enabled ? `<button class="btn-sm" onclick="cdRunAi()" ${status.running ? 'disabled' : ''}>${status.running ? 'Running…' : 'Re-run AI'}</button>` : ''}</div>
      <div class="cd-card-b cd-note">
        ${status.enabled ? `On, using <strong>${h(status.categorize_model || status.model)}</strong>. ${status.pending_merchants} merchant${status.pending_merchants === 1 ? '' : 's'} waiting.` : h(status.reason)}
        ${status.problem ? `<br><span style="color:var(--red)">⚠ Paused since ${new Date(status.problem.at).toLocaleString()}: ${h(status.problem.message)} Automatic categorization stops until the key is updated under AI in the top bar; "Re-run AI" tries the current key once.</span>` : ''}
        ${last ? `<br>Last run ${new Date(last.at).toLocaleString()}: sent ${last.sent} merchant names, categorized ${last.classified}, updated ${last.transactions_updated} transactions${last.reused ? `, and reused stored categories for ${last.reused} more without a call` : ''}.${last.error ? ' <span style="color:var(--red)">' + h(last.error) + '</span>' : ''}` : ''}
        <br>Only sanitized merchant names, the issuer's category label and the payment processor are sent: never amounts, dates, card or account details, names, or full descriptions. Each merchant is sent once; answers are cached, and other spellings of a cached merchant (CIRCLE_K, Circle K, a truncated name) reuse the stored answer.
        <br><button class="btn-sm" style="margin-top:6px" onclick="aiOpen()">AI models and privacy log…</button> (also <strong>AI</strong> in the top bar)
      </div></div>

    <div class="cd-card"><div class="cd-card-h"><span class="grow">Your categories</span>
      <button class="btn-sm" onclick="cdOpenCustomCategory(null)">+ New</button></div>
      ${custom.custom.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>Category</th><th>Counts as</th><th>Detection</th>
        <th>Rewards</th><th>AI sees name</th><th class="num">Transactions</th><th></th></tr></thead><tbody>
        ${custom.custom.map(c => `<tr><td>${h(c.l1)} › <strong>${h(c.l2)}</strong>${c.l3 ? ` › <strong>${h(c.l3)}</strong>` : ''}</td>
          <td>${c.l3 ? '<span class="cd-muted">as its subcategory</span>' : h(CD_FIXEDNESS[c.fixedness])}</td>
          <td>${c.l3 ? '' : h(CD_ROLES[c.role])}</td><td>${c.l3 ? '' : h(CD_REWARDS[c.rewards] || c.rewards)}</td>
          <td>${c.ai_visible ? 'Yes' : 'No (private)'}</td><td class="num">${c.transactions}</td>
          <td style="white-space:nowrap"><button class="btn-sm" onclick="cdOpenCustomCategory(${c.id})">Edit</button>
            <button class="btn-sm" onclick="cdDeleteCustomCategory(${c.id})">Delete</button></td></tr>`).join('')}
        </tbody></table></div>` : ''}
      <div class="cd-card-b cd-note">Add your own subcategories (and details) under Flow's categories, for spending the built-in list doesn't describe, such as Entertainment › Pottery classes. You can also pick "＋ New subcategory…" in any category list, or ask the Assistant.</div></div>

    <div class="cd-card"><div class="cd-card-h"><span class="grow">Rename a category</span></div>
      <div class="cd-card-b">
        <div class="cd-rn-row">
          <select id="cd-rn-l1" onchange="cdRnL1()">${CD_CHOOSABLE_L1.map(x => `<option>${h(x)}</option>`).join('')}</select>
          <select id="cd-rn-l2" onchange="cdRnL2()"></select>
          <select id="cd-rn-l3"></select>
          <input type="text" id="cd-rn-name" placeholder="New name" maxlength="60">
          <button class="btn-sm btn-primary" onclick="cdRename()">Rename</button>
        </div>
        <div id="cd-rn-err" class="cd-note" style="color:var(--red)"></div>
        ${CD.renames.length ? `<table class="cd-table" style="margin-top:8px"><thead><tr><th>Built-in name</th><th>Now called</th><th></th></tr></thead><tbody>
          ${CD.renames.map((r, i) => `<tr><td>${h(r.l1)} › ${h(r.l2)}${r.l3 ? ' › ' + h(r.l3) : ''}</td><td><strong>${h(r.name)}</strong></td>
            <td><button class="btn-sm" onclick="cdRestoreName(${i})">Restore original</button></td></tr>`).join('')}
          </tbody></table>` : ''}
        <div class="cd-note" style="margin-top:6px">Renames any subcategory or detail, Flow's or yours. Its charges, merchants and amount rules follow, and so do future imports. A built-in one keeps how it behaves (rewards bucket, bill and subscription detection). Top-level categories are fixed.</div>
      </div></div>

    <div class="cd-card"><div class="cd-card-h"><span class="grow">Imports</span></div>
      ${imports.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>When</th><th>Card</th><th>File</th><th>Format</th><th>Period</th><th class="num">Imported</th><th class="num">Skipped</th><th></th></tr></thead><tbody>
      ${imports.map(i => `<tr><td>${new Date(i.created_at).toLocaleString()}</td><td>${h(i.card)}</td><td>${h(i.filename || '')}</td>
        <td class="cd-note">${h(i.issuer_format)} · ${h(i.sign_reason || '')}</td><td class="cd-note">${i.date_min ? cfFmtDate(i.date_min) + ' → ' + cfFmtDate(i.date_max) : ''}</td>
        <td class="num">${i.rows_imported}</td><td class="num">${i.rows_skipped}${i.pending_dropped ? ` (+${i.pending_dropped} pending)` : ''}</td>
        <td><button class="btn-sm" onclick="cdUndoImport(${i.id}, ${i.rows_imported})">Undo</button></td></tr>`).join('')}
      </tbody></table></div>` : '<div class="cd-card-b cd-note">No imports yet. Use ↑ Import in the top bar.</div>'}</div>

    <div class="cd-card"><div class="cd-card-h"><span class="grow">Merchants</span>
      <input type="text" id="cd-merch-q" placeholder="Filter" oninput="cdFilterMerchants()" style="text-transform:none;font-weight:400;letter-spacing:0">
      <button class="btn-sm clear-filters" id="cd-merch-clear" onclick="document.getElementById('cd-merch-q').value='';cdFilterMerchants()" hidden>✕ Clear filters</button></div>
      <div class="cd-scroll"><table class="cd-table"><thead><tr><th>Merchant</th><th>Family</th><th>Category</th><th>Source</th><th class="num">Rows</th><th class="num">Spend</th><th></th></tr></thead>
      <tbody id="cd-merch-body"></tbody></table></div></div>

    <div class="cd-card"><div class="cd-card-h">Export</div><div class="cd-card-b cd-note">
      <a href="/api/cards/export.csv?${cdQS({}, {noCommon: true})}">Download normalized transactions (CSV)</a> for the cards and period selected above, in the card-spend-analyzer layout (spend positive).</div></div>`;
  cdFilterMerchants();
  cdRnL1();
}
function cdRnL1() {
  const l1 = document.getElementById('cd-rn-l1').value;
  const node = CD.taxonomy.find(t => t.l1 === l1) || {l2: []};
  document.getElementById('cd-rn-l2').innerHTML = node.l2.map(x => `<option>${h(x.name)}</option>`).join('');
  cdRnL2();
}
function cdRnL2() {
  const l1 = document.getElementById('cd-rn-l1').value, l2 = document.getElementById('cd-rn-l2').value;
  const node = ((CD.taxonomy.find(t => t.l1 === l1) || {l2: []}).l2.find(x => x.name === l2)) || {l3: []};
  document.getElementById('cd-rn-l3').innerHTML = '<option value="">— the subcategory itself —</option>'
    + node.l3.filter(x => x !== l2).map(x => `<option>${h(x)}</option>`).join('');
}
async function cdRenameTo(l1, l2, l3, name) {
  const err = document.getElementById('cd-rn-err');
  try {
    const d = await cdApi('/api/cards/categories/rename', cdJson('POST', {l1, l2, l3, new_name: name}));
    await cdReloadTaxonomy();
    cdRefresh();
    return d;
  } catch (e) { if (err) err.textContent = e.message; }
}
function cdRename() {
  const g = id => document.getElementById(id).value;
  const name = g('cd-rn-name').trim();
  if (!name) { document.getElementById('cd-rn-err').textContent = 'Type the new name.'; return; }
  cdRenameTo(g('cd-rn-l1'), g('cd-rn-l2'), g('cd-rn-l3'), name);
}
function cdRestoreName(i) {
  const r = CD.renames[i];
  if (r.l3) cdRenameTo(r.l1, r.current_l2, r.name, r.l3);
  else cdRenameTo(r.l1, r.name, '', r.l2);
}
function cdFilterMerchants() {
  const q = (document.getElementById('cd-merch-q').value || '').toLowerCase();
  document.getElementById('cd-merch-clear').hidden = !q;
  const rows = (CD.merchantList || []).filter(m => !q || (m.canonical_merchant || '').toLowerCase().includes(q) || (m.label || '').toLowerCase().includes(q)).slice(0, 200);
  document.getElementById('cd-merch-body').innerHTML = rows.map(m => `<tr><td>${h(m.canonical_merchant)}<div class="raw">${h(m.label)}</div></td>
    <td>${h(m.merchant_family)}</td><td>${h(m.cat_l1 + ' › ' + m.cat_l2)}</td>
    <td class="cd-note">${m.cache_source === 'user' ? 'your rule' : m.cache_source === 'ai' ? 'AI' + (m.confidence != null ? ' ' + Math.round(m.confidence * 100) + '%' : '') : cdSourceLabel(m.category_source)}</td>
    <td class="num">${m.count}</td><td class="num">${fmtFull(m.spend)}</td>
    <td><button class="btn-sm" onclick="cdShowMerchant(${js(m.canonical_merchant)})">Open</button></td></tr>`).join('')
    || '<tr><td colspan="7" class="cd-muted" style="padding:16px">No merchants.</td></tr>';
}
async function cdUndoImport(id, n) {
  if (!confirm(`Remove this import and its ${n} transaction${n === 1 ? '' : 's'}?`)) return;
  await fetch('/api/cards/imports/' + id, {method: 'DELETE'});
  await cdLoadAccounts();
  cdRefresh();
}

function cdOpenAccount(id) {
  cdInjectModals();
  const a = id ? CD.accounts.find(x => x.id === id) : null;
  CD.editAcct = a;
  document.getElementById('cd-acct-title').textContent = a ? 'Edit card account' : 'New card account';
  document.getElementById('cd-acct-name').value = a ? a.name : '';
  document.getElementById('cd-acct-issuer').value = a ? (a.issuer || '') : '';
  document.getElementById('cd-acct-last4').value = a ? (a.last4 || '') : '';
  document.getElementById('cd-acct-names').value = a ? (a.cardholder_names || '') : '';
  document.getElementById('cd-acct-notes').value = a ? (a.notes || '') : '';
  document.getElementById('cd-acct-archived').checked = a ? a.archived : false;
  document.getElementById('cd-acct-options').classList.toggle('hidden', !a);
  document.getElementById('cd-acct-delete').classList.toggle('hidden', !a);
  ipAccountSection(document.getElementById('cd-acct-formats'), 'card', a ? a.id : null);
  document.getElementById('cd-acct-err').textContent = '';
  document.getElementById('cd-acct-modal').classList.remove('hidden');
  document.getElementById('cd-acct-name').focus();
}
async function cdSaveAccount() {
  const body = {
    name: document.getElementById('cd-acct-name').value.trim(),
    issuer: document.getElementById('cd-acct-issuer').value.trim(),
    last4: document.getElementById('cd-acct-last4').value.trim(),
    cardholder_names: document.getElementById('cd-acct-names').value.trim(),
    notes: document.getElementById('cd-acct-notes').value.trim(),
  };
  if (CD.editAcct) body.archived = document.getElementById('cd-acct-archived').checked;
  try {
    if (CD.editAcct) await cdApi('/api/cards/accounts/' + CD.editAcct.id, cdJson('PATCH', body));
    else await cdApi('/api/cards/accounts', cdJson('POST', body));
    cdCloseModal('cd-acct-modal');
    await cdLoadAccounts();
    cdRefresh();
  } catch (e) { document.getElementById('cd-acct-err').textContent = e.message; }
}
async function cdDeleteAccount() {
  const a = CD.editAcct;
  if (!a || !confirm(`Delete ${a.name} and all ${a.txn_count} of its transactions, its import history and its import-format links? This cannot be undone.`)) return;
  await fetch('/api/cards/accounts/' + a.id, {method: 'DELETE'});
  cdCloseModal('cd-acct-modal');
  await cdLoadAccounts();
  cdRefresh();
}

// ── AI status polling ─────────────────────────────────────────────────────────
async function cdRunAi() {
  const before = CD.aiStatus ? CD.aiStatus.runs || 0 : 0;
  let r;
  try { r = await cdApi('/api/cards/categorize', {method: 'POST'}); } catch (e) { alert(e.message); return; }
  if (!r.started) {
    document.getElementById('cd-ai-banner').innerHTML = `✦ ${h(r.reason || 'Nothing to run.')}`;
    return;
  }
  CD.aiWatchRuns = before;     // show this run's outcome even if it ends before the next poll
  cdPollAi(true);
  if (CD.tab === 'settings') setTimeout(cdRefresh, 300);
}

function cdRunSummary(r) {
  const n = r.classified || 0, u = r.unanswered || 0;
  return `✦ AI categorized ${n} merchant${n === 1 ? '' : 's'}` +
    (u ? `; ${u} it couldn't place ${u === 1 ? 'is' : 'are'} marked ⚠ Needs review` : '');
}

async function cdPollAi(start) {
  const banner = document.getElementById('cd-ai-banner');
  let s;
  try { s = await cdApi('/api/cards/ai/status'); } catch (e) { return; }
  const wasRunning = (CD.aiStatus && CD.aiStatus.running) || (CD.aiWatchRuns != null && (s.runs || 0) > CD.aiWatchRuns);
  if (CD.aiWatchRuns != null && !s.running && (s.runs || 0) > CD.aiWatchRuns) CD.aiWatchRuns = null;
  CD.aiStatus = s;
  banner.className = 'cd-banner';
  if (!s.enabled) {
    banner.innerHTML = `<span title="${h(s.reason)}">AI categorization off</span>`;
  } else if (s.problem && !s.running) {
    banner.className = 'cd-banner warn';
    banner.innerHTML = `⚠ AI paused: ${h(s.problem.message)} Update the key under AI in the top bar.
      <button class="btn-sm" onclick="cdRunAi()" title="Try the configured key once">Try again</button>`;
  } else if (s.running) {
    banner.innerHTML = '✦ Categorizing merchants with AI…';
  } else if (s.last_run && s.last_run.error && wasRunning) {
    banner.className = 'cd-banner warn';
    banner.innerHTML = `AI categorization stopped: ${h(s.last_run.error)} <button class="btn-sm" onclick="cdRunAi()">Retry</button>`;
  } else if (s.pending_merchants) {
    banner.innerHTML = `✦ ${s.pending_merchants} merchant${s.pending_merchants === 1 ? '' : 's'} awaiting AI <button class="btn-sm" onclick="cdRunAi()">Run</button>`;
  } else {
    banner.innerHTML = s.last_run && wasRunning ? cdRunSummary(s.last_run) : '';
  }
  if (s.pending_merchants && !s.running && !s.problem && wasRunning && s.last_run && !s.last_run.error) {
    banner.innerHTML = cdRunSummary(s.last_run) + ` · ${s.pending_merchants} still waiting <button class="btn-sm" onclick="cdRunAi()">Run</button>`;
  }
  if (wasRunning && !s.running) cdRefresh();
  clearTimeout(CD.aiTimer);
  if (s.running || start) CD.aiTimer = setTimeout(() => cdPollAi(false), s.running ? 2500 : 1500);
}

// Called by the unified import modal after a card import: open the imported
// card's transactions. cdShow applies the selection once accounts are loaded.
function cdAfterImport(acctId) {
  CD.tf = {...CD_TF_DEFAULT};
  CD.page = 1;
  CD.tab = 'transactions';
  CD.pendingAccount = String(acctId);
  showSection('cards');
}

function cdReset() {
  CD.ready = false; CD.accounts = []; CD.tab = 'transactions'; CD.txnCache = {};
  clearTimeout(CD.aiTimer);
}
