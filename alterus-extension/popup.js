/**
 * popup.js
 * FIX v1.0.1:
 *   - Auto-tests connection on popup open so users see status immediately
 *   - Shows setup reminder if name/email not filled in
 *   - Saves API URL default on first install
 */

const DEFAULT_API = 'https://alterus.onrender.com';

// Load from URL params if redirected from app.alterus.io
const params = new URLSearchParams(window.location.search);
if (params.get('name') || params.get('email')) {
  const config = {
    name:         params.get('name')  || '',
    email:        params.get('email') || '',
    role:         '',
    company:      '',
    stakeholders: [],
  };
  chrome.storage.local.set({
    userConfig:    config,
    alterusApiUrl: DEFAULT_API,
  });
  document.getElementById('name').value    = config.name;
  document.getElementById('email').value   = config.email;
  document.getElementById('savedMsg').textContent = '✓ Connected from app.alterus.io!';
}

// Load saved config on popup open
chrome.storage.local.get(
  ['userConfig', 'alterusApiUrl', 'gmailConnected', 'slackConnected',
   'gmailHistory', 'outlookHistory', 'teamsHistory'],
  d => {
    if (d.userConfig) {
      document.getElementById('name').value         = d.userConfig.name    || '';
      document.getElementById('email').value        = d.userConfig.email   || '';
      document.getElementById('role').value         = d.userConfig.role    || '';
      document.getElementById('company').value      = d.userConfig.company || '';
      document.getElementById('stakeholders').value = (d.userConfig.stakeholders || []).join(', ');
    }

    // Set API URL — ensure default is always saved on first install
    const apiUrl = d.alterusApiUrl || DEFAULT_API;
    document.getElementById('apiUrl').value = apiUrl;
    if (!d.alterusApiUrl) {
      chrome.storage.local.set({ alterusApiUrl: DEFAULT_API });
    }

    // Badges
    if (d.gmailConnected) setBadge('gmailBadge', 'Connected', true);
    if (d.slackConnected) setBadge('slackBadge', 'Connected', true);

    const oc = (d.outlookHistory || []).length;
    const tc = (d.teamsHistory   || []).length;
    if (oc > 0) setBadge('outlookBadge', `${oc} emails`,   true);
    if (tc > 0) setBadge('teamsBadge',   `${tc} messages`, true);

    // Show setup reminder if profile is empty
    const name  = d.userConfig?.name  || '';
    const email = d.userConfig?.email || '';
    if (!name || !email) {
      const reminder = document.getElementById('setupReminder');
      if (reminder) {
        reminder.style.display = 'block';
        reminder.textContent   = '⚠️ Fill in your name and email above — needed for drafts to work.';
      }
    }

    // Auto-test connection on open
    autoTestConnection(apiUrl);
  }
);

// Auto-test: shows connection status without user clicking Test
function autoTestConnection(apiUrl) {
  const el = document.getElementById('testMsg');
  if (!el) return;
  el.textContent = '⏳ Checking connection...';
  fetch(`${apiUrl}/api/health`, { signal: AbortSignal.timeout(8000) })
    .then(r => r.json())
    .then(d => {
      if (d.status === 'ok') {
        el.textContent = '✅ Connected to Alterus';
        el.style.color  = '#22c55e';
      } else {
        el.textContent = '⚠️ Unexpected response';
        el.style.color  = '#f59e0b';
      }
    })
    .catch(e => {
      el.textContent = `❌ Cannot reach ${apiUrl}`;
      el.style.color  = '#ef4444';
    });
}

// Save profile
document.getElementById('saveBtn').onclick = () => {
  const name  = document.getElementById('name').value.trim();
  const email = document.getElementById('email').value.trim();

  if (!name || !email) {
    show('savedMsg', '⚠️ Name and email are required');
    return;
  }

  const config = {
    name,
    email,
    role:         document.getElementById('role').value.trim(),
    company:      document.getElementById('company').value.trim(),
    stakeholders: document.getElementById('stakeholders').value
                    .split(',').map(s => s.trim()).filter(Boolean),
  };
  const apiUrl = document.getElementById('apiUrl').value.trim() || DEFAULT_API;
  chrome.storage.local.set({ userConfig: config, alterusApiUrl: apiUrl }, () => {
    show('savedMsg', '✓ Saved!');
    // Re-test connection after saving in case API URL changed
    autoTestConnection(apiUrl);
  });
};

// Manual test button
document.getElementById('testBtn').onclick = () => {
  const url = document.getElementById('apiUrl').value.trim() || DEFAULT_API;
  chrome.storage.local.set({ alterusApiUrl: url });
  const el = document.getElementById('testMsg');
  el.textContent = '⏳ Testing...';
  el.style.color  = '';
  chrome.runtime.sendMessage({ action: 'testConnection' }, res => {
    if (res?.ok) {
      el.textContent = '✅ Connected!';
      el.style.color  = '#22c55e';
    } else {
      el.textContent = `❌ Failed: ${res?.error || 'unreachable'}`;
      el.style.color  = '#ef4444';
    }
  });
};

function setBadge(id, text, on) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className   = `badge ${on ? 'on' : 'off'}`;
}

function show(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  setTimeout(() => { el.textContent = ''; }, 3000);
}
