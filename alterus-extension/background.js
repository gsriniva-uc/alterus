/**
 * background.js
 * Handles Gmail OAuth, Slack OAuth, history fetching.
 * ALTERUS_API is set from storage — updated after deployment.
 */

const SLACK_CLIENT_ID = 'YOUR_SLACK_CLIENT_ID'; // update after Slack app created

// ── Get API URL from storage ──────────────────────────────────────────────────
async function getApiUrl() {
  const data = await chromeGet('alterusApiUrl');
  return data.alterusApiUrl || 'https://alterus.onrender.com';
}

// ── Message handler ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  switch(msg.action) {
    case 'connectGmail':     connectGmail(reply);       return true;
    case 'fetchGmailHistory': fetchGmailHistory();      reply({ok:true}); return true;
    case 'connectSlack':     connectSlack(reply);       return true;
    case 'getStatus':
      chromeGet(['gmailConnected','slackConnected','gmailHistory',
                 'slackHistory','outlookHistory','teamsHistory'])
        .then(reply);
      return true;
    case 'testConnection':
      getApiUrl().then(url =>
        fetch(`${url}/api/health`)
          .then(r => r.json())
          .then(d => reply({ok: true, data: d}))
          .catch(e => reply({ok: false, error: e.message}))
      );
      return true;
  }
});

// ── Gmail OAuth ───────────────────────────────────────────────────────────────
function connectGmail(reply) {
  chrome.identity.getAuthToken({ interactive: true }, token => {
    if (chrome.runtime.lastError || !token) {
      reply({ success: false, error: chrome.runtime.lastError?.message });
      return;
    }
    chrome.storage.local.set({ gmailToken: token, gmailConnected: true });
    reply({ success: true });
    fetchGmailHistory(token);
  });
}

async function fetchGmailHistory(token = null) {
  if (!token) {
    const d = await chromeGet('gmailToken');
    token = d.gmailToken;
    if (!token) return;
  }

  const ALTERUS_API = await getApiUrl();
  console.log('📧 Fetching Gmail sent history...');

  try {
    const listRes  = await fetch(
      'https://gmail.googleapis.com/gmail/v1/users/me/messages?labelIds=SENT&maxResults=80',
      { headers: { Authorization: `Bearer ${token}` } }
    );
    const listData = await listRes.json();
    const messages = listData.messages || [];

    const emails = [];
    for (let i = 0; i < Math.min(messages.length, 60); i++) {
      try {
        const msgRes = await fetch(
          `https://gmail.googleapis.com/gmail/v1/users/me/messages/${messages[i].id}?format=full`,
          { headers: { Authorization: `Bearer ${token}` } }
        );
        const msg     = await msgRes.json();
        const subject = getHeader(msg, 'Subject') || '';
        const to      = getHeader(msg, 'To') || '';
        const body    = extractBody(msg);
        if (body.length > 30) {
          emails.push({ subject, to, body: body.slice(0, 600), platform: 'gmail' });
        }
        if (i % 10 === 9) await sleep(400);
      } catch(e) { continue; }
    }

    if (emails.length > 0) {
      const config = await chromeGet('userConfig');
      await fetch(`${ALTERUS_API}/api/ingest-history`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          platform:   'gmail',
          items:      emails,
          user_name:  config.userConfig?.name  || '',
          user_email: config.userConfig?.email || '',
        }),
      });
    }

    chrome.storage.local.set({ gmailHistory: emails });
    console.log(`✅ Gmail: ${emails.length} emails sent to corpus`);

  } catch(e) {
    console.error('Gmail history error:', e);
  }
}

// ── Slack OAuth ───────────────────────────────────────────────────────────────
function connectSlack(reply) {
  const redirectUri = chrome.identity.getRedirectURL('slack');
  const scope       = 'channels:history,im:history,users:read';
  const authUrl     = `https://slack.com/oauth/v2/authorize?client_id=${SLACK_CLIENT_ID}&user_scope=${scope}&redirect_uri=${encodeURIComponent(redirectUri)}`;

  chrome.identity.launchWebAuthFlow({ url: authUrl, interactive: true }, async url => {
    if (chrome.runtime.lastError || !url) { reply({ success: false }); return; }

    const params = new URLSearchParams(new URL(url).search);
    const code   = params.get('code');

    if (code) {
      chrome.storage.local.set({ slackCode: code, slackConnected: true });
      reply({ success: true });
    } else {
      reply({ success: false });
    }
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function getHeader(msg, name) {
  return (msg.payload?.headers || []).find(h => h.name === name)?.value || '';
}

function extractBody(msg) {
  try {
    const parts = msg.payload?.parts || [msg.payload];
    for (const p of parts) {
      if (p?.mimeType === 'text/plain' && p?.body?.data) {
        return atob(p.body.data.replace(/-/g,'+').replace(/_/g,'/'));
      }
    }
    return msg.snippet || '';
  } catch(e) { return msg.snippet || ''; }
}

function chromeGet(key) {
  return new Promise(r => chrome.storage.local.get(key, r));
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
