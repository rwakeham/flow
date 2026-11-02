// The Settings dialog (top bar → ⚙ Settings): AI, Backups, Updates and Account in one place,
// one section each. The sections' contents are rendered by their own code: ai.js (aiRender),
// index.html (loadBackupList, loadVersionInfo, the password form), changes.js (changesRender). Loaded after them.
// settingsOpen('backups') opens straight onto a section; the last one opened is remembered.

const SETTINGS_SECTIONS = [
  ['ai', 'AI', 'key, models, privacy log', () => aiRender()],
  ['backups', 'Backups', 'on this server: take, download, restore', () => loadBackupList()],
  ['updates', 'Updates', 'version and the daily check', () => loadVersionInfo()],
  ['changes', 'Change log', 'what changed, when, and by whom', () => changesRender()],
  ['account', 'Account', 'change the password', () => settingsResetPassword()],
];
const SETTINGS_REMEMBER = 'flow-settings-section';

function settingsInject() {
  if (document.getElementById('settings-modal')) return;
  const nav = SETTINGS_SECTIONS.map(([k, label, sub]) =>
    `<button type="button" data-section="${k}" onclick="settingsShow('${k}')">${label}<span class="sub">${sub}</span></button>`).join('');
  document.body.insertAdjacentHTML('beforeend', `
<div id="settings-modal" class="cd-modal hidden" role="dialog" aria-modal="true" aria-labelledby="settings-title"><div class="modal-box">
  <div class="settings-head"><h2 id="settings-title" style="flex:1">Settings</h2>
    <button class="btn-sm" onclick="settingsClose()" aria-label="Close">✕</button></div>
  <div class="settings-layout">
    <nav class="settings-nav" aria-label="Settings sections">${nav}</nav>
    <div class="settings-body">
      <section id="settings-ai" hidden><div id="ai-body"><div class="empty">Loading…</div></div></section>
      <section id="settings-backups" hidden>
        <div class="settings-row">
          <h3 style="flex:1">Backups</h3>
          <label style="cursor:pointer">
            <input type="file" id="backup-upload-input" accept=".zip,.flowbackup" style="display:none" onchange="doUploadBackup(this)">
            <span class="btn-sm settings-filebtn" onclick="document.getElementById('backup-upload-input').click()">↑ Upload</span>
          </label>
          <button class="btn-primary btn-sm" onclick="doCreateBackup()">+ New Backup</button>
        </div>
        <div id="backup-upload-pw" class="settings-row hidden" style="margin-top:6px"></div>
        <div id="backup-settings" class="cd-note" style="margin:6px 0 10px">Loading…</div>
        <div id="backup-list-wrap"><div class="empty" style="padding:30px 0">Loading…</div></div>
        <div class="settings-row" style="margin-top:8px">
          <button id="backup-delete-btn" onclick="doDeleteBackups()" style="color:var(--red);border-color:var(--red);display:none">Delete Selected</button>
        </div>
      </section>
      <section id="settings-updates" hidden>
        <h3>Updates</h3>
        <div id="app-version" class="cd-note">Loading…</div>
        <div class="settings-row" style="margin-top:10px">
          <button class="btn-sm" onclick="checkForUpdates()" title="Asks GitHub for Flow's newest release now; nothing about your data is sent">Check for updates</button>
          <span id="update-check-sub" class="cd-note"></span>
        </div>
        <p class="cd-note" style="margin-top:10px">Flow never installs an update itself: on the server, run <code>git pull &amp;&amp; ./deploy.sh</code>.</p>
      </section>
      <section id="settings-changes" hidden><h3>Change log</h3><div class="empty">Loading…</div></section>
      <section id="settings-account" hidden>
        <h3>Account</h3>
        <p class="cd-note">The dashboard password is kept on this server as a hash. Changing it ends every session, this one included.</p>
        <div class="settings-form">
          <label class="f">Current password</label><input type="password" id="pw-current" autocomplete="current-password">
          <label class="f" style="margin-top:8px">New password</label><input type="password" id="pw-new" autocomplete="new-password">
          <label class="f" style="margin-top:8px">New password again</label><input type="password" id="pw-new2" autocomplete="new-password">
          <div id="pw-err" class="err" style="margin-top:8px;min-height:16px"></div>
          <div class="settings-row"><button class="btn-primary btn-sm" onclick="doChangePassword()">Change password</button></div>
        </div>
      </section>
    </div>
  </div>
</div></div>`);
  document.getElementById('settings-modal').addEventListener('click', e => { if (e.target.id === 'settings-modal') settingsClose(); });
}

function settingsOpen(section) {
  settingsInject();
  let name = section;
  if (!name) { try { name = localStorage.getItem(SETTINGS_REMEMBER); } catch (e) { /* storage off */ } }
  if (!SETTINGS_SECTIONS.some(s => s[0] === name)) name = SETTINGS_SECTIONS[0][0];
  document.getElementById('settings-modal').classList.remove('hidden');
  settingsShow(name);
}

function settingsShow(name) {
  const entry = SETTINGS_SECTIONS.find(s => s[0] === name) || SETTINGS_SECTIONS[0];
  document.querySelectorAll('#settings-modal .settings-nav button').forEach(b => b.classList.toggle('active', b.dataset.section === entry[0]));
  document.querySelectorAll('#settings-modal .settings-body > section').forEach(s => { s.hidden = s.id !== 'settings-' + entry[0]; });
  try { localStorage.setItem(SETTINGS_REMEMBER, entry[0]); } catch (e) { /* storage off */ }
  entry[3]();
}

function settingsClose() {
  const m = document.getElementById('settings-modal');
  if (m) m.classList.add('hidden');
}

function settingsIsOpen() {
  const m = document.getElementById('settings-modal');
  return !!m && !m.classList.contains('hidden');
}

function settingsResetPassword() {
  for (const id of ['pw-current', 'pw-new', 'pw-new2']) document.getElementById(id).value = '';
  document.getElementById('pw-err').textContent = '';
  document.getElementById('pw-current').focus();
}

document.addEventListener('keydown', e => { if (e.key === 'Escape' && settingsIsOpen()) settingsClose(); });
