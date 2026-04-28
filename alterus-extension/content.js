/**
 * content.js
 * Injected into Gmail, Outlook Web, Slack, Teams.
 * Reads current message, injects sidebar, scrapes history.
 *
 * FIXES in v1.0.1:
 *   1. Added outlook.office365.com detection (enterprise Outlook URL)
 *   2. Fixed ALTERUS_API race condition — now resolved per-call via getApiUrl()
 *   3. Declared userEmail and userToken properly from storage
 *   4. Better error handling — shows actual error, not just "Is it deployed?"
 *   5. Added 30s fetch timeout so errors surface faster
 */

// ── Platform detection ────────────────────────────────────────────────────────
const host     = window.location.hostname;
const PLATFORM =
  host === 'mail.google.com'                ? 'gmail'   :
  host.includes('outlook.office365.com')    ? 'outlook' :   // FIX: enterprise Outlook
  host.includes('outlook.office.com')       ? 'outlook' :
  host.includes('outlook.cloud.microsoft')  ? 'outlook' :
  host.includes('outlook.live.com')         ? 'outlook' :
  host === 'app.slack.com'                  ? 'slack'   :
  host === 'teams.microsoft.com'            ? 'teams'   :
  host.includes('teams.microsoft.com')      ? 'teams'   : null;

if (!PLATFORM) { throw new Error('Alterus: not our page'); }

// ── Get API URL per-call (FIX: was set once async, could race) ────────────────
async function getApiUrl() {
  const d = await new Promise(r => chrome.storage.local.get('alterusApiUrl', r));
  return d.alterusApiUrl || 'https://alterus.onrender.com';
}

// ── Get user config including email + token ───────────────────────────────────
async function getConfig() {
  const d = await new Promise(r => chrome.storage.local.get('userConfig', r));
  return d.userConfig || {};
}

// ── Fetch with timeout (FIX: no timeout meant errors took forever) ────────────
async function fetchWithTimeout(url, options, timeoutMs = 30000) {
  const controller = new AbortController();
  const id         = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(id);
    return res;
  } catch (e) {
    clearTimeout(id);
    throw e;
  }
}

// ── Read current context from page ────────────────────────────────────────────
function readContext() {
  try {
    switch (PLATFORM) {
      case 'gmail':   return readGmail();
      case 'outlook': return readOutlook();
      case 'slack':   return readSlack();
      case 'teams':   return readTeams();
    }
  } catch(e) { return null; }
}

function readGmail() {
  const subject     = document.querySelector('h2.hP, [data-thread-perm-id]')?.innerText?.trim() || '';
  const senderEl    = document.querySelector('.gD');
  const sender      = senderEl?.getAttribute('name') || senderEl?.getAttribute('email') || '';
  const senderEmail = senderEl?.getAttribute('email') || '';
  const bodies      = document.querySelectorAll('.a3s.aiL');
  const body        = Array.from(bodies).map(e => e.innerText?.trim()).filter(Boolean).join('\n\n');
  const composeBody = document.querySelector('[aria-label="Message Body"]')?.innerText?.trim() || '';
  const composeTo   = document.querySelector('[name="to"]')?.value || '';
  return {
    platform: 'gmail', sender, senderEmail,
    subject: subject || document.querySelector('[name="subjectbox"]')?.value || '',
    body: body || composeBody, composeTo,
    type: composeBody ? 'compose' : 'read',
  };
}

function readOutlook() {
  const subject = document.querySelector('[role="heading"]')?.innerText?.trim() ||
                  document.querySelector('.allowTextSelection h1')?.innerText?.trim() || '';
  const sender  = document.querySelector('[data-testid="from"], [class*="sender"]')?.innerText?.trim() || '';
  const body    = document.querySelector('[data-testid="body"], .allowTextSelection')?.innerText?.trim() || '';
  return { platform: 'outlook', sender, senderEmail: '', subject, body: body.slice(0, 2000), type: 'read' };
}

function readSlack() {
  const channel = document.querySelector('[data-qa="channel_name"]')?.innerText?.trim() || '';
  const msgs    = Array.from(document.querySelectorAll('[data-qa="message_content"] .p-rich_text_section'))
                      .slice(-8).map(e => e.innerText?.trim()).filter(Boolean);
  const senders = document.querySelectorAll('[data-qa="message_sender_name"]');
  const sender  = senders[senders.length - 1]?.innerText?.trim() || '';
  return { platform: 'slack', sender, senderEmail: '', subject: channel, body: msgs.join('\n'), type: 'chat' };
}

function readTeams() {
  const channel = document.querySelector('[data-tid="chat-title"]')?.innerText?.trim() || '';
  const msgs    = Array.from(document.querySelectorAll('[data-tid="chat-pane-message"]'))
                      .slice(-8).map(e => e.innerText?.trim()).filter(Boolean);
  const senders = document.querySelectorAll('[data-tid="message-author-name"]');
  const sender  = senders[senders.length - 1]?.innerText?.trim() || '';
  return { platform: 'teams', sender, senderEmail: '', subject: channel, body: msgs.join('\n'), type: 'chat' };
}

// ── Insert draft into compose box ─────────────────────────────────────────────
function insertDraft(text) {
  const selectors = {
    gmail:   '[aria-label="Message Body"]',
    outlook: '[contenteditable="true"][aria-multiline="true"], [contenteditable="true"][aria-label*="compose"]',
    slack:   '[data-qa="message_input"] [contenteditable="true"]',
    teams:   '[contenteditable="true"][role="textbox"]',
  };
  const el = document.querySelector(selectors[PLATFORM]);
  if (!el) return false;
  el.focus();
  if (PLATFORM === 'gmail' || PLATFORM === 'outlook') {
    el.innerHTML = text.replace(/\n/g, '<br>');
  } else {
    el.innerText = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  return true;
}

// ── History scraping ──────────────────────────────────────────────────────────
function scrapeOutlookSent() {
  if (!window.location.href.toLowerCase().includes('sent')) return;
  const body    = document.querySelector('.allowTextSelection')?.innerText?.trim();
  const subject = document.querySelector('[class*="subject"]')?.innerText?.trim();
  if (!body || !subject || body.length < 30) return;

  chrome.storage.local.get('outlookHistory', d => {
    const hist = d.outlookHistory || [];
    if (hist.find(e => e.subject === subject)) return;
    hist.push({ subject, body: body.slice(0, 600), platform: 'outlook' });
    if (hist.length <= 100) chrome.storage.local.set({ outlookHistory: hist });
  });
}

function scrapeTeamsMsgs() {
  chrome.storage.local.get(['userConfig', 'teamsHistory'], d => {
    const myName = (d.userConfig?.name || '').toLowerCase().split(' ')[0];
    if (!myName) return;
    const hist   = d.teamsHistory || [];
    const msgs   = document.querySelectorAll('[data-tid="message-body-content"]');
    const senders= document.querySelectorAll('[data-tid="message-author-name"]');
    msgs.forEach((msg, i) => {
      const sender = senders[i]?.innerText?.trim() || '';
      const text   = msg.innerText?.trim() || '';
      if (sender.toLowerCase().includes(myName) && text.length > 20) {
        const key = text.slice(0, 30);
        if (!hist.find(h => h.key === key)) {
          hist.push({ key, text, platform: 'teams' });
        }
      }
    });
    if (hist.length <= 100) chrome.storage.local.set({ teamsHistory: hist });
  });
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
let injected = false;

function injectSidebar() {
  if (injected) return;
  injected = true;

  const sb = document.createElement('div');
  sb.id    = 'alterus-sidebar';
  sb.innerHTML = `
    <div id="al-tab" title="Open Alterus">✦</div>
    <div id="al-panel">
      <div id="al-head">
        <span id="al-logo">✦ Alterus</span>
        <span id="al-plat">${PLATFORM}</span>
        <button id="al-close">✕</button>
      </div>
      <div id="al-body">

        <div class="al-section">
          <div class="al-label">Tone</div>
          <div id="al-tones">
            <span class="al-tone" data-t="direct">Direct</span>
            <span class="al-tone active" data-t="balanced">Balanced</span>
            <span class="al-tone" data-t="diplomatic">Diplomatic</span>
          </div>
        </div>

        <button id="al-draft-btn">✍️ Draft Reply</button>
        <div id="al-status"></div>

        <div id="al-result" style="display:none">
          <div class="al-label">Your draft</div>
          <textarea id="al-text" rows="9"></textarea>
          <div id="al-actions">
            <button class="al-btn" id="al-insert">↗ Insert</button>
            <button class="al-btn sec" id="al-copy">⎘ Copy</button>
            <div id="al-fb">
              <button class="fb" id="fb-up">👍</button>
              <button class="fb" id="fb-dn">👎</button>
              <span id="fb-msg"></span>
            </div>
          </div>
        </div>

        <div id="al-conns">
          <div class="al-label">Connected accounts</div>
          <button class="al-conn" id="conn-gmail">
            <span>G</span> Connect Gmail
          </button>
          <button class="al-conn" id="conn-slack">
            <span>#</span> Connect Slack
          </button>
          <div id="al-conn-status"></div>
        </div>

      </div>
    </div>
  `;
  document.body.appendChild(sb);
  wireEvents(sb);
  loadConnectionState(sb);
}

function wireEvents(sb) {
  let tone    = 'balanced';
  let draft   = '';
  let runId   = '';
  let visible = false;

  // Toggle panel
  sb.querySelector('#al-tab').onclick = () => {
    visible = !visible;
    sb.querySelector('#al-panel').classList.toggle('open', visible);
  };
  sb.querySelector('#al-close').onclick = () => {
    visible = false;
    sb.querySelector('#al-panel').classList.remove('open');
  };

  // Tone selector
  sb.querySelectorAll('.al-tone').forEach(btn => {
    btn.onclick = () => {
      sb.querySelectorAll('.al-tone').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      tone = btn.dataset.t;
    };
  });

  // ── Draft Reply button ────────────────────────────────────────────────────
  sb.querySelector('#al-draft-btn').onclick = async () => {
    const ctx = readContext();
    if (!ctx?.body) {
      setStatus('⚠️ Open an email or message first.', 'warn');
      return;
    }

    setStatus('🧠 Drafting in your voice...', 'loading');
    sb.querySelector('#al-result').style.display = 'none';

    try {
      // FIX: resolve API URL and user config per-call, not at module load time
      const [ALTERUS_API, cfg] = await Promise.all([getApiUrl(), getConfig()]);

      // FIX: userEmail and userToken now come from config, not undeclared vars
      const userEmail = cfg.email || '';
      const userToken = cfg.token || '';

      const headers = { 'Content-Type': 'application/json' };
      if (userToken) headers['Authorization'] = `Bearer ${userToken}`;

      // FIX: use fetchWithTimeout so errors surface in < 30s instead of hanging
      const res = await fetchWithTimeout(
        `${ALTERUS_API}/api/draft`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            platform:     ctx.platform,
            type:         ctx.type,
            sender:       ctx.sender,
            sender_email: ctx.senderEmail || '',
            subject:      ctx.subject,
            body:         ctx.body,
            tone,
            user_name:    cfg.name    || '',
            user_email:   userEmail,
            user_title:   cfg.role    || cfg.title || '',
            user_company: cfg.company || '',
            stakeholders: cfg.stakeholders || [],
          }),
        },
        30000
      );

      // FIX: handle non-JSON responses (HTML error pages from proxy/Render)
      // instead of crashing silently into "Cannot reach Alterus"
      let data;
      const contentType = res.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        data = await res.json();
      } else {
        // Got HTML or plain text — Render proxy error
        const text = await res.text();
        console.error('Alterus non-JSON response:', res.status, text.slice(0, 200));
        setStatus(`❌ Server error ${res.status} — check Render logs`, 'error');
        return;
      }

      if (data.draft) {
        draft = data.draft;
        runId = data.run_id || '';
        sb.querySelector('#al-text').value = draft;
        sb.querySelector('#al-result').style.display = 'block';
        sb.querySelector('#fb-msg').textContent = '';
        sb.querySelector('#fb-up').disabled = false;
        sb.querySelector('#fb-dn').disabled = false;
        setStatus('', '');

        // Communication risk check
        const senderName = ctx.sender ? ctx.sender.split(' ')[0] : '';
        if (senderName) {
          let riskEl = sb.querySelector('#al-risk');
          if (!riskEl) {
            riskEl = document.createElement('div');
            riskEl.id = 'al-risk';
            const textArea = sb.querySelector('#al-text');
            textArea.parentNode.insertBefore(riskEl, textArea);
          }
          riskEl.style.cssText = 'margin:6px 0;font-size:11px;color:#5a607a;font-family:monospace;padding:6px;';
          riskEl.textContent = '⏳ Checking risk...';
          checkCommunicationRisk(draft, senderName, ctx.platform, userEmail, ALTERUS_API, userToken).then(risk => {
            if (!risk) { riskEl.remove(); return; }
            if (risk.status === 'scored') {
              const color = risk.risk_level === 'high' ? '#ef4444'
                : risk.risk_level === 'medium' ? '#f59e0b' : '#22c55e';
              riskEl.style.cssText = `margin:6px 0;background:#0a0d16;border-left:3px solid ${color};
                border-radius:0 6px 6px 0;padding:8px 10px;font-size:11px;`;
              riskEl.innerHTML =
                `<div style="color:${color};font-weight:600;margin-bottom:3px;">
                  ${risk.risk_emoji} ${risk.risk_score}% Risk — ${risk.risk_level}
                </div>` +
                (risk.explanation ? `<div style="color:#8b8fa8;margin-bottom:3px;">${risk.explanation}</div>` : '') +
                (risk.suggestion  ? `<div style="color:#6366f1;">💡 ${risk.suggestion}</div>` : '');
            } else if (risk.status === 'insufficient_data') {
              riskEl.style.cssText = 'margin:6px 0;font-size:10px;color:#3a3f55;font-family:monospace;padding:4px 0;';
              riskEl.textContent = '🔒 ' + risk.message;
            } else {
              riskEl.remove();
            }
          });
        }

      } else {
        // Draft endpoint returned error field
        setStatus(`❌ ${data.error || 'Draft failed — try again'}`, 'error');
      }

    } catch (e) {
      // FIX: distinguish timeout from other network errors
      if (e.name === 'AbortError') {
        setStatus('❌ Request timed out — Render may be slow. Try again.', 'error');
      } else {
        // Show the actual error, not just "Is it deployed?"
        console.error('Alterus draft error:', e);
        setStatus(`❌ Network error: ${e.message || 'Cannot reach server'}`, 'error');
      }
    }
  };

  // Insert draft into compose box
  sb.querySelector('#al-insert').onclick = () => {
    const text = sb.querySelector('#al-text').value;
    if (insertDraft(text)) setStatus('✅ Inserted!', 'ok');
    else setStatus('⚠️ No compose box found. Copy instead.', 'warn');
  };

  // Copy to clipboard
  sb.querySelector('#al-copy').onclick = () => {
    navigator.clipboard.writeText(sb.querySelector('#al-text').value);
    setStatus('✅ Copied!', 'ok');
  };

  // Feedback
  sb.querySelector('#fb-up').onclick = async () => {
    const [url, cfg] = await Promise.all([getApiUrl(), getConfig()]);
    sendFeedback('thumbs_up', draft, runId, url, cfg.token || '');
    sb.querySelector('#fb-msg').textContent = '✓ Thanks!';
    sb.querySelector('#fb-up').disabled = true;
    sb.querySelector('#fb-dn').disabled = true;
  };
  sb.querySelector('#fb-dn').onclick = async () => {
    const [url, cfg] = await Promise.all([getApiUrl(), getConfig()]);
    sendFeedback('thumbs_down', draft, runId, url, cfg.token || '');
    sb.querySelector('#fb-msg').textContent = '✓ Noted';
    sb.querySelector('#fb-up').disabled = true;
    sb.querySelector('#fb-dn').disabled = true;
  };

  // Connect Gmail
  sb.querySelector('#conn-gmail').onclick = () => {
    chrome.runtime.sendMessage({ action: 'connectGmail' }, res => {
      if (res?.success) {
        markConnected(sb, 'gmail');
        setStatus('✅ Gmail connected — learning your style...', 'ok');
        chrome.runtime.sendMessage({ action: 'fetchGmailHistory' });
      } else {
        setStatus('❌ Gmail connection failed', 'error');
      }
    });
  };

  // Connect Slack
  sb.querySelector('#conn-slack').onclick = () => {
    chrome.runtime.sendMessage({ action: 'connectSlack' }, res => {
      if (res?.success) {
        markConnected(sb, 'slack');
        setStatus('✅ Slack connected', 'ok');
      } else {
        setStatus('❌ Slack connection failed', 'error');
      }
    });
  };

  function setStatus(msg, type) {
    const el = sb.querySelector('#al-status');
    el.textContent = msg;
    el.className   = `al-status ${type}`;
  }
}

function markConnected(sb, platform) {
  const btn = sb.querySelector(`#conn-${platform}`);
  if (btn) { btn.innerHTML = `✅ ${platform} connected`; btn.disabled = true; }
}

function loadConnectionState(sb) {
  chrome.storage.local.get(['gmailConnected', 'slackConnected'], d => {
    if (d.gmailConnected) markConnected(sb, 'gmail');
    if (d.slackConnected) markConnected(sb, 'slack');
  });
}

async function sendFeedback(type, draft, runId, apiUrl, token) {
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    await fetch(`${apiUrl}/api/feedback`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ type, draft, run_id: runId }),
    });
  } catch(e) {}
}

// ── Communication Risk Check ──────────────────────────────────────────────────
// FIX: now takes apiUrl and token as parameters instead of relying on
// undeclared module-level variables
async function checkCommunicationRisk(draft, stakeholderName, platform, userEmail, apiUrl, token) {
  if (!draft || !stakeholderName) return null;
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetchWithTimeout(
      `${apiUrl}/api/risk/analyze`,
      {
        method: 'POST',
        headers,
        body: JSON.stringify({
          draft,
          stakeholder_name: stakeholderName,
          user_email: userEmail,
          platform,
        }),
      },
      15000
    );
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
setTimeout(() => {
  injectSidebar();

  if (PLATFORM === 'outlook') setInterval(scrapeOutlookSent, 4000);
  if (PLATFORM === 'teams')   setInterval(scrapeTeamsMsgs,   4000);

  // SPA navigation watcher (Outlook is a SPA)
  let lastUrl = location.href;
  new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      if (PLATFORM === 'outlook') scrapeOutlookSent();
    }
  }).observe(document, { subtree: true, childList: true });

}, 2500);
