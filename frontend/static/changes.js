// Settings → Change log: what changed, when, and whether you, the Assistant, a schedule or startup did
// it (GET /api/changes, newest first, a page at a time). Rendered into #settings-changes by settings.js.

const CHANGES = {items: [], more: false};
const CHANGES_VIA = {ui: 'You', assistant: 'Assistant', schedule: 'Schedule', startup: 'Startup'};

async function changesRender(more = false) {
  const el = document.getElementById('settings-changes');
  if (!el) return;
  if (!more) { CHANGES.items = []; el.innerHTML = '<h3>Change log</h3><div class="empty">Loading…</div>'; }
  const before = more && CHANGES.items.length ? '&before=' + CHANGES.items[CHANGES.items.length - 1].id : '';
  let d;
  try {
    const res = await fetch('/api/changes?limit=100' + before);
    if (!res.ok) throw new Error('Could not load the change log (' + res.status + ').');
    d = await res.json();
  } catch (e) { el.innerHTML = '<h3>Change log</h3><div class="empty">' + escHtml(e.message) + '</div>'; return; }
  CHANGES.items = CHANGES.items.concat(d.items || []);
  CHANGES.more = !!d.more;
  const rows = CHANGES.items.map((c, i) => `<tr>
      <td style="white-space:nowrap">${escHtml(c.created_at ? new Date(c.created_at).toLocaleString() : '')}</td>
      <td><span class="changes-tag">${escHtml(String(c.kind))} · ${escHtml(String(c.action))}</span> ${escHtml(String(c.summary))}</td>
      <td>${escHtml(CHANGES_VIA[c.via] || String(c.via))}</td>
      <td>${c.detail ? `<button class="btn-sm" id="changes-btn-${i}" onclick="changesToggle(${i})">Details ▸</button>` : ''}</td></tr>
    <tr id="changes-row-${i}" class="hidden cd-log-row"><td colspan="4"></td></tr>`).join('');
  el.innerHTML = `<h3>Change log</h3>
    <p class="cd-note">Every change to your data: imports, edits, restores, rules and settings, and the Assistant's proposals you applied. Kept on this server only (not in backups), the newest 20,000.</p>
    ${CHANGES.items.length ? `<div class="cd-scroll"><table class="cd-table changes-table"><thead><tr><th>When</th><th>What</th><th>By</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : '<div class="empty">Nothing has changed yet.</div>'}
    ${CHANGES.more ? '<div class="settings-row" style="margin-top:8px"><button class="btn-sm" onclick="changesRender(true)">Load more</button></div>' : ''}`;
}

function changesToggle(i) {
  const row = document.getElementById('changes-row-' + i), btn = document.getElementById('changes-btn-' + i);
  const open = row.classList.toggle('hidden') === false;
  btn.textContent = open ? 'Details ▾' : 'Details ▸';
  if (open && !row.dataset.done) {
    row.firstElementChild.innerHTML = '<div class="cd-pre">' + escHtml(JSON.stringify(CHANGES.items[i].detail, null, 2)) + '</div>';
    row.dataset.done = '1';
  }
}
