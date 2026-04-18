// Load from URL params if coming from app.alterus.io
const params = new URLSearchParams(window.location.search);
if (params.get('name') || params.get('email')) {
  const config = {
    name: params.get('name') || '',
    email: params.get('email') || '',
    role: '',
    company: '',
    stakeholders: [],
  };
  chrome.storage.local.set({ 
    userConfig: config,
    alterusApiUrl: 'https://alterus.onrender.com'
  });
  document.getElementById('name').value = config.name;
  document.getElementById('email').value = config.email;
  document.getElementById('savedMsg').textContent = '✓ Connected from app.alterus.io!';
}

// Load saved values
chrome.storage.local.get(['userConfig','alterusApiUrl',
  'gmailConnected','slackConnected','gmailHistory',
  'outlookHistory','teamsHistory'], d => {

  if (d.userConfig) {
    document.getElementById('name').value         = d.userConfig.name || '';
    document.getElementById('email').value        = d.userConfig.email || '';
    document.getElementById('role').value         = d.userConfig.role || '';
    document.getElementById('company').value      = d.userConfig.company || '';
    document.getElementById('stakeholders').value = (d.userConfig.stakeholders || []).join(', ');
  }

  document.getElementById('apiUrl').value =
    d.alterusApiUrl || 'https://alterus.onrender.com';

  if (d.gmailConnected) setBadge('gmailBadge', 'Connected', true);
  if (d.slackConnected) setBadge('slackBadge', 'Connected', true);

  const oc = (d.outlookHistory || []).length;
  const tc = (d.teamsHistory || []).length;
  if (oc > 0) setBadge('outlookBadge', `${oc} emails`, true);
  if (tc > 0) setBadge('teamsBadge',   `${tc} messages`, true);
});

// Save profile
document.getElementById('saveBtn').onclick = () => {
  const config = {
    name:         document.getElementById('name').value.trim(),
    email:        document.getElementById('email').value.trim(),
    role:         document.getElementById('role').value.trim(),
    company:      document.getElementById('company').value.trim(),
    stakeholders: document.getElementById('stakeholders').value
                    .split(',').map(s => s.trim()).filter(Boolean),
  };
  const apiUrl = document.getElementById('apiUrl').value.trim();
  chrome.storage.local.set({ userConfig: config, alterusApiUrl: apiUrl }, () => {
    show('savedMsg', '✓ Saved!');
  });
};

// Test connection
document.getElementById('testBtn').onclick = () => {
  const url = document.getElementById('apiUrl').value.trim();
  chrome.storage.local.set({ alterusApiUrl: url });
  document.getElementById('testMsg').textContent = '⏳ Testing...';
  chrome.runtime.sendMessage({ action: 'testConnection' }, res => {
    if (res?.ok) show('testMsg', '✅ Connected!');
    else show('testMsg', `❌ Failed: ${res?.error || 'unreachable'}`);
  });
};

function setBadge(id, text, on) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className   = `badge ${on ? 'on' : 'off'}`;
}

function show(id, msg) {
  const el = document.getElementById(id);
  el.textContent = msg;
  setTimeout(() => { el.textContent = ''; }, 3000);
}
