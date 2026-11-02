// The AI section of the Settings dialog (top bar → ⚙ Settings → AI): whether AI is on, which
// model each feature uses and where that comes from, and the AI privacy log (every request and
// response, in full). Every AI feature shares these: card categorization, the Assistant, insights
// and balance reading. Loaded after index.html's inline script and cards.js (it reuses cards.css's
// table and log styles); settings.js, loaded after it, owns the dialog and calls aiRender().

const AI = {log: []};
const aiH = v => escHtml(v === null || v === undefined ? '' : String(v));

// Kept for the callers that open the AI settings directly (the Cards page's banners, the Assistant).
function aiOpen() { settingsOpen('ai'); }
function aiClose() { settingsClose(); }

async function aiGet(url) {
  const res = await fetch(url);
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Request failed (' + res.status + ')');
  return d;
}
async function aiSend(url, method, body) {
  const res = await fetch(url, {method, headers: body === undefined ? {} : {'Content-Type': 'application/json'},
                                body: body === undefined ? undefined : JSON.stringify(body)});
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Request failed (' + res.status + ')');
  return d;
}

const AI_KNOWN_MODELS = ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5-5'];

// A setting still set in .env wins while it is there: shown, not editable, with the way out.
function aiEnvNote(s) {
  return s.source === 'env' ? `<div class="ai-env">Set by <code>${aiH(s.env_var)}</code> in .env, which wins while it is there.
    Remove it from .env (<code>./setup.sh</code> offers to) and run <code>./deploy.sh</code> to manage it here.</div>` : '';
}

async function aiRender() {
  const body = document.getElementById('ai-body');
  let status, log;
  try {
    [status, log] = await Promise.all([aiGet('/api/ai/status'), aiGet('/api/ai/log?limit=50')]);
  } catch (e) { body.textContent = e.message; return; }
  AI.log = log;
  AI.status = status;
  const st = status.settings, key = st.api_key, locked = k => st[k].source === 'env' ? 'disabled' : '';
  const isOn = s => !(s.value && ['0', 'false', 'no', 'off'].includes(s.value.toLowerCase()));
  const areaRow = (k, label, what) => `<label class="cd-check ai-area"><input type="checkbox" id="ai-${k}" ${isOn(st[k]) ? 'checked' : ''} ${locked(k)}>
    <span><strong>${label}</strong>: ${what}</span></label>${aiEnvNote(st[k])}`;
  const modelField = (k, label, blank) => `<label class="f">${label}</label>
    <input type="text" id="ai-${k}" list="ai-models" value="${aiH(st[k].value)}" placeholder="${aiH(blank)}" ${locked(k)}
      autocomplete="off" spellcheck="false">${aiEnvNote(st[k])}`;
  const every = st.ai_model.value || status.default_model;
  const models = status.models.map(m => `<tr><td>${aiH(m.feature)}</td><td><strong>${aiH(m.model)}</strong></td>
    <td class="cd-note">${m.source === 'env' ? `${aiH(m.env_var)} in .env` : m.source === 'flow' ? 'set here' : "Flow's default"}</td></tr>`).join('');
  body.innerHTML = `
    <div class="cd-card"><div class="cd-card-h">Status</div><div class="cd-card-b cd-note">
      ${status.enabled ? '<strong>On.</strong>' : `<strong>Off.</strong> ${aiH(status.reason)}`}
      ${status.problem ? `<br><span style="color:var(--red)">⚠ Paused since ${new Date(status.problem.at).toLocaleString()}: ${aiH(status.problem.message)}
        Automatic AI calls stop until the key below is replaced (or tested and found working).</span>` : ''}
      <br>Card categorization, the Assistant, spending insights and reading pasted balances use the Anthropic API with
      your own key. Without a key nothing is sent. What each sends is limited by design, and every
      request is recorded below.
    </div></div>
    <div class="cd-card"><div class="cd-card-h">Anthropic API key</div><div class="cd-card-b">
      <div class="cd-note" style="margin-bottom:6px">${key.unreadable ? '<span style="color:var(--red)">A key is saved but can\'t be read: SECRET_KEY changed since it was saved. Enter it again.</span>'
        : key.set ? `A key ending in <strong>…${aiH(key.last4)}</strong> is ${key.source === 'env' ? 'set by ANTHROPIC_API_KEY in .env' : 'stored in Flow, encrypted'}.`
        : 'No key. Get one at console.anthropic.com; it is stored encrypted on this server and never sent anywhere but the Anthropic API.'}</div>
      ${aiEnvNote(key)}
      <div class="ai-row"><input type="password" id="ai-key" placeholder="${key.set ? 'Replace the key…' : 'sk-ant-…'}" ${locked('api_key')} autocomplete="off" spellcheck="false">
        <button class="btn-sm" onclick="aiSaveKey()" ${locked('api_key')}>Save key</button>
        <button class="btn-sm" onclick="aiTestKey()" ${key.set ? '' : 'disabled'}>Test the key</button>
        ${key.set && key.source !== 'env' ? '<button class="btn-sm" onclick="aiRemoveKey()">Remove</button>' : ''}</div>
      <div id="ai-key-msg" class="cd-note"></div>
    </div></div>
    <div class="cd-card"><div class="cd-card-h">Settings</div><div class="cd-card-b">
      <label class="cd-check"><input type="checkbox" id="ai-ai_enabled" ${isOn(st.ai_enabled) ? 'checked' : ''} ${locked('ai_enabled')}>
        AI calls on (off keeps the key but sends nothing)</label>${aiEnvNote(st.ai_enabled)}
      <div class="ai-areas" id="ai-areas">
        <div class="cd-note">What the AI may see. Switch an area off and nothing of it is sent: the feature that would
          send it says so, and the Assistant tells you when a question needs it.</div>
        ${areaRow('ai_cards', 'Card spending', 'merchant categorization, spending insights and Assistant questions about cards')}
        ${areaRow('ai_cash', 'Cash accounts', 'Assistant questions about the cash register and cash flow')}
        ${areaRow('ai_balances', 'Balance sheet', 'reading pasted balances with AI, and Assistant questions about net worth')}
      </div>
      <datalist id="ai-models">${AI_KNOWN_MODELS.map(m => `<option value="${m}">`).join('')}</datalist>
      ${modelField('ai_model', 'Model for every feature', "Flow's default: " + status.default_model)}
      ${modelField('ai_model_categorize', 'Model for merchant categorization only (a cheaper one such as claude-haiku-5-5 works well)', 'same as every feature: ' + every)}
      ${modelField('ai_model_ask', 'Model for the Assistant only', 'same as every feature: ' + every)}
      <label class="f">Extra words to strip from merchant names before any AI call, comma-separated (family names printed in descriptions, for example)</label>
      <input type="text" id="ai-ai_redact" value="${aiH(st.ai_redact.value)}" placeholder="none" ${locked('ai_redact')} autocomplete="off">${aiEnvNote(st.ai_redact)}
      <div class="ai-row"><button class="btn-primary btn-sm" onclick="aiSaveSettings()">Save settings</button><span id="ai-set-msg" class="cd-note"></span></div>
    </div></div>
    <div class="cd-card"><div class="cd-card-h">Models in use</div>
      <div class="cd-scroll"><table class="cd-table"><thead><tr><th>Used for</th><th>Model</th><th>Set by</th></tr></thead>
        <tbody>${models}</tbody></table></div></div>
    <div class="cd-card"><div class="cd-card-h">AI privacy log (every request and response, exactly as sent and received)</div>
      ${log.length ? `<div class="cd-scroll"><table class="cd-table"><thead><tr><th>When</th><th>Purpose</th><th>Model</th>
        <th class="num">Items</th><th class="num">Tokens in / out</th><th>Status</th><th></th></tr></thead><tbody>
      ${log.map((l, i) => `<tr><td>${new Date(l.created_at).toLocaleString()}</td><td>${aiH(l.purpose)}</td><td>${aiH(l.model)}</td>
        <td class="num">${l.item_count}</td><td class="num">${l.input_tokens ?? '—'} / ${l.output_tokens ?? '—'}</td>
        <td>${aiH(l.status)}${l.error ? '<div class="raw">' + aiH(l.error) + '</div>' : ''}</td>
        <td><button class="btn-sm" id="ai-log-btn-${i}" onclick="aiToggleLog(${i})">Conversation ▸</button></td></tr>
        <tr id="ai-log-${i}" class="hidden cd-log-row"><td colspan="7"></td></tr>`).join('')}
      </tbody></table></div>` : '<div class="cd-card-b cd-note">Nothing has been sent.</div>'}</div>`;
  // The area switches mean nothing while the global switch is off: dim them.
  const global = document.getElementById('ai-ai_enabled');
  const dim = () => document.getElementById('ai-areas').classList.toggle('dim', !global.checked);
  global.addEventListener('change', dim);
  dim();
}

// ── Settings and the key ─────────────────────────────────────────────────────
// The Cards page's AI banner and the Assistant panel read the same settings: refresh them after a change.
function aiRefreshCards() {
  if (typeof cdPollAi === 'function' && document.getElementById('cd-ai-banner')) cdPollAi();
  if (typeof askRefreshStatus === 'function') askRefreshStatus();
}

function aiMsg(id, text, bad) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.style.color = bad ? 'var(--red)' : '';
}

async function aiSaveSettings() {
  const v = id => document.getElementById('ai-' + id).value;
  const on = id => document.getElementById('ai-' + id).checked;
  const body = {ai_model: v('ai_model'), ai_model_categorize: v('ai_model_categorize'), ai_model_ask: v('ai_model_ask'),
                ai_enabled: on('ai_enabled'), ai_cards: on('ai_cards'), ai_cash: on('ai_cash'), ai_balances: on('ai_balances'),
                ai_redact: v('ai_redact')};
  for (const k of Object.keys(body)) if (AI.status.settings[k].source === 'env') delete body[k];   // .env wins: not sent
  try { await aiSend('/api/ai/settings', 'PUT', body); }
  catch (e) { aiMsg('ai-set-msg', e.message, true); return; }
  await aiRender();
  aiMsg('ai-set-msg', 'Saved.');
  aiRefreshCards();
}

async function aiSaveKey() {
  const key = document.getElementById('ai-key').value.trim();
  if (!key) { aiMsg('ai-key-msg', 'Paste the key first.', true); return; }
  try { await aiSend('/api/ai/key', 'PUT', {key}); }
  catch (e) { aiMsg('ai-key-msg', e.message, true); return; }
  await aiRender();
  aiMsg('ai-key-msg', 'Key saved. Test it to be sure it works.');
  aiRefreshCards();
}

async function aiRemoveKey() {
  if (!confirm('Remove the API key? AI features turn off until a key is entered again.')) return;
  try { await aiSend('/api/ai/key', 'DELETE'); }
  catch (e) { aiMsg('ai-key-msg', e.message, true); return; }
  await aiRender();
  aiRefreshCards();
}

async function aiTestKey() {
  aiMsg('ai-key-msg', 'Testing…');
  let d;
  try { d = await aiSend('/api/ai/key/test', 'POST'); }
  catch (e) { aiMsg('ai-key-msg', e.message, true); return; }
  await aiRender();
  aiMsg('ai-key-msg', d.message, !d.ok);
  aiRefreshCards();
}

// ── AI privacy log: one request and its response, read as a conversation ─────
function aiToggleLog(i) {
  const row = document.getElementById('ai-log-' + i), btn = document.getElementById('ai-log-btn-' + i);
  const open = row.classList.toggle('hidden') === false;
  btn.textContent = open ? 'Conversation ▾' : 'Conversation ▸';
  if (open && !row.dataset.done) { row.firstElementChild.innerHTML = aiLogConversation(AI.log[i]); row.dataset.done = '1'; }
}
function aiLogJson(v) {
  // Strings that hold JSON (Flow sends its data that way) are shown pretty-printed.
  if (typeof v === 'string') { try { const p = JSON.parse(v); if (p && typeof p === 'object') v = p; } catch (e) { return v; } }
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2);
}
function aiLogText(v) {
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.map(b => b && b.type === 'text' ? b.text : JSON.stringify(b, null, 2)).join('\n\n');
  return JSON.stringify(v, null, 2);
}
function aiLogBlock(b) {
  if (typeof b === 'string') { const s = aiLogJson(b); return s === b ? `<div class="cd-log-text">${aiH(b)}</div>` : `<div class="cd-pre">${aiH(s)}</div>`; }
  if (b.type === 'text') return `<div class="cd-log-text">${aiH(b.text)}</div>`;
  if (b.type === 'tool_use') return `<div class="cd-log-tag">Calls <strong>${aiH(b.name)}</strong> <span class="cd-muted">${aiH(b.id || '')}</span></div><div class="cd-pre">${aiH(aiLogJson(b.input))}</div>`;
  if (b.type === 'tool_result') return `<div class="cd-log-tag">Result <span class="cd-muted">${aiH(b.tool_use_id || '')}</span>${b.is_error ? ' <span style="color:var(--red)">error</span>' : ''}</div>
    <div class="cd-pre">${aiH(Array.isArray(b.content) ? b.content.map(c => c.type === 'text' ? aiLogJson(c.text) : JSON.stringify(c, null, 2)).join('\n\n') : aiLogJson(b.content ?? ''))}</div>`;
  if (b.type === 'thinking') return `<details class="cd-details"><summary>Thinking</summary><div class="cd-pre">${aiH(b.thinking || '')}</div></details>`;
  return `<div class="cd-log-tag">${aiH(b.type || 'block')}</div><div class="cd-pre">${aiH(JSON.stringify(b, null, 2))}</div>`;
}
function aiLogMessage(who, cls, content) {
  const blocks = typeof content === 'string' ? [content] : (content || []);
  return `<div class="cd-log-msg ${cls}"><div class="cd-log-who">${who}</div>${blocks.map(aiLogBlock).join('')}</div>`;
}
function aiLogConversation(l) {
  const req = l.request || {}, res = l.response;
  const settings = [req.max_tokens && `max ${req.max_tokens} tokens`, req.output_config && req.output_config.effort && `effort ${req.output_config.effort}`,
    req.tool_choice && `tool choice ${req.tool_choice.type || req.tool_choice}`, req.output_format && 'structured output'].filter(Boolean);
  const tools = req.tools || [];
  let html = `<div class="cd-log">`;
  if (!l.request) html += `<div class="cd-note">The full request was not recorded for this entry (it predates request logging). The data sent is below.</div>`;
  if (req.system) html += `<details class="cd-details"><summary>System prompt (instructions from Flow)</summary><div class="cd-pre">${aiH(aiLogText(req.system))}</div></details>`;
  if (tools.length) html += `<details class="cd-details"><summary>Tools offered (${tools.length}): ${aiH(tools.map(t => t.name).join(', '))}</summary>
    ${tools.map(t => `<div class="cd-log-tag"><strong>${aiH(t.name)}</strong></div><div class="cd-log-text cd-note">${aiH(t.description || '')}</div><div class="cd-pre">${aiH(JSON.stringify(t.input_schema || {}, null, 2))}</div>`).join('')}</details>`;
  if (req.output_format) html += `<details class="cd-details"><summary>Answer format required</summary><div class="cd-pre">${aiH(JSON.stringify(req.output_format, null, 2))}</div></details>`;
  if (settings.length) html += `<div class="cd-note" style="margin:4px 0 8px">${aiH(settings.join(' · '))}</div>`;
  for (const m of req.messages || []) {
    html += m.role === 'assistant' ? aiLogMessage('Claude (earlier in this exchange)', 'claude', m.content) : aiLogMessage('Flow sent', 'flow', m.content);
  }
  if (!l.request && l.payload != null) html += aiLogMessage('Flow sent', 'flow', [JSON.stringify(l.payload)]);
  if (res) {
    const blocks = res.content || (res.parsed_output !== undefined ? [JSON.stringify(res.parsed_output)] : [JSON.stringify(res)]);
    const u = res.usage || {};
    const usage = [u.input_tokens != null && `${u.input_tokens} input`, u.cache_read_input_tokens && `${u.cache_read_input_tokens} cached input`,
      u.cache_creation_input_tokens && `${u.cache_creation_input_tokens} written to cache`, u.output_tokens != null && `${u.output_tokens} output`].filter(Boolean);
    html += aiLogMessage('Claude replied', 'claude', blocks);
    html += `<div class="cd-note" style="margin-top:4px">${aiH([res.stop_reason && `Stopped: ${res.stop_reason}`, usage.length && `Tokens: ${usage.join(', ')}`].filter(Boolean).join(' · '))}</div>`;
  } else {
    html += `<div class="cd-log-msg error"><div class="cd-log-who">No reply</div><div class="cd-log-text">${aiH(l.error || 'Nothing was received.')}</div></div>`;
  }
  html += `<details class="cd-details" style="margin-top:8px"><summary>Exact JSON (data extracted, request, response)</summary>
    <div class="cd-log-tag">Data extracted from Flow</div><div class="cd-pre">${aiH(JSON.stringify(l.payload, null, 2))}</div>
    <div class="cd-log-tag">Request</div><div class="cd-pre">${aiH(l.request ? JSON.stringify(l.request, null, 2) : 'not recorded')}</div>
    <div class="cd-log-tag">Response</div><div class="cd-pre">${aiH(res ? JSON.stringify(res, null, 2) : 'none')}</div></details>`;
  return html + '</div>';
}

