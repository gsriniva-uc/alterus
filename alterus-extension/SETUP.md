# Alterus Extension — Setup Guide

## Install in Chrome (30 seconds)

1. Download this `alterus-extension` folder
2. Open Chrome → go to `chrome://extensions`
3. Enable **Developer mode** (top right toggle)
4. Click **"Load unpacked"**
5. Select the `alterus-extension` folder
6. Done — the ✦ tab appears on the right side of Gmail/Outlook/Slack/Teams

---

## First-Time Setup

Click the Alterus icon in your Chrome toolbar (top right):

1. Enter your **name**, **role**, and **company**
2. Enter your **key contacts** (comma separated — e.g. "John Smith, Amy Lee")
3. Click **Save Profile**

---

## Connect Your Accounts (for better drafts)

### Gmail (recommended)
- Open Gmail → click the ✦ tab on the right
- Click **Connect Gmail**
- Sign in with Google → approve permissions
- Alterus learns from your last 80 sent emails (~2 min)

### Slack
- Open Slack Web (app.slack.com) → click the ✦ tab
- Click **Connect Slack**
- Sign in to Slack → approve
- Alterus learns from your recent messages (~1 min)

### Outlook (automatic)
- Open Outlook Web → go to **Sent Items**
- Scroll through your sent emails for 2-3 minutes
- Alterus silently reads them in the background
- Check the popup — it will show "X emails" collected

### Teams (automatic)
- Open Teams Web (teams.microsoft.com)
- Browse through your chats for 2-3 minutes
- Alterus reads your messages in the background

---

## Using Alterus

1. Open any email or chat in your browser
2. Click the **✦ tab** on the right edge of the screen
3. Choose your **tone** (Direct / Balanced / Diplomatic)
4. Click **Draft Reply**
5. Review the draft → edit if needed
6. Click **↗ Insert** to insert into the compose box
7. Hit send as normal

---

## Platforms Supported

- ✅ Gmail (mail.google.com)
- ✅ Outlook Web (outlook.office.com)
- ✅ Slack Web (app.slack.com)
- ✅ Microsoft Teams Web (teams.microsoft.com)

---

## Before using — update the API URL

Open `content.js` and `background.js` and replace:
```
https://YOUR_RAILWAY_APP.up.railway.app
```
with your actual Railway URL.

---

## Troubleshooting

**Draft says "Could not reach Alterus server"**
→ Make sure the Railway app is deployed and running

**Sidebar doesn't appear**
→ Reload the page, then click the ✦ tab on the right edge

**Gmail OAuth fails**
→ Make sure your Google Client ID is set in manifest.json
