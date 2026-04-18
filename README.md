# Alterus — Your AI Digital Twin

> *The other self. When you can't be in three places at once — Alterus is.*

AI that drafts your emails, Teams replies, and meeting prep in your voice.

---

## Quick Start (Local)

```bash
git clone https://github.com/YOUR_USERNAME/alterus
cd alterus
cp .env.example .env       # fill in your values
pip install -r requirements.txt
streamlit run ui/app.py
```

## Deploy to Railway

1. Fork this repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select this repo
4. Add environment variables (from .env.example)
5. Deploy

## Environment Variables

See `.env.example` for all required variables.

Minimum required:
- `USER_NAME` — your full name
- `USER_STAKEHOLDERS` — comma-separated list of key contacts
- `ANTHROPIC_API_KEY` — from console.anthropic.com
