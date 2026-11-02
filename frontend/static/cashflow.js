// ═══════════════════════════════════════════════════════════════════════════════
// CASH FLOW: Dashboard tab with a Sankey of where money went (backend/cash_flow.py),
// and the Sort payees dialog that gives each register payee a role.
// Built with DOM nodes; payee names never go through innerHTML.
// ═══════════════════════════════════════════════════════════════════════════════

const CFW = {period: 'ytd', perMonth: true, from: '', to: '', data: null, payees: null, pf: {q: '', show: 'needs'}, touched: new Set()};
const CFW_DEFAULT = {period: 'ytd', perMonth: true};
const CFW_NS = 'http://www.w3.org/2000/svg';
const CFW_GROUP = {income: 'node', hub: 'node', cash: 'node', unsorted: 'node', spend: 'spend', debt: 'debt', save: 'save'};

function cfwEl(tag, attrs = {}, ...kids) {
  const ns = ['svg', 'g', 'path', 'rect', 'text'].includes(tag);
  const el = ns ? document.createElementNS(CFW_NS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.setAttribute('class', v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'checked' || k === 'selected' || k === 'disabled' || k === 'hidden') el[k] = !!v;
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
}
async function cfwApi(url, opts) {
  const res = await fetch(url, opts);
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Request failed (' + res.status + ')');
  return d;
}
const cfwMoney = v => (v < 0 ? '-$' : '$') + Math.round(Math.abs(v)).toLocaleString('en-US');

// ── Period ────────────────────────────────────────────────────────────────────
function cfwRange() {
  const iso = localIso;
  const today = new Date();
  const monthStart = (y, m) => new Date(y, m, 1);
  const y = today.getFullYear(), m = today.getMonth();
  const endOfLast = new Date(y, m, 0);
  switch (CFW.period) {
    case '1m': return {start: iso(monthStart(y, m - 1)), end: iso(endOfLast)};
    case 'this': return {start: iso(monthStart(y, m)), end: iso(today)};
    case '3m': return {start: iso(monthStart(y, m - 3)), end: iso(endOfLast)};
    case '12m': return {start: iso(monthStart(y, m - 12)), end: iso(endOfLast)};
    case 'ytd': return {start: y + '-01-01', end: iso(today)};
    case 'custom': return {start: CFW.from, end: CFW.to};
  }
  return {start: '', end: ''};
}
function cfwIsDefault() { return CFW.period === CFW_DEFAULT.period && CFW.perMonth === CFW_DEFAULT.perMonth; }

// ── Tab ───────────────────────────────────────────────────────────────────────
function cfwRenderShell() {
  const panel = document.getElementById('tab-cashflow');
  if (panel.dataset.ready) return;
  panel.dataset.ready = '1';
  const periodSel = cfwEl('select', {id: 'cfw-period', 'aria-label': 'Period', onchange: e => {
    CFW.period = e.target.value; document.getElementById('cfw-custom').hidden = CFW.period !== 'custom'; cfwLoad(); }},
    ...[['1m', 'Last month'], ['this', 'This month so far'], ['3m', 'Last 3 months'], ['12m', 'Last 12 months'],
        ['ytd', 'This year'], ['custom', 'Custom…']].map(([v, t]) => cfwEl('option', {value: v, selected: v === CFW.period}, t)));
  const custom = cfwEl('span', {id: 'cfw-custom', hidden: true},
    cfwEl('input', {type: 'date', id: 'cfw-from', onchange: e => { CFW.from = e.target.value; cfwLoad(); }}), '→',
    cfwEl('input', {type: 'date', id: 'cfw-to', onchange: e => { CFW.to = e.target.value; cfwLoad(); }}));
  const mode = cfwEl('select', {id: 'cfw-mode', 'aria-label': 'Amounts', onchange: e => { CFW.perMonth = e.target.value === 'avg'; cfwLoad(); }},
    cfwEl('option', {value: 'avg', selected: CFW.perMonth}, 'Per month (average)'),
    cfwEl('option', {value: 'total', selected: !CFW.perMonth}, 'Totals for the period'));
  const clear = cfwEl('button', {class: 'btn-sm clear-filters', id: 'cfw-clear', hidden: true, onclick: cfwClearFilters}, '✕ Clear filters');
  panel.replaceChildren(cfwEl('div', {class: 'chart-card'},
    cfwEl('div', {class: 'cfw-controls'}, periodSel, custom, mode, clear, cfwEl('span', {class: 'grow'}),
      cfwEl('button', {class: 'btn-sm', onclick: () => cfwOpenPayees()}, 'Sort payees')),
    cfwEl('div', {class: 'cfw-stats', id: 'cfw-stats'}),
    cfwEl('div', {id: 'cfw-notes'}),
    cfwEl('div', {class: 'cfw-legend', 'aria-hidden': 'true'},
      ...[['node', 'Money in and cash'], ['spend', 'Spending'], ['debt', 'Debt payments'], ['save', 'Saving and investing']]
        .map(([k, t]) => cfwEl('span', {}, cfwEl('i', {style: `background:var(--cfw-${k})`}), t))),
    cfwEl('div', {class: 'cfw-crumbs', id: 'cfw-crumbs'}),
    cfwEl('div', {class: 'cfw-scroll', id: 'cfw-scroll'}, cfwEl('svg', {id: 'cfw-svg', role: 'img', 'aria-label': 'Cash flow diagram'}),
      cfwEl('div', {class: 'cfw-tip', id: 'cfw-tip', hidden: true})),
    cfwEl('div', {id: 'cfw-list'}),
    cfwEl('details', {class: 'cfw-flows'}, cfwEl('summary', {}, 'Show the flows as a table'),
      cfwEl('div', {style: 'overflow-x:auto'}, cfwEl('table', {class: 'cfw-table', id: 'cfw-table'})))));
}
function cfwClearFilters() {
  Object.assign(CFW, CFW_DEFAULT, {from: '', to: ''});
  document.getElementById('cfw-period').value = CFW.period;
  document.getElementById('cfw-mode').value = CFW.perMonth ? 'avg' : 'total';
  document.getElementById('cfw-custom').hidden = true;
  cfwLoad();
}

async function cfwLoad() {
  cfwRenderShell();
  document.getElementById('cfw-clear').hidden = cfwIsDefault();
  const {start, end} = cfwRange();
  if (CFW.period === 'custom' && (!start || !end)) return;
  const q = new URLSearchParams({per_month: CFW.perMonth ? 'true' : 'false'});
  if (start) q.set('start', start);
  if (end) q.set('end', end);
  CFW.detailCache = {};                          // transactions may have changed (an edit, a new role)
  try { CFW.data = await cfwApi('/api/cashflow?' + q); }
  catch (e) { document.getElementById('cfw-notes').replaceChildren(cfwEl('div', {class: 'cfw-note warn'}, e.message)); return; }
  cfwRender();
  if (!CFW.zoom && CFW.list) cfwShowList({...CFW.list, quiet: true});   // keep an open list, refreshed
}

// ── Sankey layout and drawing ─────────────────────────────────────────────────
const CFW_W = 1000, CFW_NODE_W = 12, CFW_PAD = 18, CFW_LABEL_GAP = 33;
const CFW_COL_X = [40, 300, 560, 948];          // overview: sources, money in, groups, details
const CFW_ZOOM_X = [40, 470, 948];              // zoomed into a group: the group, its bars, one level further
function cfwLayout(nodes, links, colX = CFW_COL_X) {
  const last = colX.length - 1;
  const cols = colX.map((_, c) => nodes.filter(n => n.col === c));
  nodes.forEach((n, i) => {
    n.i = i;
    n.inV = links.filter(l => l.target === i).reduce((a, l) => a + l.value, 0);
    n.outV = links.filter(l => l.source === i).reduce((a, l) => a + l.value, 0);
    n.v = Math.max(n.inV, n.outV, n.value || 0);
  });
  let H = Math.max(520, cols[last].length * 38 + 40, cols[0].length * 38 + 40);
  const gapFor = c => c === last ? 8 : c === 0 ? 18 : 22;
  const k = Math.min(...cols.filter(ns => ns.length).map(ns => {
    const c = ns[0].col;
    return (H - 2 * CFW_PAD - gapFor(c) * (ns.length - 1)) / Math.max(1, ns.reduce((a, n) => a + n.v, 0));
  }));
  cols.forEach((ns, c) => {
    if (!ns.length) return;
    const total = ns.reduce((a, n) => a + n.v * k, 0) + gapFor(c) * (ns.length - 1);
    let y = CFW_PAD + (H - 2 * CFW_PAD - total) / 2;
    ns.forEach(n => { n.x = colX[c]; n.y = y; n.h = Math.max(2, n.v * k); y += n.v * k + gapFor(c); });
    let prevBottom = -Infinity, prevC = -Infinity;
    ns.forEach(n => {
      n.y = Math.max(n.y, prevBottom + 3, prevC + CFW_LABEL_GAP - n.h / 2);
      prevBottom = n.y + n.h; prevC = n.y + n.h / 2;
    });
    const over = prevBottom + CFW_PAD - H, up = Math.min(Math.max(over, 0), ns[0].y - CFW_PAD);
    ns.forEach(n => { n.y -= up; n.ly = n.y + n.h / 2; });
    H = Math.max(H, ns[ns.length - 1].y + ns[ns.length - 1].h + CFW_PAD);
  });
  nodes.forEach(n => {
    let yo = n.y, yi = n.y;
    links.filter(l => l.source === n.i).sort((a, b) => nodes[a.target].y - nodes[b.target].y)
      .forEach(l => { l.w = l.value * k; l.sy = yo + l.w / 2; yo += l.w; });
    links.filter(l => l.target === n.i).sort((a, b) => nodes[a.source].y - nodes[b.source].y)
      .forEach(l => { l.ty = yi + l.w / 2; yi += l.w; });
  });
  return H;
}

function cfwRender() {
  const d = CFW.data, t = d.totals;
  const per = d.per_month ? ' a month' : '';
  const pct = v => t.income ? ' · ' + Math.round(v / t.income * 100) + '%' : '';
  const stat = (label, v, extra = '') => cfwEl('span', {}, label + ' ', cfwEl('b', {}, cfwMoney(v)), extra);
  document.getElementById('cfw-stats').replaceChildren(
    stat('Money in', t.income, per), stat('Spending', t.spending, pct(t.spending)), stat('Debt payments', t.debt, pct(t.debt)),
    stat('Saving and investing', t.saving, pct(t.saving)), stat(t.net >= 0 ? 'Added to cash' : 'Drawn from cash', Math.abs(t.net)));
  const notes = [];
  if (t.unsorted > 0) notes.push(cfwEl('div', {class: 'cfw-note warn'},
    `${cfwMoney(t.unsorted)}${per} went to ${d.unsorted.length} payee${d.unsorted.length === 1 ? '' : 's'} without a role, shown as Not sorted yet.`,
    cfwEl('button', {class: 'btn-sm', onclick: () => cfwOpenPayees('needs')}, 'Sort payees')));
  for (const w of d.warnings || []) notes.push(cfwEl('div', {class: 'cfw-note warn'}, w));
  if (t.card_payments || t.transfers) notes.push(cfwEl('div', {class: 'cfw-note'},
    `Not counted again: ${cfwMoney(t.card_payments)}${per} of card payments (the card charges are shown instead)`
    + ` and ${cfwMoney(t.transfers)}${per} moved between your own cash accounts.`));
  document.getElementById('cfw-notes').replaceChildren(...notes);
  if (CFW.zoom) return cfwRenderZoom();
  cfwCrumbs();
  if (!d.links.length) {
    const svg = document.getElementById('cfw-svg');
    svg.replaceChildren();
    svg.setAttribute('viewBox', `0 0 ${CFW_W} 120`);
    svg.append(cfwEl('text', {x: CFW_W / 2, y: 60, 'text-anchor': 'middle', class: 'name'}, 'No register or card activity in this period.'));
    document.getElementById('cfw-table').replaceChildren();
    return;
  }
  cfwDraw(d.nodes.map(n => ({...n})), d.links.map(l => ({...l})), CFW_COL_X, cfwNodeAction);
}

// Draw nodes and links into the diagram, with the flows table underneath.
function cfwDraw(nodes, links, colX, actionFor) {
  const t = CFW.data.totals, per = CFW.data.per_month ? ' a month' : '';
  const svg = document.getElementById('cfw-svg');
  svg.replaceChildren();
  const H = cfwLayout(nodes, links, colX);
  const last = colX.length - 1;
  svg.setAttribute('viewBox', `0 0 ${CFW_W} ${H}`);
  const bands = cfwEl('g'), bars = cfwEl('g'), labels = cfwEl('g');
  svg.append(bands, bars, labels);
  const colorOf = n => `var(--cfw-${CFW_GROUP[n.group] || 'node'})`;
  const bandColor = l => {
    const s = nodes[l.source], tg = nodes[l.target];
    const g = CFW_GROUP[tg.group] !== 'node' ? tg.group : CFW_GROUP[s.group] !== 'node' ? s.group : null;
    return g ? `var(--cfw-${CFW_GROUP[g]})` : 'var(--cfw-band)';
  };
  const share = v => t.income ? ` · ${(v / t.income * 100).toFixed(1)}% of money in` : '';
  links.forEach((l, li) => {
    const s = nodes[l.source], tg = nodes[l.target];
    const x0 = s.x + CFW_NODE_W, x1 = tg.x, mx = (x0 + x1) / 2;
    const p = cfwEl('path', {d: `M${x0},${l.sy} C${mx},${l.sy} ${mx},${l.ty} ${x1},${l.ty}`, class: 'band',
      stroke: bandColor(l), 'stroke-width': Math.max(1, l.w - 1.5), 'data-link': li});
    p.addEventListener('mousemove', e => cfwTip(e, `${s.name} → ${tg.name}`, cfwMoney(l.value) + per, share(l.value)));
    p.addEventListener('mouseenter', () => cfwHighlight(svg, links, {links: [li]}));
    p.addEventListener('mouseleave', () => cfwClearHi(svg));
    bands.append(p);
  });
  nodes.forEach(n => {
    const action = actionFor(n);
    const g = cfwEl('g', {class: 'node' + (action ? ' link' : ''), tabindex: 0, role: action ? 'button' : 'img',
      'aria-label': `${n.name}: ${cfwMoney(n.v)}${per}` + (action ? `. ${action.hint}` : '')});
    g.append(cfwEl('rect', {x: n.x, y: n.y, width: CFW_NODE_W, height: n.h, rx: 2, fill: colorOf(n)}));
    const hint = action ? action.hint : '';
    const show = e => { cfwHighlight(svg, links, {node: n.i}); cfwTip(e, n.name, cfwMoney(n.v) + per, share(n.v), hint); };
    g.addEventListener('mousemove', show);
    g.addEventListener('focus', () => { const r = g.getBoundingClientRect(); show({clientX: r.right, clientY: r.top}); });
    g.addEventListener('mouseleave', () => cfwClearHi(svg));
    g.addEventListener('blur', () => cfwClearHi(svg));
    if (action) {
      g.addEventListener('click', () => { cfwClearHi(svg); action.run(); });
      g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); action.run(); } });
    }
    bars.append(g);
    const right = n.col < last, x = right ? n.x + CFW_NODE_W + 8 : n.x - 8, anchor = right ? 'start' : 'end';
    const big = n.col > 0 && n.col < last;
    const nameText = cfwEl('text', {x, y: n.ly - 3, 'text-anchor': anchor, class: 'name' + (action ? ' link' : '')}, n.name);
    if (action) nameText.addEventListener('click', () => action.run());
    labels.append(nameText);
    labels.append(cfwEl('text', {x, y: n.ly + 12, 'text-anchor': anchor, class: 'val' + (big ? ' big' : '')}, cfwMoney(n.v)));
  });
  const table = document.getElementById('cfw-table');
  table.replaceChildren(
    cfwEl('thead', {}, cfwEl('tr', {}, cfwEl('th', {}, 'From'), cfwEl('th', {}, 'To'),
      cfwEl('th', {class: 'num'}, 'Amount' + per), cfwEl('th', {class: 'num'}, 'Share of money in'))),
    cfwEl('tbody', {}, ...links.map(l => cfwEl('tr', {}, cfwEl('td', {}, nodes[l.source].name), cfwEl('td', {}, nodes[l.target].name),
      cfwEl('td', {class: 'num'}, cfwMoney(l.value)), cfwEl('td', {class: 'num'}, t.income ? (l.value / t.income * 100).toFixed(1) + '%' : '—')))));
}

// ── Drill-in ──────────────────────────────────────────────────────────────────
// Overview: a group (Spending, Debt payments, Saving and investing) zooms in; a bar on either
// side lists its transactions. Zoomed: the group's bars, one level further (card subcategory or
// payee); any bar lists its transactions. The server tags every transaction with its bar
// (cash_flow.tag), so a list always adds up to the bar it came from.
const CFW_GROUP_NAME = {spend: 'Spending', debt: 'Debt payments', save: 'Saving and investing',
                        income: 'Money in', unsorted: 'Not sorted yet'};

function cfwNodeAction(n) {
  if (['spend', 'debt', 'save'].includes(n.key)) return {hint: 'Click to break it down', run: () => cfwZoom(n.key)};
  if (n.key === 'unsorted') return {hint: 'Click to see these payments', run: () => cfwShowList({group: 'unsorted', title: 'Not sorted yet'})};
  if (n.col === 0 && n.group === 'income') return {hint: 'Click to see these deposits',
    run: () => cfwShowList({group: 'income', details: n.details, title: `Money in › ${n.name}`})};
  if (n.col === 3 && n.details) return {hint: 'Click to see its transactions', run: () => cfwShowList({group: n.group,
    details: n.details, title: `${CFW_GROUP_NAME[n.group]} › ${n.name}`, cardCategory: n.from === 'card' ? n.category : ''})};
  return null;
}

async function cfwDetail(group) {
  const {start, end} = cfwRange();
  const key = `${group}|${start}|${end}`;
  CFW.detailCache = CFW.detailCache || {};
  if (!CFW.detailCache[key]) {
    const q = new URLSearchParams({group});
    if (start) q.set('start', start);
    if (end) q.set('end', end);
    CFW.detailCache[key] = await cfwApi('/api/cashflow/detail?' + q);
  }
  return CFW.detailCache[key].entries;
}

function cfwSum(entries) { return entries.reduce((a, e) => a + e.amount, 0); }
function cfwFold(map, n, other) {
  // [[name, value, members]] biggest first; past n, the rest fold into one "other" bar.
  const ranked = [...map.entries()].map(([k, es]) => [k, cfwSum(es), [k]]).filter(x => x[1] > 0.005).sort((a, b) => b[1] - a[1]);
  if (ranked.length <= n + 1) return ranked;
  const rest = ranked.slice(n);
  return [...ranked.slice(0, n), [other, rest.reduce((a, x) => a + x[1], 0), rest.flatMap(x => x[2])]];
}
function cfwGroupBy(entries, key) {
  const m = new Map();
  for (const e of entries) { if (!m.has(e[key])) m.set(e[key], []); m.get(e[key]).push(e); }
  return m;
}

function cfwZoom(group) {
  CFW.zoom = {group};
  CFW.list = null;
  document.getElementById('cfw-list').replaceChildren();
  cfwRenderZoom();
}
function cfwUnzoom() {
  CFW.zoom = null;
  CFW.list = null;
  document.getElementById('cfw-list').replaceChildren();
  cfwRender();
}

async function cfwRenderZoom() {
  const group = CFW.zoom.group;
  cfwCrumbs();
  let entries;
  try { entries = await cfwDetail(group); }
  catch (e) { document.getElementById('cfw-notes').append(cfwEl('div', {class: 'cfw-note warn'}, e.message)); return; }
  if (!CFW.zoom || CFW.zoom.group !== group) return;
  const div = CFW.data.per_month ? CFW.data.months : 1;
  const nodes = [], links = [];
  const add = (n) => { nodes.push(n); return nodes.length - 1; };
  const root = add({key: 'root', name: CFW_GROUP_NAME[group], col: 0, group, value: cfwSum(entries) / div});
  const byDetail = cfwGroupBy(entries, 'detail');
  for (const [name, value, members] of cfwFold(byDetail, 9, 'Other')) {
    const mine = entries.filter(e => members.includes(e.detail));
    const di = add({key: 'd:' + name, name, col: 1, group, details: members, value: value / div,
                    cardCategory: mine.every(e => e.source === 'card') && members.length === 1 ? name : ''});
    links.push({source: root, target: di, value: value / div});
    if (members.length !== 1) continue;
    const subs = cfwGroupBy(mine, 'sub');
    if (subs.size === 1 && subs.has(name)) continue;          // a payee is its own last level
    for (const [sname, svalue, smembers] of cfwFold(subs, 5, `Other in ${name}`)) {
      const si = add({key: `s:${name}:${sname}`, name: sname, col: 2, group, details: members, subs: smembers, value: svalue / div});
      links.push({source: di, target: si, value: svalue / div});
    }
  }
  if (!links.length) {
    const svg = document.getElementById('cfw-svg');
    svg.replaceChildren();
    svg.setAttribute('viewBox', `0 0 ${CFW_W} 120`);
    svg.append(cfwEl('text', {x: CFW_W / 2, y: 60, 'text-anchor': 'middle', class: 'name'}, 'Nothing here in this period.'));
    return;
  }
  cfwDraw(nodes, links, CFW_ZOOM_X, n => {
    if (n.col === 0) return {hint: 'Click to go back to everything', run: cfwUnzoom};
    const title = [CFW_GROUP_NAME[group], ...(n.col === 2 ? [nodes.find(x => x.col === 1 && x.details === n.details).name] : []), n.name].join(' › ');
    return {hint: 'Click to see its transactions', run: () => cfwShowList({group, details: n.details, subs: n.subs, title,
      cardCategory: n.col === 1 ? n.cardCategory : ''})};
  });
  if (CFW.list) cfwShowList({...CFW.list, quiet: true});
}

function cfwCrumbs() {
  const box = document.getElementById('cfw-crumbs');
  if (!CFW.zoom) { box.replaceChildren(cfwEl('span', {class: 'hint'}, 'Click a bar to break it down or see its transactions.')); return; }
  box.replaceChildren(cfwEl('button', {class: 'crumb', onclick: cfwUnzoom}, 'All'), ' › ',
    cfwEl('span', {class: 'here'}, CFW_GROUP_NAME[CFW.zoom.group]));
}

// The transactions behind a bar, under the diagram.
const CFW_LIST_PAGE = 200;
async function cfwShowList(spec) {
  CFW.list = spec;
  const box = document.getElementById('cfw-list');
  let entries;
  try { entries = await cfwDetail(spec.group); }
  catch (e) { box.replaceChildren(cfwEl('div', {class: 'cfw-note warn'}, e.message)); return; }
  if (CFW.list !== spec) return;
  const rows = entries.filter(e => (!spec.details || spec.details.includes(e.detail)) && (!spec.subs || spec.subs.includes(e.sub)));
  const total = cfwSum(rows), months = CFW.data.months;
  const shown = rows.slice(0, spec.all ? rows.length : CFW_LIST_PAGE);
  const actions = [];
  if (spec.cardCategory) actions.push(cfwEl('button', {class: 'btn-sm', onclick: () => cfwOpenCardCategory(spec.cardCategory)}, 'Open in Cards'));
  if (rows.some(e => e.source === 'cash')) actions.push(cfwEl('button', {class: 'btn-sm', onclick: () =>
    cfwOpenPayees(spec.group === 'unsorted' ? 'needs' : 'all', rows.length && rows.every(e => e.label === rows[0].label) ? rows[0].label : '')}, 'Sort payees'));
  actions.push(cfwEl('button', {class: 'btn-sm', 'aria-label': 'Close', onclick: () => { CFW.list = null; box.replaceChildren(); }}, '✕'));
  const body = shown.map(e => {
    const cat = e.source === 'card' ? `${e.detail} › ${e.sub}` : e.detail.replace(/ \(cash\)$/, '');
    const open = e.source === 'card'
      ? cfwEl('button', {class: 'btn-sm', onclick: () => cfwEditCard(e)}, 'Edit')
      : cfwEl('button', {class: 'btn-sm', onclick: () => cfwOpenPayees('all', e.label)}, 'Role');
    return cfwEl('tr', {},
      cfwEl('td', {}, cfwFmtDay(e.date)),
      cfwEl('td', {}, cfwEl('div', {}, e.label)),
      cfwEl('td', {class: 'sub'}, cat),
      cfwEl('td', {class: 'sub'}, e.where + (e.source === 'cash' ? ' (cash)' : '')),
      cfwEl('td', {class: 'num'}, cfwMoney2(e.amount)),
      cfwEl('td', {}, open));
  });
  box.replaceChildren(cfwEl('div', {class: 'cfw-list-card'},
    cfwEl('div', {class: 'cfw-list-head'},
      cfwEl('div', {class: 'grow'}, cfwEl('div', {class: 'title'}, spec.title),
        cfwEl('div', {class: 'sub'}, `${rows.length} transaction${rows.length === 1 ? '' : 's'} · ${cfwMoney2(total)} in the period`
          + (CFW.data.per_month && months > 1 ? ` · ${cfwMoney(total / months)} a month` : ''))),
      ...actions),
    cfwEl('div', {style: 'overflow-x:auto'}, cfwEl('table', {class: 'cfw-table'},
      cfwEl('thead', {}, cfwEl('tr', {}, cfwEl('th', {}, 'Date'), cfwEl('th', {}, 'Merchant or payee'), cfwEl('th', {}, 'Category'),
        cfwEl('th', {}, 'Card or account'), cfwEl('th', {class: 'num'}, 'Amount'), cfwEl('th', {}, ''))),
      cfwEl('tbody', {}, ...body))),
    rows.length > shown.length ? cfwEl('button', {class: 'btn-sm', style: 'margin-top:8px',
      onclick: () => cfwShowList({...spec, all: true})}, `Show all ${rows.length}`) : ''));
  if (!spec.quiet) box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
function cfwFmtDay(iso) {
  return parseLocalDate(iso).toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric'});
}
const cfwMoney2 = v => (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

// Card charges open the Cards edit dialog right here; the diagram reloads after a change.
async function cfwEditCard(e) {
  if (!e.txn || typeof cdOpenCategory !== 'function') return cfwOpenCardCategory(e.detail);
  cdInjectModals();
  if (!CD.taxonomy || !CD.taxonomy.length) {
    try { CD.taxonomy = (await cdApi('/api/cards/taxonomy')).tree; } catch (err) { return cfwOpenCardCategory(e.detail); }
  }
  CD.onEdited = () => { CD.onEdited = null; cfwLoad(); };
  cdOpenCategory(e.id, e.txn);
}

function cfwOpenCardCategory(l1) {
  const {start, end} = cfwRange();
  showSection('cards');
  setTimeout(() => {
    const g = id => document.getElementById(id);
    if (start && end) { g('cd-period').value = 'custom'; g('cd-from').value = start; g('cd-to').value = end;
      g('cd-custom-range').style.display = 'flex'; }
    cdDrill({l1});
  }, 80);
}

function cfwHighlight(svg, links, {links: ls, node}) {
  svg.classList.add('hovering');
  const on = new Set(ls || links.map((l, i) => (l.source === node || l.target === node) ? i : -1).filter(i => i >= 0));
  svg.querySelectorAll('.band').forEach(p => p.classList.toggle('on', on.has(+p.dataset.link)));
}
function cfwClearHi(svg) { svg.classList.remove('hovering'); document.getElementById('cfw-tip').hidden = true; }
function cfwTip(e, title, amount, share, hint) {
  const tip = document.getElementById('cfw-tip'), sc = document.getElementById('cfw-scroll');
  tip.replaceChildren(cfwEl('div', {}, title), cfwEl('div', {}, cfwEl('b', {}, amount), cfwEl('span', {class: 'sub'}, share)),
    hint ? cfwEl('div', {class: 'sub'}, hint) : '');
  tip.hidden = false;
  const box = sc.getBoundingClientRect();
  let x = e.clientX - box.left + sc.scrollLeft + 14;
  if (x + tip.offsetWidth > sc.scrollLeft + box.width) x = e.clientX - box.left + sc.scrollLeft - tip.offsetWidth - 14;
  tip.style.left = x + 'px'; tip.style.top = (e.clientY - box.top + 14) + 'px';
}

// ── Sort payees ───────────────────────────────────────────────────────────────
function cfwInjectPayees() {
  if (document.getElementById('cfw-payees-modal')) return;
  const box = cfwEl('div', {class: 'modal-box'},
    cfwEl('h2', {}, 'Sort payees'),
    cfwEl('p', {}, 'Give each payee in your cash register a role once; every month then sorts itself. Card payments and transfers between your accounts are found automatically and not counted twice. Suggestions come from those matches and from the payee\'s name; a role you set always wins.'),
    cfwEl('div', {class: 'cfw-pf'},
      cfwEl('input', {type: 'text', id: 'cfw-pf-q', placeholder: 'Search payees', oninput: e => { CFW.pf.q = e.target.value; cfwRenderPayees(); }}),
      cfwEl('select', {id: 'cfw-pf-show', onchange: e => { CFW.pf.show = e.target.value; cfwRenderPayees(); }},
        cfwEl('option', {value: 'needs'}, 'Needs a role'), cfwEl('option', {value: 'suggested'}, 'Suggested, not confirmed'),
        cfwEl('option', {value: 'set'}, 'Set by you'), cfwEl('option', {value: 'all'}, 'All payees')),
      cfwEl('button', {class: 'btn-sm clear-filters', id: 'cfw-pf-clear', onclick: () => { CFW.pf = {q: '', show: 'needs'}; cfwSyncPf(); cfwRenderPayees(); }}, '✕ Clear filters'),
      cfwEl('span', {class: 'count', id: 'cfw-pf-count'})),
    cfwEl('div', {style: 'overflow-x:auto'}, cfwEl('table', {class: 'cfw-table', id: 'cfw-payees-table'})),
    cfwEl('div', {id: 'cfw-pf-err', style: 'color:var(--red);font-size:12px'}),
    cfwEl('div', {class: 'modal-footer'}, cfwEl('button', {class: 'btn-primary', onclick: cfwClosePayees}, 'Done')));
  document.body.append(cfwEl('div', {id: 'cfw-payees-modal', class: 'hidden'}, box));
}
function cfwSyncPf() {
  document.getElementById('cfw-pf-q').value = CFW.pf.q;
  document.getElementById('cfw-pf-show').value = CFW.pf.show;
  document.getElementById('cfw-pf-clear').hidden = !CFW.pf.q && CFW.pf.show === 'needs';
}
async function cfwOpenPayees(show = 'needs', q = '') {
  cfwInjectPayees();
  CFW.pf = {q, show};
  CFW.touched = new Set();
  cfwSyncPf();
  document.getElementById('cfw-payees-modal').classList.remove('hidden');
  const {start, end} = cfwRange();
  const qs = new URLSearchParams();
  if (start) qs.set('start', start);
  if (end) qs.set('end', end);
  try { CFW.payees = await cfwApi('/api/cashflow/payees?' + qs); }
  catch (e) { document.getElementById('cfw-pf-err').textContent = e.message; return; }
  cfwRenderPayees();
}
function cfwClosePayees() {
  document.getElementById('cfw-payees-modal').classList.add('hidden');
  if (CFW.data) cfwLoad();
}
function cfwRenderPayees() {
  if (!CFW.payees) return;
  cfwSyncPf();
  const P = CFW.payees, q = CFW.pf.q.trim().toLowerCase();
  const rows = P.payees.filter(p => {
    if (q && !(p.label.toLowerCase().includes(q) || (p.example || '').toLowerCase().includes(q))) return false;
    if (CFW.touched.has(p.key)) return true;          // stays in view while you finish it
    if (CFW.pf.show === 'needs') return !p.role && !p.suggested;
    if (CFW.pf.show === 'suggested') return !p.role && p.suggested;
    if (CFW.pf.show === 'set') return !!p.role;
    return true;
  });
  document.getElementById('cfw-pf-count').textContent = `${rows.length} of ${P.payees.length} payees`;
  const roleName = r => P.roles[r] || r;
  const why = {matched: 'matched automatically', keyword: 'from the name', default: 'deposits default to income'};
  const body = rows.map(p => {
    const current = p.role || '';
    const sel = cfwEl('select', {'aria-label': `Role for ${p.label}`, onchange: e => cfwSaveRole(p, e.target.value)},
      cfwEl('option', {value: '', selected: !current}, p.suggested ? `Suggested: ${roleName(p.suggested)}` : '— choose —'),
      ...Object.entries(P.roles).map(([k, v]) => cfwEl('option', {value: k, selected: current === k}, v)));
    const cell = cfwEl('div', {class: 'cfw-role-cell'}, sel);
    const effective = p.role || p.suggested;
    if (effective === 'spending' && p.role) {
      cell.append(cfwEl('select', {'aria-label': 'Category', onchange: e => cfwSaveRole(p, 'spending', {category: e.target.value})},
        cfwEl('option', {value: '', selected: !p.category}, 'Category: by payee'),
        ...P.categories.map(c => cfwEl('option', {value: c, selected: p.category === c}, c))));
    }
    if ((effective === 'debt' || effective === 'saving') && p.role) {
      cell.append(cfwEl('select', {'aria-label': 'Account', onchange: e => cfwSaveRole(p, p.role, {target: e.target.value})},
        cfwEl('option', {value: '', selected: !p.target}, 'Account: by payee'),
        ...P.accounts.map(a => cfwEl('option', {value: a, selected: p.target === a}, a))));
    }
    if (p.role) cell.append(cfwEl('button', {class: 'btn-sm', onclick: () => cfwClearRole(p)}, 'Reset'));
    const note = p.role ? 'set by you' : p.suggested ? (why[p.suggested_by] || '') + (p.mixed ? '; rows differ' : '') : 'no suggestion';
    return cfwEl('tr', {},
      cfwEl('td', {}, cfwEl('div', {}, p.label), cfwEl('div', {class: 'sub'}, p.example)),
      cfwEl('td', {class: 'num'}, p.count),
      cfwEl('td', {class: 'num'}, p.money_in ? cfwMoney(p.money_in) : ''),
      cfwEl('td', {class: 'num'}, p.money_out ? cfwMoney(p.money_out) : ''),
      cfwEl('td', {}, cell, cfwEl('div', {class: 'cfw-why' + (p.role ? '' : ' auto')}, note)));
  });
  document.getElementById('cfw-payees-table').replaceChildren(
    cfwEl('thead', {}, cfwEl('tr', {}, cfwEl('th', {}, 'Payee'), cfwEl('th', {class: 'num'}, 'Rows'),
      cfwEl('th', {class: 'num'}, 'In'), cfwEl('th', {class: 'num'}, 'Out'), cfwEl('th', {}, 'Role'))),
    cfwEl('tbody', {}, ...(body.length ? body : [cfwEl('tr', {}, cfwEl('td', {colspan: 5, class: 'sub', style: 'padding:16px'},
      CFW.pf.show === 'needs' && !q ? 'Every payee in this period has a role or a suggestion.' : 'No payees match.'))])));
}
async function cfwSaveRole(p, role, extra = {}) {
  const err = document.getElementById('cfw-pf-err');
  err.textContent = '';
  try {
    if (!role) { await cfwClearRole(p); return; }
    const body = {role, label: p.label, category: role === 'spending' ? (extra.category ?? p.category ?? '') : '',
                  target: (role === 'debt' || role === 'saving') ? (extra.target ?? p.target ?? '') : ''};
    const saved = await cfwApi('/api/cashflow/payees/' + encodeURIComponent(p.key),
      {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    Object.assign(p, {role: saved.role, category: saved.category, target: saved.target});
    CFW.touched.add(p.key);
    cfwRenderPayees();
  } catch (e) { err.textContent = e.message; }
}
async function cfwClearRole(p) {
  try {
    await cfwApi('/api/cashflow/payees/' + encodeURIComponent(p.key), {method: 'DELETE'});
    Object.assign(p, {role: null, category: null, target: null});
    CFW.touched.add(p.key);
    cfwRenderPayees();
  } catch (e) { document.getElementById('cfw-pf-err').textContent = e.message; }
}

// ── Wiring: load when the Cash Flow Analysis tab opens ─────────────────────────────────
(function cfwInit() {
  const orig = switchTab;
  switchTab = function (name, btn) {
    orig(name, btn);
    if (name === 'cashflow') cfwLoad();
  };
})();
