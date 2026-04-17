/**
 * background.js
 * Service worker — handles:
 *   - Gmail OAuth (chrome.identity)
 *   - Slack OAuth (opens popup window)
 *   - Gmail history fetch
 *   - Sending history to Alterus API
 */

const ALTERUS_API  = 'https://YOUR_RAILWAY_APP.up.railway.app';
const SLACK_CLIENT_ID = 'YOUR_SLACK_CLIENT_ID';

// ── Message handler ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch(message.action) {

    case 'connectGmail':
      connectGmail(sendResponse);
      return true; // keep message channel open for async

    case 'fetchGmailHistory':
      fetchGmailHistory();
      sendResponse({ success: true });
      return true;

    case 'connectSlack':
      connectSlack(sendResponse);
      return true;

    case 'getStatus':
      chrome.storage.local.get(
        ['gmailConnected','slackConnected','historyCount'],
        data => sendResponse(data)
      );
      return true;
  }
});

// ── Gmail OAuth ───────────────────────────────────────────────────────────────
function connectGmail(sendResponse) {
  chrome.identity.getAuthToken({ interactive: true }, token => {
    if (chrome.runtime.lastError || !token) {
      console.error('Gmail auth failed:', chrome.runtime.lastError);
      sendResponse({ success: false, error: chrome.runtime.lastError?.message });
      return;
    }

    // Store token and mark as connected
    chrome.storage.local.set({
      gmailToken:    token,
      gmailConnected: true,
    });

    sendResponse({ success: true, token });

    // Auto-fetch history after connecting
    fetchGmailHistory(token);
  });
}

// ── Fetch Gmail sent mail history ─────────────────────────────────────────────
async function fetchGmailHistory(token = null) {
  // Get token from storage if not passed
  if (!token) {
    const data = await chromeStorageGet('gmailToken');
    token = data.gmailToken;
    if (!token) return;
  }

  console.log('📧 Fetching Gmail sent mail history...');

  try {
    // Get list of sent messages (last 100)
    const listRes = await fetch(
      'https://gmail.googleapis.com/gmail/v1/users/me/messages?labelIds=SENT&maxResults=100',
      { headers: { Authorization: `Bearer ${token}` } }
    );
    const listData = await listRes.json();
    const messages = listData.messages || [];

    console.log(`📧 Found ${messages.length} sent messages`);

    // Fetch each message (batch to avoid rate limits)
    const emails = [];
    for (let i = 0; i < Math.min(messages.length, 80); i++) {
      try {
        const msgRes = await fetch(
          `https://gmail.googleapis.com/gmail/v1/users/me/messages/${messages[i].id}?format=full`,
          { headers: { Authorization: `Bearer ${token}` } }
        );
        const msg = await msgRes.json();

        const subject = getHeader(msg, 'Subject') || '(no subject)';
        const to      = getHeader(msg, 'To') || '';
        const date    = getHeader(msg, 'Date') || '';
        const body    = extractBody(msg);

        if (body.length > 30) {
          emails.push({ subject, to, date, body: body.slice(0, 800), platform: 'gmail' });
        }

        // Small delay to respect rate limits
        if (i % 10 === 9) await sleep(500);

      } catch(e) {
        continue;
      }
    }

    console.log(`📧 Extracted ${emails.length} emails`);

    // Send to Alterus API for ingestion
    if (emails.length > 0) {
      await sendHistoryToAPI(emails, 'gmail');
    }

    chrome.storage.local.set({ gmailHistory: emails, historyCount: emails.length });

  } catch(e) {
    console.error('Gmail history fetch failed:', e);
  }
}

function getHeader(msg, name) {
  const headers = msg.payload?.headers || [];
  return headers.find(h => h.name === name)?.value || '';
}

function extractBody(msg) {
  try {
    // Try plain text first
    const parts = msg.payload?.parts || [msg.payload];
    for (const part of parts) {
      if (part?.mimeType === 'text/plain' && part?.body?.data) {
        return atob(part.body.data.replace(/-/g, '+').replace(/_/g, '/'));
      }
    }
    // Fallback: decode snippet
    return msg.snippet || '';
  } catch(e) {
    return msg.snippet || '';
  }
}

// ── Slack OAuth ───────────────────────────────────────────────────────────────
function connectSlack(sendResponse) {
  const redirectUri  = chrome.identity.getRedirectURL('slack');
  const scope        = 'channels:history,im:history,mpim:history,groups:history,users:read';
  const authUrl      = `https://slack.com/oauth/v2/authorize?client_id=${SLACK_CLIENT_ID}&scope=${scope}&redirect_uri=${encodeURIComponent(redirectUri)}&response_type=token`;

  chrome.identity.launchWebAuthFlow(
    { url: authUrl, interactive: true },
    async redirectUrl => {
      if (chrome.runtime.lastError || !redirectUrl) {
        sendResponse({ success: false });
        return;
      }

      // Extract token from redirect URL
      const params = new URLSearchParams(new URL(redirectUrl).hash.slice(1));
      const token  = params.get('access_token');

      if (token) {
        chrome.storage.local.set({ slackToken: token, slackConnected: true });
        sendResponse({ success: true });
        // Fetch Slack history
        fetchSlackHistory(token);
      } else {
        sendResponse({ success: false });
      }
    }
  );
}

async function fetchSlackHistory(token) {
  console.log('💬 Fetching Slack history...');
  try {
    // Get user info first (to filter own messages)
    const userRes  = await fetch('https://slack.com/api/users.identity', {
      headers: { Authorization: `Bearer ${token}` }
    });
    const userData = await userRes.json();
    const myUserId = userData.user?.id;

    // Get list of conversations
    const convRes  = await fetch('https://slack.com/api/conversations.list?types=im,mpim,public_channel,private_channel&limit=20', {
      headers: { Authorization: `Bearer ${token}` }
    });
    const convData = await convRes.json();
    const channels = convData.channels || [];

    const messages = [];

    // Fetch history from each channel
    for (const channel of channels.slice(0, 10)) {
      try {
        const histRes  = await fetch(
          `https://slack.com/api/conversations.history?channel=${channel.id}&limit=50`,
          { headers: { Authorization: `Bearer ${token}` } }
        );
        const histData = await histRes.json();

        // Keep only YOUR messages
        const myMsgs = (histData.messages || []).filter(m =>
          m.user === myUserId && m.text && m.text.length > 20
        );

        myMsgs.forEach(m => {
          messages.push({
            channel: channel.name || channel.id,
            text:    m.text.slice(0, 500),
            ts:      m.ts,
            platform: 'slack',
          });
        });

        await sleep(300);
      } catch(e) { continue; }
    }

    console.log(`💬 Extracted ${messages.length} Slack messages`);

    if (messages.length > 0) {
      await sendHistoryToAPI(messages, 'slack');
    }

    chrome.storage.local.set({ slackHistory: messages });

  } catch(e) {
    console.error('Slack history fetch failed:', e);
  }
}

// ── Send history to Alterus API ───────────────────────────────────────────────
async function sendHistoryToAPI(items, platform) {
  try {
    const config = await chromeStorageGet('userConfig');
    const res = await fetch(`${ALTERUS_API}/api/ingest-history`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        platform,
        items,
        user_name: config.userConfig?.name || '',
      }),
    });
    const data = await res.json();
    console.log(`✅ Sent ${items.length} ${platform} items to Alterus. Chunks: ${data.chunks_added}`);
  } catch(e) {
    console.error('Failed to send history to API:', e);
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function chromeStorageGet(key) {
  return new Promise(resolve => {
    chrome.storage.local.get(key, data => resolve(data));
  });
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
