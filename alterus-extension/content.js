/**
 * content.js
 * Runs on Gmail, Outlook, Slack, Teams.
 * 
 * Does 3 things:
 *   1. Detects which platform you're on
 *   2. Reads current email/message from the page
 *   3. Silently scrapes sent history when user browses Sent Items
 *   4. Injects the Alterus sidebar
 */

// ── Config ────────────────────────────────────────────────────────────────────
const ALTERUS_API = 'https://YOUR_RAILWAY_APP.up.railway.app';

// ── Platform detection ────────────────────────────────────────────────────────
function detectPlatform() {
  const host = window.location.hostname;
  const path = window.location.pathname;
  if (host === 'mail.google.com')          return 'gmail';
  if (host.includes('outlook.office.com')) return 'outlook';
  if (host.includes('outlook.live.com'))   return 'outlook';
  if (host === 'app.slack.com')            return 'slack';
  if (host === 'teams.microsoft.com')      return 'teams';
  return null;
}

const PLATFORM = detectPlatform();
if (!PLATFORM) { /* not our page, do nothing */ }

// ── Read email/message from current page ──────────────────────────────────────
function readCurrentContext() {
  switch (PLATFORM) {
    case 'gmail':   return readGmail();
    case 'outlook': return readOutlook();
    case 'slack':   return readSlack();
    case 'teams':   return readTeams();
    default:        return null;
  }
}

function readGmail() {
  try {
    // Email being read
    const subject  = document.querySelector('h2[data-thread-perm-id], h2.hP')?.innerText?.trim() || '';
    const sender   = document.querySelector('.gD')?.getAttribute('email') ||
                     document.querySelector('.go')?.innerText?.trim() || '';
    const senderName = document.querySelector('.gD')?.getAttribute('name') || sender;
    
    // Body of current email
    const bodyEls = document.querySelectorAll('.a3s.aiL, .ii.gt');
    const body    = Array.from(bodyEls).map(el => el.innerText?.trim()).filter(Boolean).join('\n\n');

    // Compose window if open
    const composeBody    = document.querySelector('[aria-label="Message Body"]')?.innerText?.trim() || '';
    const composeTo      = document.querySelector('[name="to"]')?.value || '';
    const composeSubject = document.querySelector('[name="subjectbox"]')?.value || '';

    return {
      platform:    'gmail',
      type:        composeBody ? 'compose' : 'read',
      sender:      senderName,
      senderEmail: sender,
      subject:     subject || composeSubject,
      body:        body || composeBody,
      composeTo,
    };
  } catch(e) { return null; }
}

function readOutlook() {
  try {
    const subject   = document.querySelector('[role="heading"][aria-level="2"]')?.innerText?.trim() ||
                      document.querySelector('.allowTextSelection h1')?.innerText?.trim() || '';
    const senderEl  = document.querySelector('[data-testid="from"]') ||
                      document.querySelector('[class*="sender"]');
    const sender    = senderEl?.innerText?.trim() || '';
    const bodyEl    = document.querySelector('[data-testid="body"]') ||
                      document.querySelector('.allowTextSelection');
    const body      = bodyEl?.innerText?.trim() || '';

    return {
      platform: 'outlook',
      type:     'read',
      sender,
      subject,
      body:     body.slice(0, 2000),
    };
  } catch(e) { return null; }
}

function readSlack() {
  try {
    // Current channel/DM name
    const channel = document.querySelector('[data-qa="channel_name"]')?.innerText?.trim() ||
                    document.querySelector('.p-view_header__channel_title')?.innerText?.trim() || '';

    // Messages in current view
    const msgEls  = document.querySelectorAll('[data-qa="message_content"] .p-rich_text_section');
    const messages = Array.from(msgEls).slice(-10).map(el => el.innerText?.trim()).filter(Boolean);
    const body    = messages.join('\n');

    // Who sent the last message
    const lastSenderEl = document.querySelectorAll('[data-qa="message_sender_name"]');
    const sender  = lastSenderEl[lastSenderEl.length - 1]?.innerText?.trim() || '';

    return {
      platform: 'slack',
      type:     'chat',
      sender,
      subject:  channel,
      body,
    };
  } catch(e) { return null; }
}

function readTeams() {
  try {
    const channel  = document.querySelector('[data-tid="channel-name"]')?.innerText?.trim() ||
                     document.querySelector('[class*="channel-header"]')?.innerText?.trim() || '';
    const msgEls   = document.querySelectorAll('[data-tid="message-body-content"]');
    const messages = Array.from(msgEls).slice(-10).map(el => el.innerText?.trim()).filter(Boolean);
    const body     = messages.join('\n');
    const senderEl = document.querySelectorAll('[data-tid="message-author-name"]');
    const sender   = senderEl[senderEl.length - 1]?.innerText?.trim() || '';

    return {
      platform: 'teams',
      type:     'chat',
      sender,
      subject:  channel,
      body,
    };
  } catch(e) { return null; }
}

// ── History scraper ────────────────────────────────────────────────────────────
// Silently collects sent emails/messages as user browses
let scrapedHistory = [];
let scrapeCount    = 0;
const MAX_SCRAPE   = 100;

function scrapeOutlookSentItems() {
  if (scrapeCount >= MAX_SCRAPE) return;
  if (!window.location.href.includes('sentitems') &&
      !window.location.href.includes('sent')) return;

  try {
    // Read visible email list items
    const items = document.querySelectorAll('[data-convid], [role="option"]');
    items.forEach(item => {
      const subject = item.querySelector('[class*="subject"]')?.innerText?.trim();
      const preview = item.querySelector('[class*="preview"]')?.innerText?.trim();
      if (subject && preview && scrapedHistory.length < MAX_SCRAPE) {
        const key = `${subject}_${preview.slice(0,20)}`;
        if (!scrapedHistory.find(h => h.key === key)) {
          scrapedHistory.push({ key, subject, preview, platform: 'outlook' });
          scrapeCount++;
        }
      }
    });

    // If user clicks into a sent email, capture the full body
    const body = document.querySelector('.allowTextSelection')?.innerText?.trim();
    const subj = document.querySelector('[class*="subject"]')?.innerText?.trim();
    if (body && subj && body.length > 50) {
      chrome.storage.local.get('outlookHistory', data => {
        const existing = data.outlookHistory || [];
        if (!existing.find(e => e.subject === subj)) {
          existing.push({ subject: subj, body: body.slice(0, 800), platform: 'outlook' });
          if (existing.length <= MAX_SCRAPE) {
            chrome.storage.local.set({ outlookHistory: existing });
          }
        }
      });
    }
  } catch(e) {}
}

function scrapeTeamsHistory() {
  if (scrapeCount >= MAX_SCRAPE) return;
  try {
    const messages = document.querySelectorAll('[data-tid="message-body-content"]');
    const senders  = document.querySelectorAll('[data-tid="message-author-name"]');
    
    chrome.storage.local.get(['userConfig', 'teamsHistory'], data => {
      const myName    = (data.userConfig?.name || '').toLowerCase();
      const existing  = data.teamsHistory || [];

      messages.forEach((msg, i) => {
        const sender  = senders[i]?.innerText?.trim() || '';
        const text    = msg.innerText?.trim() || '';
        // Only capture YOUR messages for style learning
        if (sender.toLowerCase().includes(myName) && text.length > 20) {
          const key = text.slice(0, 30);
          if (!existing.find(e => e.key === key)) {
            existing.push({ key, sender, text, platform: 'teams' });
            scrapeCount++;
          }
        }
      });

      if (existing.length <= MAX_SCRAPE) {
        chrome.storage.local.set({ teamsHistory: existing });
      }
    });
  } catch(e) {}
}

// ── Insert draft into compose box ─────────────────────────────────────────────
function insertDraft(draft) {
  switch(PLATFORM) {
    case 'gmail':   insertGmailDraft(draft);   break;
    case 'outlook': insertOutlookDraft(draft); break;
    case 'slack':   insertSlackDraft(draft);   break;
    case 'teams':   insertTeamsDraft(draft);   break;
  }
}

function insertGmailDraft(draft) {
  const compose = document.querySelector('[aria-label="Message Body"]');
  if (compose) {
    compose.focus();
    compose.innerHTML = draft.replace(/\n/g, '<br>');
  }
}

function insertOutlookDraft(draft) {
  const compose = document.querySelector('[contenteditable="true"][aria-label*="compose"],'+
                                        '[contenteditable="true"][aria-multiline="true"]');
  if (compose) {
    compose.focus();
    compose.innerHTML = draft.replace(/\n/g, '<br>');
  }
}

function insertSlackDraft(draft) {
  const box = document.querySelector('[data-qa="message_input"] [contenteditable="true"]');
  if (box) {
    box.focus();
    box.innerText = draft;
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

function insertTeamsDraft(draft) {
  const box = document.querySelector('[contenteditable="true"][role="textbox"]');
  if (box) {
    box.focus();
    box.innerText = draft;
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

// ── Sidebar injection ─────────────────────────────────────────────────────────
let sidebarInjected = false;
let sidebarVisible  = false;

function injectSidebar() {
  if (sidebarInjected) return;
  sidebarInjected = true;

  // Sidebar container
  const sidebar = document.createElement('div');
  sidebar.id    = 'alterus-sidebar';
  sidebar.innerHTML = `
    <div id="alterus-header">
      <span id="alterus-logo">✦ Alterus</span>
      <span id="alterus-platform">${PLATFORM}</span>
      <button id="alterus-close">✕</button>
    </div>

    <div id="alterus-body">

      <!-- Tone dial -->
      <div class="alterus-section">
        <label class="alterus-label">Tone</label>
        <div id="alterus-tone-row">
          <span class="tone-opt active" data-tone="direct">Direct</span>
          <span class="tone-opt" data-tone="balanced">Balanced</span>
          <span class="tone-opt" data-tone="diplomatic">Diplomatic</span>
        </div>
      </div>

      <!-- Draft button -->
      <button id="alterus-draft-btn">
        ✍️ Draft Reply
      </button>

      <!-- Status -->
      <div id="alterus-status"></div>

      <!-- Draft output -->
      <div id="alterus-draft-area" style="display:none">
        <label class="alterus-label">Your draft</label>
        <textarea id="alterus-draft-text" rows="8"></textarea>
        <div id="alterus-draft-actions">
          <button class="alterus-action-btn" id="alterus-insert">
            ↗ Insert
          </button>
          <button class="alterus-action-btn secondary" id="alterus-copy">
            ⎘ Copy
          </button>
          <div id="alterus-feedback">
            <button class="fb-btn" id="fb-up"   title="Good draft">👍</button>
            <button class="fb-btn" id="fb-down" title="Needs work">👎</button>
            <span id="fb-msg"></span>
          </div>
        </div>
      </div>

      <!-- Connect accounts -->
      <div id="alterus-connections">
        <label class="alterus-label">Connected accounts</label>
        <button class="connect-btn" id="connect-gmail">
          <img src="https://www.google.com/favicon.ico" width="14"> Connect Gmail
        </button>
        <button class="connect-btn" id="connect-slack">
          <img src="https://slack.com/favicon.ico" width="14"> Connect Slack
        </button>
        <div id="connection-status"></div>
      </div>

    </div>

    <!-- Toggle tab -->
    <div id="alterus-tab">✦</div>
  `;

  document.body.appendChild(sidebar);

  // Wire up events
  setupSidebarEvents(sidebar);
  loadConnections();
}

function setupSidebarEvents(sidebar) {
  let currentTone   = 'balanced';
  let currentDraft  = '';
  let currentRunId  = '';

  // Tone selector
  sidebar.querySelectorAll('.tone-opt').forEach(btn => {
    btn.addEventListener('click', () => {
      sidebar.querySelectorAll('.tone-opt').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentTone = btn.dataset.tone;
    });
  });

  // Close button
  sidebar.querySelector('#alterus-close').addEventListener('click', () => {
    sidebar.classList.remove('visible');
    sidebarVisible = false;
  });

  // Toggle tab
  sidebar.querySelector('#alterus-tab').addEventListener('click', () => {
    sidebarVisible = !sidebarVisible;
    sidebar.classList.toggle('visible', sidebarVisible);
  });

  // Draft button
  sidebar.querySelector('#alterus-draft-btn').addEventListener('click', async () => {
    const ctx = readCurrentContext();
    if (!ctx || !ctx.body) {
      setStatus('⚠️ Could not read message. Make sure an email or chat is open.', 'warn');
      return;
    }

    setStatus('🧠 Drafting in your voice...', 'loading');
    sidebar.querySelector('#alterus-draft-area').style.display = 'none';

    try {
      const config = await getStoredConfig();
      const res    = await fetch(`${ALTERUS_API}/api/draft`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          platform:    ctx.platform,
          type:        ctx.type,
          sender:      ctx.sender,
          sender_email: ctx.senderEmail || '',
          subject:     ctx.subject,
          body:        ctx.body,
          tone:        currentTone,
          user_name:   config.name || '',
          stakeholders: config.stakeholders || [],
        }),
      });

      const data = await res.json();

      if (data.draft) {
        currentDraft  = data.draft;
        currentRunId  = data.run_id || '';
        sidebar.querySelector('#alterus-draft-text').value = currentDraft;
        sidebar.querySelector('#alterus-draft-area').style.display = 'block';
        sidebar.querySelector('#fb-msg').textContent = '';
        setStatus('', '');
      } else {
        setStatus('❌ Draft failed. Is your API key set?', 'error');
      }
    } catch(e) {
      setStatus(`❌ Could not reach Alterus server. Is it running?`, 'error');
    }
  });

  // Insert button
  sidebar.querySelector('#alterus-insert').addEventListener('click', () => {
    const draft = sidebar.querySelector('#alterus-draft-text').value;
    insertDraft(draft);
    setStatus('✅ Inserted into compose box', 'ok');
  });

  // Copy button
  sidebar.querySelector('#alterus-copy').addEventListener('click', () => {
    const draft = sidebar.querySelector('#alterus-draft-text').value;
    navigator.clipboard.writeText(draft);
    setStatus('✅ Copied to clipboard', 'ok');
  });

  // Feedback buttons
  sidebar.querySelector('#fb-up').addEventListener('click', () => {
    sendFeedback('thumbs_up', currentDraft, currentRunId);
    sidebar.querySelector('#fb-msg').textContent = '✓ Thanks!';
    sidebar.querySelector('#fb-up').disabled   = true;
    sidebar.querySelector('#fb-down').disabled = true;
  });
  sidebar.querySelector('#fb-down').addEventListener('click', () => {
    sendFeedback('thumbs_down', currentDraft, currentRunId);
    sidebar.querySelector('#fb-msg').textContent = '✓ Noted — will improve';
    sidebar.querySelector('#fb-up').disabled   = true;
    sidebar.querySelector('#fb-down').disabled = true;
  });

  // Connect Gmail
  sidebar.querySelector('#connect-gmail').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'connectGmail' }, response => {
      if (response?.success) {
        updateConnectionStatus('gmail', true);
        setStatus('✅ Gmail connected — learning your style...', 'ok');
        // Trigger history fetch in background
        chrome.runtime.sendMessage({ action: 'fetchGmailHistory' });
      } else {
        setStatus('❌ Gmail connection failed', 'error');
      }
    });
  });

  // Connect Slack
  sidebar.querySelector('#connect-slack').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'connectSlack' }, response => {
      if (response?.success) {
        updateConnectionStatus('slack', true);
        setStatus('✅ Slack connected', 'ok');
      }
    });
  });

  function setStatus(msg, type) {
    const el = sidebar.querySelector('#alterus-status');
    el.textContent = msg;
    el.className   = `alterus-status ${type}`;
  }

  function updateConnectionStatus(platform, connected) {
    const btn = sidebar.querySelector(`#connect-${platform}`);
    if (btn) {
      btn.innerHTML = `✅ ${platform.charAt(0).toUpperCase() + platform.slice(1)} connected`;
      btn.disabled = true;
      btn.classList.add('connected');
    }
  }
}

async function getStoredConfig() {
  return new Promise(resolve => {
    chrome.storage.local.get('userConfig', data => {
      resolve(data.userConfig || {});
    });
  });
}

async function loadConnections() {
  chrome.storage.local.get(['gmailConnected', 'slackConnected'], data => {
    if (data.gmailConnected) {
      const btn = document.querySelector('#connect-gmail');
      if (btn) { btn.innerHTML = '✅ Gmail connected'; btn.disabled = true; }
    }
    if (data.slackConnected) {
      const btn = document.querySelector('#connect-slack');
      if (btn) { btn.innerHTML = '✅ Slack connected'; btn.disabled = true; }
    }
  });
}

async function sendFeedback(type, draft, runId) {
  try {
    await fetch(`${ALTERUS_API}/api/feedback`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ type, draft, run_id: runId }),
    });
  } catch(e) {}
}

// ── Init ──────────────────────────────────────────────────────────────────────
if (PLATFORM) {
  // Wait for page to settle before injecting
  setTimeout(() => {
    injectSidebar();

    // Start history scraping in background
    if (PLATFORM === 'outlook') {
      setInterval(scrapeOutlookSentItems, 3000);
    }
    if (PLATFORM === 'teams') {
      setInterval(scrapeTeamsHistory, 3000);
    }

    // Watch for URL changes (Gmail is a SPA)
    let lastUrl = window.location.href;
    new MutationObserver(() => {
      if (window.location.href !== lastUrl) {
        lastUrl = window.location.href;
        if (PLATFORM === 'outlook') scrapeOutlookSentItems();
      }
    }).observe(document, { subtree: true, childList: true });

  }, 2000);
}
