"""
app.py — Alterus Dashboard v3
New in v3:
  - Sidebar: Quick Compose (floating, always accessible) + Tone Dial
  - Dashboard: Calendar card with today's meetings + auto prep
  - Tone Dial threads through ALL draft calls globally

Run:
    python -m streamlit run ui/app.py
"""

import sys, json, time, re, requests, xml.etree.ElementTree as ET, difflib
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from agent.graph import run_agent
from agent.drafter import generate
from agent.persona import build_system_prompt, PERSONA_CARD
from retrieval.retriever import Retriever
from agent.corpus_extractor import get_context
from agent.classifier import batch_analyze_messages, analyze_sentiment
from agent.prioritizer import prioritize_emails, prioritize_calendar, score_email
from agent.feedback import log_feedback, run_batch_healer, load_local_feedback
from config import (USER_NAME, USER_TITLE, USER_COMPANY, USER_LOCATION,
                    USER_STAKEHOLDERS, USER_TONE, get_persona_summary, is_configured)
from channels.zoom_watcher import scan_zoom_folder, load_meeting_transcript, get_new_meetings, load_meetings_cache
from agent.transcript_analyzer import process_all_new_meetings, process_meeting
from channels.webhook_server import load_json, INBOX_FILE, TEAMS_FILE, CALENDAR_FILE
from channels.mailto_sender import make_mailto_link, make_outlook_web_link, build_mailto_from_result

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="Alterus", page_icon="🤖",
                   layout="wide", initial_sidebar_state="expanded")

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap');

html,body,[class*="css"]{font-family:'DM Sans',sans-serif;}
.stApp{background:#090b12;color:#dde1f0;}

/* Sidebar */
[data-testid="stSidebar"]{background:#0d1020 !important;border-right:1px solid #1e2235;}
[data-testid="stSidebar"] .g-ct{font-size:13px;}

/* Tabs */
.stTabs [data-baseweb="tab-list"]{background:#10131e;border-bottom:1px solid #1e2235;gap:4px;padding:0 8px;}
.stTabs [data-baseweb="tab"]{background:transparent;color:#5a607a;font-size:13px;font-weight:500;border-radius:8px 8px 0 0;padding:10px 18px;}
.stTabs [aria-selected="true"]{background:#1a1e2e !important;color:#e8eaed !important;border-bottom:2px solid #6366f1 !important;}

/* Cards */
.g-card{background:#13162200;border:1px solid #1e2235;border-radius:14px;padding:20px;margin-bottom:14px;transition:border-color .2s;}
.g-card:hover{border-color:#2d3250;}
.g-card-accent{border-left:3px solid #6366f1;}
.g-card-green{border-left:3px solid #22c55e;}
.g-card-amber{border-left:3px solid #f59e0b;}

/* Card header */
.g-ch{display:flex;align-items:center;gap:10px;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #1e2235;}
.g-ct{font-family:'Syne',sans-serif;font-size:15px;font-weight:700;color:#e8eaed;}
.g-badge{background:#1e2235;color:#6366f1;font-size:10px;padding:2px 9px;border-radius:20px;font-family:'DM Mono',monospace;margin-left:auto;}
.g-badge-green{color:#22c55e;}
.g-badge-amber{color:#f59e0b;}

/* Tone dial */
.tone-label{font-size:11px;font-family:'DM Mono',monospace;color:#5a607a;margin-top:4px;display:flex;justify-content:space-between;}
.tone-active{color:#6366f1;font-size:12px;text-align:center;margin-top:6px;font-weight:600;}

/* Briefing */
.briefing-text{font-size:14px;line-height:1.75;color:#c8cce0;background:#10131e;border-radius:10px;padding:16px;border-left:3px solid #6366f1;}

/* Workstream item */
.ws-item{background:#10131e;border:1px solid #1e2235;border-radius:10px;padding:14px;margin-bottom:8px;}
.ws-name{font-weight:600;font-size:13px;color:#e8eaed;}
.ws-status{font-size:12px;color:#8b8fa8;margin-top:4px;line-height:1.5;}
.ws-tag{font-size:10px;background:#1e2235;padding:2px 8px;border-radius:20px;font-family:'DM Mono',monospace;}
.ws-tag-active{color:#22c55e;}
.ws-tag-risk{color:#f59e0b;}

/* Calendar */
.cal-item{background:#10131e;border:1px solid #1e2235;border-radius:10px;padding:12px 14px;margin-bottom:8px;display:flex;gap:14px;align-items:flex-start;}
.cal-time{font-family:'DM Mono',monospace;font-size:11px;color:#6366f1;min-width:60px;padding-top:2px;}
.cal-title{font-weight:600;font-size:13px;color:#e8eaed;}
.cal-att{font-size:11px;color:#5a607a;margin-top:3px;}
.cal-dur{font-size:10px;background:#1e2235;padding:2px 7px;border-radius:10px;font-family:'DM Mono',monospace;color:#8b8fa8;margin-top:4px;display:inline-block;}

/* Confidence ring */
.conf-ring{text-align:center;padding:20px;}
.conf-val{font-family:'Syne',sans-serif;font-size:48px;font-weight:800;}
.conf-label{font-size:12px;color:#5a607a;font-family:'DM Mono',monospace;margin-top:4px;}

/* Msg items */
.msg-item{background:#10131e;border:1px solid #1e2235;border-radius:10px;padding:11px 14px;margin-bottom:8px;}
.msg-sender{font-weight:600;font-size:13px;color:#e8eaed;}
.msg-preview{font-size:11px;color:#5a607a;margin-top:3px;}
.msg-time{font-size:10px;color:#3a3f55;font-family:'DM Mono',monospace;}

/* News/video */
.feed-item{padding:10px 0;border-bottom:1px solid #1e2235;}
.feed-item:last-child{border-bottom:none;}
.feed-title{font-size:13px;font-weight:500;color:#dde1f0;line-height:1.4;}
.feed-meta{font-size:10px;margin-top:3px;font-family:'DM Mono',monospace;}

/* Stakeholder card */
.sk-card{background:#10131e;border:1px solid #1e2235;border-radius:12px;padding:16px;margin-bottom:10px;}
.sk-name{font-family:'Syne',sans-serif;font-size:14px;font-weight:700;color:#e8eaed;}
.sk-role{font-size:11px;color:#6366f1;font-family:'DM Mono',monospace;margin-top:2px;}

/* Decision log */
.log-row{background:#10131e;border:1px solid #1e2235;border-radius:8px;padding:12px;margin-bottom:6px;display:flex;gap:12px;align-items:flex-start;}

/* Header */
.g-header{display:flex;align-items:center;justify-content:space-between;padding:6px 0 20px;border-bottom:1px solid #1e2235;margin-bottom:20px;}
.g-htitle{font-family:'Syne',sans-serif;font-size:24px;font-weight:800;color:#e8eaed;}
.g-hsub{font-size:12px;color:#4a5070;}
.dot-online{width:7px;height:7px;background:#22c55e;border-radius:50%;display:inline-block;margin-right:5px;}

/* Quick compose result */
.qc-result{background:#10131e;border:1px solid #2d3250;border-radius:10px;padding:14px;margin-top:10px;font-size:12px;color:#c8cce0;line-height:1.6;}

/* Button overrides */
.stButton>button{background:#6366f1 !important;color:#fff !important;border:none !important;border-radius:8px !important;font-family:'DM Sans',sans-serif !important;font-weight:500 !important;font-size:12px !important;}
.stButton>button:hover{background:#4f46e5 !important;}

/* Slider */
.stSlider [data-baseweb="slider"] div[role="slider"]{background:#6366f1 !important;}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
DEFAULTS = {
    "briefing":None,"briefing_date":None,
    "workstreams":None,"clone_score":None,
    "active_email":None,"email_draft":None,
    "active_teams":None,"teams_draft":None,
    "prd_result":None,"mirror_result":None,"meetprep_result":None,
    "news_cache":None,"news_fetched_at":None,"videos_cache":None,
    "decision_log":[],
    "sk_drafts":{},"zoom_meetings":[],"zoom_loaded":False,"zoom_active":None,"zoom_followup_draft":None,"use_live_data":False,"webhook_status":None,"feedback_given":{},
    "email_sentiments":{},"teams_sentiments":{},"sentiments_loaded":False,
    "ws_active":None,"ws_drafts":{},
    "corpus_ctx":None,
    "sk_active":None,
    "tone_value":50,           # 0=Direct, 100=Diplomatic
    "qc_result":None,          # Quick Compose result
    "cal_prep":{},             # {meeting_id: prep_text}
}
for k,v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Mock data ─────────────────────────────────────────────────────────────────
# ── Fallback mock data (used when no live webhook data exists) ────────────────
MOCK_EMAILS_FALLBACK = [
    {"id":"e1","from":"Jason Wong","subject":"Customer Engine — Q2 priorities",
     "preview":"Can you send the current state + Q2 targets?",
     "body":"Ganesh, can you send over the current state of Customer Engine and what we're targeting for Q2? Also want to understand the dependency on Sibanjan's team. Thanks, Jason",
     "time":"10:32 AM","unread":True},
    {"id":"e2","from":"Raghu (EDP)","subject":"ML model deployment timeline",
     "preview":"Following up on the ZTSD ML consulting engagement...",
     "body":"Hi Ganesh, following up on the ZTSD ML model consulting engagement. Can you share the current deployment timeline and blockers? We need this for EDP planning next week.",
     "time":"9:15 AM","unread":True},
    {"id":"e3","from":"Sibanjan Das","subject":"Re: Semantic router fix",
     "preview":"Fix deployed to staging. Review before prod?",
     "body":"The fix is deployed to staging. Can you review the test results and confirm before we push to prod?",
     "time":"Yesterday","unread":False},
]
MOCK_TEAMS_FALLBACK = [
    {"id":"t1","from":"Jerry Jiang",
     "message":"Hey Ganesh — dual-model vs single router for NBA agent? Need to finalize sprint plan.",
     "time":"11:04 AM","unread":True},
    {"id":"t2","from":"Senthil V.",
     "message":"3 sprint items past due. Can we sync today? Also need Stardog vs Neo4j decision.",
     "time":"10:45 AM","unread":True},
    {"id":"t3","from":"Jason Wong",
     "message":"Great Q1 delivery. Lock Q2 roadmap before all-hands Friday.",
     "time":"Yesterday","unread":False},
]

# ── Load live or fallback data — called on every render ──────────────────────
def load_live_emails(top_n: int = 10):
    """Load emails and prioritize using Copilot-inspired scoring."""
    live = load_json(INBOX_FILE)
    emails = live if live else MOCK_EMAILS_FALLBACK
    return prioritize_emails(emails, top_n=top_n)

def load_live_teams():
    live = load_json(TEAMS_FILE)
    return live if live else MOCK_TEAMS_FALLBACK

def load_live_calendar(top_n: int = 5):
    """Load calendar events and prioritize using Copilot-inspired scoring."""
    live = load_json(CALENDAR_FILE)
    if not live:
        return []
    return prioritize_calendar(live, top_n=top_n)

# Reload on every Streamlit render — picks up new webhook data
MOCK_EMAILS = load_live_emails()
MOCK_TEAMS  = load_live_teams()
# Loaded dynamically from corpus — see agent/corpus_extractor.py
WORKSTREAMS = []  # populated at runtime
# Loaded dynamically from corpus — see agent/corpus_extractor.py
STAKEHOLDERS = []  # populated at runtime
# ── Load calendar (live from webhook or fallback) — reloads every render ──────
_live_cal = load_live_calendar()
TODAY_MEETINGS = _live_cal if _live_cal else [
    {"id":"m1","time":"9:00 AM","title":"EAI Sprint 14 Planning",
     "attendees":"Sibanjan Das, Jerry Jiang, Senthil V.","duration":"60 min",
     "goal":"Lock sprint scope, resolve 3 overdue items, decide NBA agent architecture"},
    {"id":"m2","time":"11:00 AM","title":"Customer Engine Sync — Jason Wong",
     "attendees":"Jason Wong, Sibanjan Das","duration":"30 min",
     "goal":"Q2 priorities alignment, Customer Engine status, dependency review"},
    {"id":"m3","time":"2:00 PM","title":"EDP Planning — Raghu",
     "attendees":"Raghu","duration":"60 min",
     "goal":"ZTSD timeline, Stardog vs Neo4j decision, Feature Store planning"},
    {"id":"m4","time":"4:00 PM","title":"Halliburton PLEP Prep",
     "attendees":"Amin (Deputy CISO), External team","duration":"30 min",
     "goal":"Finalize AI strategy deck, confirm talking points for board session"},
]

# ── Load corpus context (replaces all hardcoded mock data) ───────────────────
def load_corpus_context(force: bool = False):
    """Load workstreams + stakeholders from corpus. Cached for 6 hours."""
    if st.session_state.corpus_ctx is None or force:
        with st.spinner("🧠 Loading your context from corpus..."):
            try:
                ctx = get_context(force_refresh=force)
                st.session_state.corpus_ctx = ctx
            except Exception as e:
                st.warning(f"Could not load corpus context: {e}")
                st.session_state.corpus_ctx = {
                    "workstreams": [], "stakeholders": [],
                    "topics": [], "news_query": "LangGraph+agentic+AI+enterprise"
                }
    return st.session_state.corpus_ctx

# Load on every page render (uses cache internally)
_ctx         = load_corpus_context()
WORKSTREAMS  = _ctx.get("workstreams", [])
STAKEHOLDERS = _ctx.get("stakeholders", [])
NEWS_QUERY   = _ctx.get("news_query", "LangGraph+agentic+AI+enterprise+ServiceNow")
TOPICS       = _ctx.get("topics", [])


# ── Sentiment loader ─────────────────────────────────────────────────────────
def load_sentiments():
    """Run sentiment analysis on all inbox messages. Cached in session state."""
    if not st.session_state.sentiments_loaded:
        with st.spinner("🧠 Analyzing message sentiment..."):
            try:
                for email in MOCK_EMAILS:
                    if email["id"] not in st.session_state.email_sentiments:
                        s = analyze_sentiment(email["body"], email["from"])
                        st.session_state.email_sentiments[email["id"]] = s
                for msg in MOCK_TEAMS:
                    if msg["id"] not in st.session_state.teams_sentiments:
                        s = analyze_sentiment(msg["message"], msg["from"])
                        st.session_state.teams_sentiments[msg["id"]] = s
                st.session_state.sentiments_loaded = True
            except Exception as e:
                st.session_state.sentiments_loaded = True  # don't retry on error

# ── Feedback helper ──────────────────────────────────────────────────────────
def render_feedback_buttons(key: str, draft: str, edited_draft: str,
                             input_text: str, task_type: str, run_id: str = None):
    """
    Render thumbs up/down feedback buttons.
    Logs to LangSmith + local file for self-healing.
    """
    feedback_key = f"feedback_{key}"
    already_given = st.session_state.feedback_given.get(feedback_key)

    if already_given:
        icon = "👍" if already_given == "thumbs_up" else "👎"
        st.markdown(f"""
        <div style="font-size:11px;color:#5a607a;font-family:'DM Mono',monospace;
                    margin-top:4px;">
          {icon} Feedback recorded — helps improve future drafts
        </div>""", unsafe_allow_html=True)
        return

    fb1, fb2, fb3 = st.columns([1, 1, 4])
    if fb1.button("👍", key=f"up_{key}", help="This draft is good"):
        log_feedback(
            run_id        = run_id,
            feedback_type = "thumbs_up",
            draft         = draft,
            input_text    = input_text,
            task_type     = task_type,
            edited_draft  = edited_draft,
        )
        st.session_state.feedback_given[feedback_key] = "thumbs_up"
        st.rerun()

    if fb2.button("👎", key=f"down_{key}", help="This draft needs improvement"):
        log_feedback(
            run_id        = run_id,
            feedback_type = "thumbs_down",
            draft         = draft,
            input_text    = input_text,
            task_type     = task_type,
            edited_draft  = edited_draft,
        )
        st.session_state.feedback_given[feedback_key] = "thumbs_down"
        st.rerun()

# ── Tone helper ───────────────────────────────────────────────────────────────
def get_tone_instruction(tone_val: int) -> str:
    if tone_val <= 25:
        return "TONE: Be extremely direct and concise. No softening language, no filler. Get straight to the point."
    elif tone_val <= 50:
        return "TONE: Direct and professional. Clear and confident with minimal softening."
    elif tone_val <= 75:
        return "TONE: Balanced and collaborative. Professional warmth while staying clear and decisive."
    else:
        return "TONE: Thoughtful and diplomatic. Acknowledge perspectives, soften edges, maintain relationship while being clear."

def tone_label(val: int) -> str:
    if val <= 20:   return "⚡ Very Direct"
    elif val <= 40: return "📌 Direct"
    elif val <= 60: return "⚖️ Balanced"
    elif val <= 80: return "🤝 Diplomatic"
    else:           return "🕊️ Very Diplomatic"

def inject_tone(input_text: str) -> str:
    """Append tone instruction to input before sending to agent."""
    return f"{input_text}\n\n[{get_tone_instruction(st.session_state.tone_value)}]"

# ── Data fetchers ─────────────────────────────────────────────────────────────
def fetch_ai_news():
    try:
        url = f"https://news.google.com/rss/search?q={NEWS_QUERY}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
        root = ET.fromstring(r.content)
        out = []
        for item in root.findall(".//item")[:5]:
            raw = item.findtext("title","")
            parts = raw.rsplit(" - ",1)
            out.append({"title":parts[0].strip(),
                        "source":parts[1].strip() if len(parts)>1 else "News",
                        "link":item.findtext("link","#")})
        return out
    except:
        return [{"title":"Could not load news","source":"","link":"#"}]

def fetch_ai_videos():
    channels = [("UCbmNph6atAoGfqLoCL_duAg","Two Minute Papers"),
                ("UCWX3yGbODI3RHyMSzMCQMFQ","AI Explained"),
                ("UCHnyfMqiRRG1u-2MsSQLbXA","Veritasium")]
    videos = []
    for cid,cname in channels:
        try:
            r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",timeout=8)
            root = ET.fromstring(r.content)
            ns = {"atom":"http://www.w3.org/2005/Atom"}
            for e in root.findall("atom:entry",ns)[:2]:
                title = e.findtext("atom:title","",ns)
                link  = e.find("atom:link",ns)
                href  = link.get("href","#") if link is not None else "#"
                videos.append({"title":title,"channel":cname,"url":href})
            if len(videos)>=5: break
        except: continue
    if not videos:
        videos = [
            {"title":"GPT-4o Explained","channel":"Two Minute Papers","url":"https://youtube.com"},
            {"title":"Claude vs GPT-4o","channel":"AI Explained","url":"https://youtube.com"},
            {"title":"LangGraph Production Agents","channel":"LangChain","url":"https://youtube.com"},
            {"title":"Agentic AI 2025","channel":"AI Explained","url":"https://youtube.com"},
            {"title":"Gemini 2.0 Deep Dive","channel":"Two Minute Papers","url":"https://youtube.com"},
        ]
    return videos[:5]

def compute_clone_score(text):
    try:
        r = Retriever()
        results = r.search(text, top_k=5)
        if not results: return 0.72
        avg = sum(x["similarity"] for x in results)/len(results)
        return min(round(avg*1.2,2),0.99)
    except:
        return 0.74

def log_decision(task_type, action, draft, input_text, critique=None):
    st.session_state.decision_log.append({
        "timestamp": datetime.now().strftime("%b %d %H:%M"),
        "task_type": task_type,
        "action":    action,
        "input":     input_text[:80],
        "draft":     draft[:120],
        "score":     critique.get("overall",0) if critique else 0,
        "tone":      tone_label(st.session_state.tone_value),
    })

def run_with_tone(input_text, source, sender, subject):
    return run_agent(
        input_text=inject_tone(input_text),
        source=source, sender=sender, subject=subject
    )

# ════════════════════════════════════════════════════════
# SIDEBAR — Quick Compose + Tone Dial (always accessible)
# ════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("""
    <div style="padding:8px 0 16px;">
      <div style="font-family:'Syne',sans-serif;font-size:18px;font-weight:800;color:#e8eaed;">
        🤖 Alterus
      </div>
      <div style="font-size:11px;color:#3a3f55;font-family:'DM Mono',monospace;margin-top:2px;">
        <span style="color:#22c55e;">●</span> online · llama3.2
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── TONE DIAL ─────────────────────────────────────────
    st.markdown("""
    <div style="background:#10131e;border:1px solid #1e2235;border-radius:12px;padding:14px;margin-bottom:14px;">
      <div style="font-family:'Syne',sans-serif;font-size:13px;font-weight:700;color:#e8eaed;margin-bottom:10px;">
        🎛️ Tone Dial
      </div>
    </div>
    """, unsafe_allow_html=True)

    tone_val = st.slider(
        label     = "tone_slider",
        min_value = 0,
        max_value = 100,
        value     = st.session_state.tone_value,
        step      = 5,
        label_visibility = "collapsed",
        key = "tone_slider_widget"
    )
    st.session_state.tone_value = tone_val

    st.markdown(f"""
    <div style="display:flex;justify-content:space-between;font-size:10px;
                color:#3a3f55;font-family:'DM Mono',monospace;margin-top:-8px;">
      <span>⚡ Direct</span>
      <span style="color:#6366f1;font-weight:600;">{tone_label(tone_val)}</span>
      <span>🕊️ Diplomatic</span>
    </div>
    <div style="font-size:10px;color:#3a3f55;margin-top:6px;font-style:italic;
                font-family:'DM Mono',monospace;">
      {get_tone_instruction(tone_val).replace('TONE: ','')}
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    # ── QUICK COMPOSE ─────────────────────────────────────
    st.markdown("""
    <div style="font-family:'Syne',sans-serif;font-size:13px;font-weight:700;
                color:#e8eaed;margin-bottom:10px;">
      ⚡ Quick Compose
    </div>
    <div style="font-size:11px;color:#5a607a;margin-bottom:8px;">
      Type anything → draft in your voice
    </div>
    """, unsafe_allow_html=True)

    qc_input = st.text_area(
        label            = "qc_input",
        placeholder      = "e.g. Quick reply to Jerry about the NBA agent decision\n\nOr: Email Raghu about ZTSD delay\n\nOr: Draft a Teams message to the team about the sprint",
        height           = 120,
        label_visibility = "collapsed",
        key              = "qc_text"
    )

    qc_type = st.selectbox("Type", ["auto","email","teams","prd","reply"],
                            key="qc_type", label_visibility="collapsed")

    if st.button("⚡ Generate", key="qc_gen", use_container_width=True):
        if qc_input.strip():
            with st.spinner("Drafting..."):
                result = run_with_tone(qc_input, qc_type if qc_type!="auto" else "manual", "", "")
                st.session_state.qc_result = result

    if st.session_state.qc_result:
        r = st.session_state.qc_result
        c = r.get("critique",{})

        # Score pill
        score = c.get("overall",0)
        score_color = "#22c55e" if score>=0.8 else "#f59e0b" if score>=0.65 else "#5a607a"
        st.markdown(f"""
        <div style="display:flex;justify-content:space-between;align-items:center;
                    margin-bottom:6px;">
          <span style="font-size:10px;color:#5a607a;font-family:'DM Mono',monospace;">
            {r.get('classification',{}).get('task_type','—').upper()} ·
            {r.get('classification',{}).get('audience','—').upper()} ·
            {tone_label(tone_val)}
          </span>
          <span style="font-size:12px;font-weight:700;color:{score_color};
                       font-family:'DM Mono',monospace;">{score:.0%}</span>
        </div>
        """, unsafe_allow_html=True)

        qc_edited = st.text_area(
            label            = "qc_edit",
            value            = r.get("final_draft",""),
            height           = 200,
            label_visibility = "collapsed",
            key              = "qc_edited_text"
        )

        qa1, qa2 = st.columns(2)
        if qa1.button("✅ Save", key="qc_save"):
            log_decision(
                r.get("classification",{}).get("task_type","compose"),
                "approved", qc_edited, qc_input, c
            )
            st.success("Saved to log!")
            st.session_state.qc_result = None
        if qa2.button("✖ Clear", key="qc_clear"):
            st.session_state.qc_result = None
            st.rerun()

    st.divider()

    # Live channel status
    st.markdown("""
    <div style="font-size:11px;font-weight:600;color:#3a3f55;
                letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px;">
      Live Channels
    </div>""", unsafe_allow_html=True)

    import requests as _req
    try:
        _health = _req.get("http://localhost:8000/health", timeout=1).json()
        _ws_running = True
        _email_ct = _health.get("emails", 0)
        _teams_ct = _health.get("teams", 0)
        _cal_ct   = _health.get("calendar", 0)
    except Exception:
        _ws_running = False
        _email_ct = _teams_ct = _cal_ct = 0

    if _ws_running:
        st.markdown(f"""
        <div style="font-size:11px;font-family:'DM Mono',monospace;color:#22c55e;">
          ● webhook server online
        </div>
        <div style="font-size:10px;color:#3a3f55;margin-top:3px;">
          📧 {_email_ct} emails · 💬 {_teams_ct} teams · 📅 {_cal_ct} events
        </div>""", unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style="font-size:11px;font-family:'DM Mono',monospace;color:#ef4444;">
          ○ webhook server offline
        </div>""", unsafe_allow_html=True)
        st.caption("Start: `python -m channels.webhook_server`")
        st.caption("Then: `ngrok http 8000`")

    st.divider()

    # Corpus refresh
    st.markdown("""
    <div style="font-size:11px;font-weight:600;color:#3a3f55;
                letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px;">
      Corpus
    </div>""", unsafe_allow_html=True)
    corpus_age = ""
    if st.session_state.corpus_ctx:
        try:
            from datetime import datetime
            extracted = st.session_state.corpus_ctx.get("extracted_at","")
            if extracted:
                age = (datetime.now() - datetime.fromisoformat(extracted)).seconds // 60
                corpus_age = f"{age}m ago"
        except: pass
    st.caption(f"Context: {len(WORKSTREAMS)} workstreams · {len(STAKEHOLDERS)} stakeholders {corpus_age}")
    if st.button("🔄 Refresh Corpus", key="refresh_corpus", use_container_width=True):
        st.session_state.corpus_ctx = None
        st.session_state.news_cache = None
        st.rerun()

    st.divider()

    # Task history mini
    log = st.session_state.decision_log
    if log:
        st.markdown("""
        <div style="font-size:11px;font-weight:600;color:#3a3f55;
                    letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px;">
          Recent
        </div>
        """, unsafe_allow_html=True)
        for entry in reversed(log[-5:]):
            icon = "✅" if entry["action"]=="approved" else "✖"
            st.caption(f"{icon} {entry['task_type']} · {entry['timestamp']}")


# ── Header ────────────────────────────────────────────────────────────────────
now_str = datetime.now().strftime("%A, %B %d · %I:%M %p")

# Auto-refresh every 30 seconds to pick up new emails/teams/calendar
import streamlit as _st_meta
_st_meta.cache_data.clear  # no-op but signals intent
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=30000, key="data_refresh")
except ImportError:
    pass  # install with: pip install streamlit-autorefresh
st.markdown(f"""
<div class="g-header">
  <div>
    <div class="g-htitle">🤖 Alterus</div>
    <div class="g-hsub">{USER_TITLE} · {USER_COMPANY} · {USER_LOCATION}</div>
  </div>
  <div style="text-align:right;font-size:11px;color:#3a3f55;font-family:'DM Mono',monospace;">
    <span class="dot-online"></span>online &nbsp;·&nbsp;
    {tone_label(st.session_state.tone_value)} &nbsp;·&nbsp; {now_str}
  </div>
</div>
""", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────
# Run sentiment analysis on inbox messages (cached per session)
load_sentiments()

unread_e = sum(1 for e in MOCK_EMAILS if e["unread"])
unread_t = sum(1 for m in MOCK_TEAMS if m["unread"])
inbox_label = f"📬  Inbox ({unread_e + unread_t})" if (unread_e + unread_t) > 0 else "📬  Inbox"

tab1,tab2,tab3,tab4,tab5,tab6 = st.tabs([
    "🏠  Dashboard",
    inbox_label,
    "✍️  Create",
    "👥  People",
    "📋  Log",
    "🎥  Zoom",
])


# ════════════════════════════════════════════════════════
# TAB 1 — DASHBOARD
# ════════════════════════════════════════════════════════
with tab1:

    # Row 1: Briefing + Clone Score
    br_col, conf_col = st.columns([3,1], gap="medium")

    with br_col:
        st.markdown("""
        <div class="g-card g-card-accent">
          <div class="g-ch">
            <span style="font-size:18px">☀️</span>
            <span class="g-ct">Daily Briefing</span>
            <span class="g-badge">AI · first person</span>
          </div>
        </div>""", unsafe_allow_html=True)

        today_iso = date.today().isoformat()
        if st.session_state.briefing is None or st.session_state.briefing_date != today_iso:
            with st.spinner("Writing your morning briefing..."):
                prompt = build_system_prompt("strategy")
                brief = generate(prompt, f"""
Today is {datetime.now().strftime('%A, %B %d, %Y')}.
Meetings today: {', '.join(m['title'] for m in TODAY_MEETINGS)}.

Write a concise daily briefing for Ganesh in first person, covering:
1. Top 3 priorities for today
2. Who needs a response today (Jason Wong, Raghu, Jerry, Senthil)
3. One risk to watch
4. One motivational sentence to start the day

Under 120 words. Direct. No fluff. Sound like Ganesh wrote it to himself.
Active workstreams: Customer Engine, CCX Skill Builder, Feature Store, Stardog, ZTSD.
""")
                st.session_state.briefing      = brief
                st.session_state.briefing_date = today_iso

        st.markdown(f'<div class="briefing-text">{st.session_state.briefing}</div>',
                    unsafe_allow_html=True)
        if st.button("🔄 Regenerate Briefing", key="regen_brief"):
            st.session_state.briefing = None
            st.rerun()

    with conf_col:
        st.markdown("""
        <div class="g-card">
          <div class="g-ch">
            <span style="font-size:18px">🎯</span>
            <span class="g-ct">Clone Score</span>
          </div>
        </div>""", unsafe_allow_html=True)

        if st.session_state.clone_score is None:
            with st.spinner("Calibrating..."):
                st.session_state.clone_score = compute_clone_score(
                    "VP level email status update Customer Engine ServiceNow EAI"
                )

        sp = int(st.session_state.clone_score * 100)
        color = "#22c55e" if sp>=80 else "#f59e0b" if sp>=65 else "#ef4444"
        corpus_count = len(list(Path("data/claude_conversations").glob("*.txt"))
                           if Path("data/claude_conversations").exists() else [])
        st.markdown(f"""
        <div class="conf-ring">
          <div class="conf-val" style="background:linear-gradient(135deg,{color},{color}88);
               -webkit-background-clip:text;-webkit-text-fill-color:transparent;">{sp}%</div>
          <div class="conf-label">clone confidence</div>
          <div style="font-size:10px;color:#3a3f55;margin-top:6px;font-family:'DM Mono',monospace;">
            {corpus_count} corpus docs
          </div>
        </div>""", unsafe_allow_html=True)
        st.progress(sp/100)
        st.caption("↑ Improves as you approve drafts")
        if st.button("🔄 Recalibrate", key="recal"):
            st.session_state.clone_score = None; st.rerun()

    st.divider()

    # Row 2: Calendar
    today_display = datetime.now().strftime("%A, %B %d")
    cal_count = len(TODAY_MEETINGS)
    st.markdown(f"""
    <div class="g-ch" style="border-bottom:none;margin-bottom:12px;">
      <span style="font-size:18px">📅</span>
      <span class="g-ct">Today's Calendar</span>
      <span style="font-size:12px;color:#6366f1;font-family:'DM Mono',monospace;margin-left:6px;">
        {today_display}
      </span>
      <span class="g-badge">{cal_count} meetings</span>
    </div>""", unsafe_allow_html=True)

    cal_cols = st.columns(2, gap="medium")
    for i, mtg in enumerate(TODAY_MEETINGS):
        col = cal_cols[i % 2]
        with col:
            st.markdown(f"""
            <div class="cal-item">
              <div class="cal-time">{mtg['time']}</div>
              <div style="flex:1;">
                <div class="cal-title">{mtg['title']}</div>
                <div class="cal-att">👥 {mtg['attendees']}</div>
                <div><span class="cal-dur">⏱ {mtg['duration']}</span></div>
                <div style="font-size:11px;color:#5a607a;margin-top:4px;">
                  🎯 {mtg['goal']}
                </div>
              </div>
            </div>""", unsafe_allow_html=True)

            if st.button("📝 Generate Prep", key=f"cal_btn_{mtg['id']}", use_container_width=True):
                with st.spinner(f"Prepping for {mtg['title']}..."):
                    prompt = build_system_prompt("strategy")
                    attendees_str = mtg["attendees"] if isinstance(mtg["attendees"], str) else ", ".join(mtg["attendees"]) if isinstance(mtg["attendees"], list) else str(mtg["attendees"])
                    meeting_body  = mtg.get("body","")[:400] if mtg.get("body") else "No description provided."

                    # Retrieve history for meeting attendees
                    try:
                        retriever = Retriever()
                        history_results = retriever.multi_search([
                            f"{mtg['title']} meeting discussion",
                            f"{attendees_str[:50]} previous meeting context",
                            f"zoom teams email {attendees_str[:30]}",
                        ], top_k=3)
                        history_context = retriever.format_context(history_results, max_chars=1000)
                    except Exception:
                        history_context = "No previous meeting history available."

                    prep = generate(prompt, f"""
Write a concise meeting prep brief for Ganesh Srinivasan.

MEETING FACTS (use only these — do not invent):
Title: {mtg["title"]}
Time: {mtg["time"]}
Duration: {mtg["duration"]}
Attendees: {attendees_str}
Description: {meeting_body}

PAST CONTEXT WITH THESE ATTENDEES (reference if relevant):
{history_context}
{get_tone_instruction(st.session_state.tone_value)}

Write based on the above:
1. Opening line to kick off
2. 2-3 talking points grounded in actual meeting title + past context
3. One clear decision or outcome needed

Under 150 words. Only use facts from above.
""")
                    st.session_state.cal_prep[mtg["id"]] = prep

            if st.session_state.cal_prep.get(mtg["id"]):
                with st.expander("📋 Meeting Prep", expanded=True):
                    edited_prep = st.text_area("",
                        value=st.session_state.cal_prep[mtg["id"]],
                        height=180, key=f"cal_edit_{mtg['id']}")
                    if st.button("✅ Save", key=f"cal_save_{mtg['id']}"):
                        log_decision("meeting_prep","approved",edited_prep,mtg["title"])
                        st.success("Saved!")

    st.divider()

    # Row 3: Workstreams
    st.markdown("""
    <div class="g-ch" style="border-bottom:none;margin-bottom:12px;">
      <span style="font-size:18px">⚡</span>
      <span class="g-ct">My Workstreams</span>
      <span class="g-badge">6 active</span>
    </div>""", unsafe_allow_html=True)

    ws_cols = st.columns(3, gap="medium")
    for i, ws in enumerate(WORKSTREAMS):
        with ws_cols[i%3]:
            tag_class = "ws-tag-active" if ws["tag"]=="active" else "ws-tag-risk"
            st.markdown(f"""
            <div class="ws-item">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <span class="ws-name">{ws['name']}</span>
                <span class="ws-tag {tag_class}">{ws['tag_label']}</span>
              </div>
              <div class="ws-status">{ws['description']}</div>
              <div style="margin-top:8px;font-size:10px;color:#3a3f55;font-family:'DM Mono',monospace;">
                👤 {ws['owner']} &nbsp;·&nbsp; 📌 {ws['stakeholder']}
              </div>
            </div>""", unsafe_allow_html=True)

            if st.button("📝 Draft Update", key=f"ws_btn_{i}", use_container_width=True):
                st.session_state.ws_active = ws
                st.session_state.ws_drafts[ws["name"]] = None

    if st.session_state.ws_active:
        ws = st.session_state.ws_active
        st.divider()
        st.caption(f"**Drafting:** {ws['name']} → {ws['stakeholder']} | Tone: {tone_label(st.session_state.tone_value)}")
        if st.session_state.ws_drafts.get(ws["name"]) is None:
            with st.spinner(f"Drafting {ws['name']} update..."):
                r = run_with_tone(
                    f"Draft a status update for {ws['name']} workstream to {ws['stakeholder']}. Status: {ws['description']}",
                    "manual","",f"{ws['name']} Status Update"
                )
                st.session_state.ws_drafts[ws["name"]] = r
        r = st.session_state.ws_drafts.get(ws["name"])
        if r:
            c = r.get("critique",{})
            if c:
                m1,m2,m3 = st.columns(3)
                m1.metric("Style",  f"{c.get('style_match',0):.0%}")
                m2.metric("Tone",   f"{c.get('tone_calibration',0):.0%}")
                m3.metric("Score",  f"{c.get('overall',0):.0%}")
            edited = st.text_area("",value=r.get("final_draft",""),height=180,key=f"ws_edit_{ws['name']}")
            render_feedback_buttons(
                key          = f"ws_{ws['name'].replace(' ','_')}",
                draft        = r.get("final_draft",""),
                edited_draft = edited,
                input_text   = ws.get("description",""),
                task_type    = "workstream",
                run_id       = r.get("run_id",""),
            )
            c1,c2,c3 = st.columns(3)
            if c1.button("✅ Approve",key="ws_app"):
                log_decision(ws["name"],"approved",edited,ws["description"],c)
                was_edited = edited.strip() != r.get("final_draft","").strip()
                log_feedback(run_id=r.get("run_id",""),
                             feedback_type="edited" if was_edited else "approved",
                             draft=r.get("final_draft",""), edited_draft=edited,
                             input_text=ws.get("description",""), task_type="workstream")
                st.success("Approved!")
                st.session_state.ws_active=None
            if c2.button("🔄 Retry",key="ws_ret"):
                st.session_state.ws_drafts[ws["name"]]=None; st.rerun()
            if c3.button("✖ Close",key="ws_cls"):
                st.session_state.ws_active=None; st.rerun()

    st.divider()

    # Row 4: News + Videos
    news_col, vid_col = st.columns(2, gap="medium")
    with news_col:
        st.markdown("""
        <div class="g-card">
          <div class="g-ch">
            <span style="font-size:18px">📰</span>
            <span class="g-ct">AI News</span>
            <span class="g-badge g-badge-green">live</span>
          </div>
        </div>""", unsafe_allow_html=True)
        now = datetime.now()
        if (st.session_state.news_cache is None or st.session_state.news_fetched_at is None or
            (now-st.session_state.news_fetched_at).seconds>1800):
            with st.spinner("Fetching..."):
                st.session_state.news_cache=fetch_ai_news(); st.session_state.news_fetched_at=now
        for item in (st.session_state.news_cache or [])[:5]:
            st.markdown(f"""
            <div class="feed-item">
              <div class="feed-title"><a href="{item['link']}" target="_blank"
                   style="color:#dde1f0;text-decoration:none;">{item['title']}</a></div>
              <div class="feed-meta" style="color:#6366f1;">{item['source']}</div>
            </div>""", unsafe_allow_html=True)
        if st.button("🔄 Refresh",key="news_ref"): st.session_state.news_cache=None; st.rerun()

    with vid_col:
        st.markdown("""
        <div class="g-card">
          <div class="g-ch">
            <span style="font-size:18px">▶️</span>
            <span class="g-ct">AI on YouTube</span>
            <span class="g-badge">top 5</span>
          </div>
        </div>""", unsafe_allow_html=True)
        if st.session_state.videos_cache is None:
            with st.spinner("Loading..."): st.session_state.videos_cache=fetch_ai_videos()
        for v in (st.session_state.videos_cache or [])[:5]:
            st.markdown(f"""
            <div class="feed-item">
              <div class="feed-title"><a href="{v['url']}" target="_blank"
                   style="color:#dde1f0;text-decoration:none;">{v['title']}</a></div>
              <div class="feed-meta" style="color:#ef4444;">▶ {v['channel']}</div>
            </div>""", unsafe_allow_html=True)
        if st.button("🔄 Refresh",key="vid_ref"): st.session_state.videos_cache=None; st.rerun()


# ════════════════════════════════════════════════════════
# TAB 2 — INBOX
# ════════════════════════════════════════════════════════
with tab2:
    tone_banner = f"""
    <div style="background:#10131e;border:1px solid #1e2235;border-radius:8px;
                padding:8px 14px;margin-bottom:14px;font-size:11px;
                color:#5a607a;font-family:'DM Mono',monospace;">
      🎛️ Active tone: <span style="color:#6366f1;font-weight:600;">{tone_label(st.session_state.tone_value)}</span>
      &nbsp;·&nbsp; Adjust in the sidebar
    </div>"""
    st.markdown(tone_banner, unsafe_allow_html=True)

    em_col, tm_col = st.columns(2, gap="large")

    with em_col:
        unread_emails = [e for e in MOCK_EMAILS if e.get("unread")]
        read_emails   = [e for e in MOCK_EMAILS if not e.get("unread")]
        unread_e      = len(unread_emails)

        st.markdown(f"""
        <div class="g-card">
          <div class="g-ch">
            <span style="font-size:18px">📧</span>
            <span class="g-ct">Email Inbox</span>
            <span class="g-badge">{unread_e} unread</span>
          </div>
        </div>""", unsafe_allow_html=True)

        email_tab_unread, email_tab_read, email_tab_all = st.tabs([
            f"🔴 Unread ({unread_e})",
            f"✅ Read ({len(read_emails)})",
            f"📋 All ({len(MOCK_EMAILS)})"
        ])

        def render_email_list(emails_to_show, tab_key):
            for email in emails_to_show:
                # Priority scoring
                pri_label   = email.get("priority_label","")
                pri_signals = email.get("priority_signals",[])
                pri_score   = email.get("priority_score", 0)
                pri_color   = "#ef4444" if "High" in pri_label else "#f59e0b" if "Medium" in pri_label else "#22c55e" if "Normal" in pri_label else "#5a607a"
                border_color = "#ef4444" if "High" in pri_label else "#6366f1" if email["unread"] else "#1e2235"
                border = f"border-left:3px solid {border_color};"
            # Get sentiment for this email
            es = st.session_state.email_sentiments.get(email["id"])
            sentiment_badge = ""
            tone_suggest = ""
            if es:
                sentiment_badge = f'<span style="background:{es.color}22;color:{es.color};font-size:10px;padding:2px 7px;border-radius:10px;font-family:DM Mono,monospace;">{es.emoji} {es.sentiment}</span>'
                if es.sentiment in ("tense","frustrated","urgent","pressured"):
                    tone_suggest = f'<div style="font-size:10px;color:#f59e0b;margin-top:3px;">💡 Suggested tone: Diplomatic</div>'
                urgency_dot = '<span style="color:#ef4444;font-size:10px;">🔴 urgent &nbsp;</span>' if es.urgency == "high" else ""
            else:
                urgency_dot = ""
            st.markdown(f"""
            <div class="msg-item" style="{border}">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <span class="msg-sender">{email["from"]}</span>
                <span class="msg-time">{urgency_dot}{email["time"]}</span>
              </div>
              <div style="font-size:12px;color:#8b8fa8;margin-top:2px;">{email["subject"]}</div>
              <div class="msg-preview">{email["preview"]}</div>
              <div style="margin-top:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                {f'<span style="background:{pri_color}22;color:{pri_color};font-size:10px;padding:1px 7px;border-radius:8px;font-family:DM Mono,monospace;font-weight:600;">{pri_label}</span>' if pri_label and pri_score >= 0 else ""}
                {sentiment_badge}
                {f'<span style="font-size:10px;color:#5a607a;font-family:DM Mono,monospace;">{es.tone[:50]}</span>' if es and es.tone else ""}
              </div>
              {f'<div style="font-size:10px;color:#3a3f55;margin-top:3px;">{" · ".join(pri_signals[:2])}</div>' if pri_signals else ""}
              {tone_suggest}
            </div>""", unsafe_allow_html=True)
            if st.button("✍️ Draft Reply",key=f"ebtn_{email['id']}_{tab_key}",use_container_width=True):
                st.session_state.active_email=email; st.session_state.email_draft=None

        with email_tab_unread:
            if unread_emails:
                render_email_list(unread_emails, "unread")
            else:
                st.markdown("""
                <div style="text-align:center;padding:30px;color:#3a3f55;font-size:13px;">
                  ✅ No unread emails
                </div>""", unsafe_allow_html=True)

        with email_tab_read:
            if read_emails:
                render_email_list(read_emails, "read")
            else:
                st.caption("No read emails yet.")

        with email_tab_all:
            render_email_list(MOCK_EMAILS, "all")

        if st.session_state.active_email:
            e = st.session_state.active_email
            st.divider()
            st.caption(f"**To:** {e['from']} · **Re:** {e['subject']} · Tone: {tone_label(st.session_state.tone_value)}")
            if st.session_state.email_draft is None:
                with st.spinner("Drafting... (retrieving email history)"):
                    # Retrieve past email history with this sender
                    try:
                        retriever = Retriever()
                        history_results = retriever.multi_search([
                            f"email {e['from']} history conversation",
                            f"{e['subject']} previous discussion",
                            f"{e['from'].split()[0]} past emails context",
                        ], top_k=3)
                        history_context = retriever.format_context(history_results, max_chars=1500)
                    except Exception:
                        history_context = "No previous email history available."

                    email_context = f"""
INCOMING EMAIL TO REPLY TO:
From: {e["from"]}
Subject: {e["subject"]}
Body: {e["body"]}

PREVIOUS EMAIL HISTORY WITH {e["from"]} (use this for context):
{history_context}

YOUR TASK: Write a reply email FROM Ganesh Srinivasan TO {e["from"]}.
- Ground your reply in the actual email content above
- Reference relevant history from past conversations if applicable
- Do NOT invent facts, projects, or commitments not in this email or history
- Be specific to what {e["from"]} actually asked
"""
                    st.session_state.email_draft = run_with_tone(
                        email_context, "outlook", e["from"], e["subject"]
                    )
            if st.session_state.email_draft:
                r=st.session_state.email_draft; c=r.get("critique",{})
                if c:
                    m1,m2,m3=st.columns(3)
                    m1.metric("Style",f"{c.get('style_match',0):.0%}")
                    m2.metric("Tone",f"{c.get('tone_calibration',0):.0%}")
                    m3.metric("Score",f"{c.get('overall',0):.0%}")
                edited=st.text_area("",value=r.get("final_draft",""),height=220,key=f"eedit_{e['id']}")
                b1,b2,b3=st.columns(3)
                if b1.button("✅ Approve",key=f"eapp_{e['id']}"):
                    log_decision("email","approved",edited,e["body"],c)
                    st.success("Approved!"); st.session_state.active_email=None; st.session_state.email_draft=None
                if b2.button("🔄 Retry",key=f"eret_{e['id']}"):
                    st.session_state.email_draft=None; st.rerun()
                if b3.button("✖ Close",key=f"ecls_{e['id']}"):
                    st.session_state.active_email=None; st.session_state.email_draft=None; st.rerun()

    with tm_col:
        unread_t = sum(1 for m in MOCK_TEAMS if m["unread"])
        st.markdown(f"""
        <div class="g-card">
          <div class="g-ch">
            <span style="font-size:18px">💬</span>
            <span class="g-ct">Teams Messages</span>
            <span class="g-badge g-badge-green">{unread_t} new</span>
          </div>
        </div>""", unsafe_allow_html=True)

        for msg in MOCK_TEAMS:
            border = "border-left:3px solid #22c55e;" if msg["unread"] else ""
            # Get sentiment for this Teams message
            ts = st.session_state.teams_sentiments.get(msg["id"])
            t_badge = ""
            t_suggest = ""
            if ts:
                t_badge = f'<span style="background:{ts.color}22;color:{ts.color};font-size:10px;padding:2px 7px;border-radius:10px;font-family:DM Mono,monospace;">{ts.emoji} {ts.sentiment}</span>'
                if ts.sentiment in ("tense","frustrated","urgent","pressured"):
                    t_suggest = '<div style="font-size:10px;color:#f59e0b;margin-top:3px;">💡 Suggested: Diplomatic</div>'
                t_urgency = '<span style="color:#ef4444;font-size:10px;">🔴 </span>' if ts.urgency == "high" else ""
            else:
                t_urgency = ""
            st.markdown(f"""
            <div class="msg-item" style="{border}">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <span class="msg-sender">{msg["from"]}</span>
                <span class="msg-time">{t_urgency}{msg["time"]}</span>
              </div>
              <div class="msg-preview">{msg["message"][:100]}...</div>
              <div style="margin-top:6px;display:flex;align-items:center;gap:6px;">
                {t_badge}
                {f'<span style="font-size:10px;color:#5a607a;font-family:DM Mono,monospace;">{ts.tone[:50]}...</span>' if ts and ts.tone else ""}
              </div>
              {t_suggest}
            </div>""", unsafe_allow_html=True)
            if st.button("✍️ Draft Reply",key=f"tbtn_{msg['id']}",use_container_width=True):
                st.session_state.active_teams=msg; st.session_state.teams_draft=None

        if st.session_state.active_teams:
            m=st.session_state.active_teams
            st.divider()
            st.caption(f"**Replying to:** {m['from']} · Tone: {tone_label(st.session_state.tone_value)}")
            if st.session_state.teams_draft is None:
                with st.spinner("Drafting..."):
                    st.session_state.teams_draft=run_with_tone(m["message"],"teams",m["from"],"")
            if st.session_state.teams_draft:
                r=st.session_state.teams_draft; c=r.get("critique",{})
                if c:
                    m1,m2=st.columns(2)
                    m1.metric("Tone",f"{c.get('tone_calibration',0):.0%}")
                    m2.metric("Score",f"{c.get('overall',0):.0%}")
                edited=st.text_area("",value=r.get("final_draft",""),height=180,key=f"tedit_{m['id']}")
                render_feedback_buttons(
                    key          = f"teams_{m['id']}",
                    draft        = r.get("final_draft",""),
                    edited_draft = edited,
                    input_text   = m.get("message",""),
                    task_type    = "teams",
                    run_id       = r.get("run_id",""),
                )
                b1,b2,b3=st.columns(3)
                if b1.button("✅ Send",key=f"tapp_{m['id']}"):
                    log_decision("teams","approved",edited,m["message"],c)
                    was_edited = edited.strip() != r.get("final_draft","").strip()
                    log_feedback(run_id=r.get("run_id",""),
                                 feedback_type="edited" if was_edited else "approved",
                                 draft=r.get("final_draft",""), edited_draft=edited,
                                 input_text=m.get("message",""), task_type="teams")
                    st.success("Sent!"); st.session_state.active_teams=None; st.session_state.teams_draft=None
                if b2.button("🔄 Retry",key=f"tret_{m['id']}"):
                    st.session_state.teams_draft=None; st.rerun()
                if b3.button("✖ Close",key=f"tcls_{m['id']}"):
                    st.session_state.active_teams=None; st.session_state.teams_draft=None; st.rerun()


# ════════════════════════════════════════════════════════
# TAB 3 — CREATE
# ════════════════════════════════════════════════════════
with tab3:
    st.markdown(tone_banner, unsafe_allow_html=True)
    prd_col, mirror_col, meet_col = st.columns(3, gap="medium")

    with prd_col:
        st.markdown("""
        <div class="g-card g-card-accent">
          <div class="g-ch">
            <span style="font-size:18px">📄</span>
            <span class="g-ct">PRD Generator</span>
            <span class="g-badge">your format</span>
          </div>
        </div>""", unsafe_allow_html=True)
        prd_topic   = st.text_input("Feature / Product",placeholder="e.g. Feature Store for ML teams",key="prd_topic")
        prd_problem = st.text_area("Problem Statement",placeholder="What are we solving and why now?",height=80,key="prd_problem")
        prd_aud     = st.selectbox("Target audience",["Internal (EAI/EDP)","CCX","GTM","External"],key="prd_aud")
        if st.button("🚀 Generate PRD",key="prd_gen",use_container_width=True):
            if prd_topic:
                with st.spinner("Writing PRD..."):
                    r=run_with_tone(f"Write a full PRD for: {prd_topic}. Problem: {prd_problem}. Audience: {prd_aud}",
                                    "manual","",f"PRD: {prd_topic}")
                    st.session_state.prd_result=r
        if st.session_state.prd_result:
            r=st.session_state.prd_result; c=r.get("critique",{})
            if c:
                p1,p2=st.columns(2)
                p1.metric("Complete",f"{c.get('completeness',0):.0%}")
                p2.metric("Score",f"{c.get('overall',0):.0%}")
            edited=st.text_area("",value=r.get("final_draft",""),height=380,key="prd_edit")
            render_feedback_buttons(
                key          = "prd_draft",
                draft        = r.get("final_draft",""),
                edited_draft = edited,
                input_text   = prd_topic,
                task_type    = "prd",
                run_id       = r.get("run_id",""),
            )
            a1,a2=st.columns(2)
            if a1.button("✅ Save",key="prd_save"):
                log_decision("prd","approved",edited,prd_topic,c)
                was_edited = edited.strip() != r.get("final_draft","").strip()
                log_feedback(run_id=r.get("run_id",""),
                             feedback_type="edited" if was_edited else "approved",
                             draft=r.get("final_draft",""), edited_draft=edited,
                             input_text=prd_topic, task_type="prd")
                st.success("Saved!")
            if a2.button("🔄 Regen",key="prd_regen"):
                st.session_state.prd_result=None; st.rerun()

    with mirror_col:
        st.markdown("""
        <div class="g-card">
          <div class="g-ch">
            <span style="font-size:18px">🪞</span>
            <span class="g-ct">Style Mirror</span>
            <span class="g-badge">rewrite as you</span>
          </div>
        </div>""", unsafe_allow_html=True)
        mirror_input=st.text_area("Paste text to rewrite in your voice",
            placeholder="Paste any draft...",height=140,key="mirror_input")
        mirror_type=st.selectbox("Rewrite as",["Email to VP","Email to peer","Teams message","PRD section","Strategy doc"],key="mirror_type")
        if st.button("🪞 Mirror",key="mirror_gen",use_container_width=True):
            if mirror_input:
                with st.spinner("Rewriting..."):
                    prompt=build_system_prompt("email")
                    result_text=generate(prompt,
                        f"Rewrite as a {mirror_type} in Ganesh's voice. "
                        f"{get_tone_instruction(st.session_state.tone_value)}\n\nOriginal:\n{mirror_input}")
                    st.session_state.mirror_result={"original":mirror_input,"rewritten":result_text}
        if st.session_state.mirror_result:
            mr=st.session_state.mirror_result
            score=compute_clone_score(mr["rewritten"])
            c1,c2=st.columns(2)
            c1.metric("Clone Match",f"{int(score*100)}%")
            c2.metric("Word Δ",f"{abs(len(mr['rewritten'].split())-len(mr['original'].split()))}w")
            edited=st.text_area("Rewritten:",value=mr["rewritten"],height=200,key="mirror_edit")
            with st.expander("📊 Diff"):
                diff=list(difflib.unified_diff(mr["original"].split(),edited.split(),lineterm=""))
                st.code("\n".join(diff[:25]) if diff else "No changes",language="diff")
            if st.button("✅ Use This",key="mirror_use"):
                log_decision("mirror","approved",edited,mirror_input); st.success("Saved!")

    with meet_col:
        st.markdown("""
        <div class="g-card g-card-green">
          <div class="g-ch">
            <span style="font-size:18px">🗓️</span>
            <span class="g-ct">Meeting Prep</span>
            <span class="g-badge g-badge-green">your voice</span>
          </div>
        </div>""", unsafe_allow_html=True)
        meet_title=st.text_input("Meeting",placeholder="e.g. EAI Q2 Planning with Jason Wong",key="meet_title")
        meet_att=st.text_input("Attendees",placeholder="e.g. Jason Wong, Raghu",key="meet_att")
        meet_dur=st.selectbox("Duration",["30 min","60 min","90 min"],key="meet_dur")
        meet_goal=st.text_area("Your goal",placeholder="What do you need to achieve?",height=60,key="meet_goal")
        if st.button("🗓️ Generate Prep",key="meet_gen",use_container_width=True):
            if meet_title:
                with st.spinner("Preparing..."):
                    prompt=build_system_prompt("strategy")
                    prep=generate(prompt,f"""
Meeting prep for Ganesh:
Title: {meet_title} | Attendees: {meet_att} | Duration: {meet_dur} | Goal: {meet_goal}
{get_tone_instruction(st.session_state.tone_value)}

Include: 1) Opening line, 2) 3 key talking points, 3) Likely pushback + response, 4) Decision needed.
Under 180 words. Specific to Ganesh's workstreams.
""")
                    st.session_state.meetprep_result=prep
        if st.session_state.meetprep_result:
            edited=st.text_area("",value=st.session_state.meetprep_result,height=360,key="meet_edit")
            a1,a2=st.columns(2)
            if a1.button("✅ Save",key="meet_save"):
                log_decision("meeting_prep","approved",edited,meet_title); st.success("Saved!")
            if a2.button("🔄 Regen",key="meet_regen"):
                st.session_state.meetprep_result=None; st.rerun()


# ════════════════════════════════════════════════════════
# TAB 4 — PEOPLE
# ════════════════════════════════════════════════════════
with tab4:
    st.markdown(tone_banner, unsafe_allow_html=True)
    st.markdown("""
    <div class="g-ch" style="border-bottom:none;margin-bottom:16px;">
      <span style="font-size:18px">👥</span>
      <span class="g-ct">Stakeholder Pulse</span>
      <span class="g-badge">5 key relationships</span>
    </div>""", unsafe_allow_html=True)

    sk_cols = st.columns(3, gap="medium")
    for i, sk in enumerate(STAKEHOLDERS):
        with sk_cols[i%3]:
            urgency_color = "#ef4444" if "Today" in sk["last"] else "#f59e0b" if "1 day" in sk["last"] else "#5a607a"
            # Aggregate sentiment from inbox messages for this stakeholder
            sk_msgs = [e for e in MOCK_EMAILS if sk["name"].split()[0].lower() in e["from"].lower()]
            sk_msgs += [m for m in MOCK_TEAMS if sk["name"].split()[0].lower() in m["from"].lower()]
            sk_sentiments = []
            for m in sk_msgs:
                mid = m.get("id","")
                s = (st.session_state.email_sentiments.get(mid) or
                     st.session_state.teams_sentiments.get(mid))
                if s: sk_sentiments.append(s)

            rel_health = ""
            if sk_sentiments:
                positive_ct = sum(1 for s in sk_sentiments if s.sentiment in ("positive","appreciative","neutral"))
                negative_ct = sum(1 for s in sk_sentiments if s.sentiment in ("tense","frustrated","pressured","urgent"))
                if positive_ct > negative_ct:
                    rel_health = '<span style="color:#22c55e;font-size:10px;font-family:DM Mono,monospace;">✅ Positive relationship</span>'
                elif negative_ct > positive_ct:
                    rel_health = '<span style="color:#ef4444;font-size:10px;font-family:DM Mono,monospace;">⚠️ Needs attention</span>'
                else:
                    rel_health = '<span style="color:#8b8fa8;font-size:10px;font-family:DM Mono,monospace;">➡️ Neutral</span>'

            st.markdown(f"""
            <div class="sk-card">
              <div class="sk-name">{sk["name"]}</div>
              <div class="sk-role">{sk["role"]}</div>
              <div style="font-size:11px;color:{urgency_color};margin-top:4px;">
                Last: {sk["last"]}
              </div>
              <div style="margin-top:5px;">{rel_health}</div>
              <div style="font-size:12px;color:#f59e0b;margin-top:4px;">⏳ {sk["pending"]}</div>
              <div style="font-size:11px;color:#3a3f55;margin-top:6px;font-style:italic;">
                {sk["notes"]}
              </div>
            </div>""", unsafe_allow_html=True)
            if st.button("✍️ Reach Out",key=f"sk_btn_{i}",use_container_width=True):
                st.session_state.sk_active=sk; st.session_state.sk_drafts[sk["name"]]=None

    if st.session_state.sk_active:
        sk=st.session_state.sk_active
        st.divider()
        st.caption(f"**Outreach to:** {sk['name']} ({sk['role']}) · Tone: {tone_label(st.session_state.tone_value)}")
        if st.session_state.sk_drafts.get(sk["name"]) is None:
            with st.spinner(f"Drafting message to {sk['name']}..."):
                r=run_with_tone(
                    f"Draft proactive outreach to {sk['name']} ({sk['role']}). Pending: {sk['pending']}. Notes: {sk['notes']}",
                    "manual","",f"Outreach to {sk['name']}"
                )
                st.session_state.sk_drafts[sk["name"]]=r
        r=st.session_state.sk_drafts.get(sk["name"])
        if r:
            c=r.get("critique",{})
            if c:
                s1,s2,s3=st.columns(3)
                s1.metric("Style",f"{c.get('style_match',0):.0%}")
                s2.metric("Tone",f"{c.get('tone_calibration',0):.0%}")
                s3.metric("Score",f"{c.get('overall',0):.0%}")
            edited=st.text_area("",value=r.get("final_draft",""),height=200,key=f"sk_edit_{sk['name']}")
            b1,b2,b3=st.columns(3)
            if b1.button("✅ Approve",key=f"sk_app_{i}"):
                log_decision("email","approved",edited,sk["name"],c)
                st.success("Approved!"); st.session_state.sk_active=None
            if b2.button("🔄 Retry",key=f"sk_ret_{i}"):
                st.session_state.sk_drafts[sk["name"]]=None; st.rerun()
            if b3.button("✖ Close",key=f"sk_cls_{i}"):
                st.session_state.sk_active=None; st.rerun()


# ════════════════════════════════════════════════════════
# TAB 5 — LOG
# ════════════════════════════════════════════════════════
with tab5:
    st.markdown("""
    <div class="g-ch" style="border-bottom:none;margin-bottom:16px;">
      <span style="font-size:18px">📋</span>
      <span class="g-ct">Decision Log</span>
      <span class="g-badge">every draft outcome</span>
    </div>""", unsafe_allow_html=True)

    log = st.session_state.decision_log
    if not log:
        st.markdown("""
        <div style="text-align:center;padding:60px 0;color:#3a3f55;">
          <div style="font-size:32px;margin-bottom:12px;">📋</div>
          <div style="font-family:'DM Mono',monospace;font-size:13px;">
            No decisions yet. Approve your first draft to start the log.
          </div>
        </div>""", unsafe_allow_html=True)
    else:
        approved=sum(1 for l in log if l["action"]=="approved")
        rejected=sum(1 for l in log if l["action"]=="rejected")
        avg_score=sum(l.get("score",0) for l in log)/len(log) if log else 0

        s1,s2,s3,s4=st.columns(4)
        s1.metric("Total",len(log))
        s2.metric("✅ Approved",approved)
        s3.metric("❌ Rejected",rejected)
        s4.metric("Avg Score",f"{avg_score:.0%}")

        st.divider()
        filter_type=st.selectbox("Filter",
            ["All","email","teams","prd","mirror","meeting_prep","strategy"],key="log_filter")

        for entry in reversed(log):
            if filter_type!="All" and entry["task_type"]!=filter_type: continue
            action_color={"approved":"#22c55e","rejected":"#ef4444","edited":"#f59e0b"}.get(entry["action"],"#5a607a")
            score=entry.get("score",0)
            score_color="#22c55e" if score>=0.8 else "#f59e0b" if score>=0.65 else "#5a607a"
            tone_str=entry.get("tone","")
            st.markdown(f"""
            <div class="log-row">
              <div style="min-width:90px;">
                <div style="color:{action_color};font-size:10px;font-family:'DM Mono',monospace;font-weight:600;">
                  {entry['action'].upper()}
                </div>
                <div style="font-size:10px;color:#3a3f55;font-family:'DM Mono',monospace;margin-top:3px;">
                  {entry['timestamp']}
                </div>
                <div style="font-size:9px;color:#6366f1;font-family:'DM Mono',monospace;margin-top:3px;">
                  {tone_str}
                </div>
              </div>
              <div style="flex:1;">
                <div style="font-size:10px;background:#1e2235;padding:1px 7px;border-radius:4px;
                            display:inline-block;color:#8b8fa8;font-family:'DM Mono',monospace;margin-bottom:5px;">
                  {entry['task_type']}
                </div>
                <div style="font-size:12px;color:#8b8fa8;">📥 {entry['input']}</div>
                <div style="font-size:11px;color:#5a607a;margin-top:3px;">📝 {entry['draft']}...</div>
              </div>
              <div style="min-width:45px;text-align:right;font-family:'DM Mono',monospace;
                          font-size:14px;font-weight:700;color:{score_color};">
                {f"{score:.0%}" if score else "—"}
              </div>
            </div>""", unsafe_allow_html=True)

        if st.button("🗑️ Clear Log",key="clear_log"):
            st.session_state.decision_log=[]; st.rerun()

    st.divider()

    # ── Self-Healing Section ──────────────────────────────────────────────────
    st.markdown("""
    <div class="g-ch" style="border-bottom:none;margin-bottom:12px;">
      <span style="font-size:18px">🔄</span>
      <span class="g-ct">Self-Healing Layer</span>
      <span class="g-badge">LangSmith powered</span>
    </div>""", unsafe_allow_html=True)

    # Load local feedback stats
    local_fb = load_local_feedback()
    thumbs_up_count   = sum(1 for f in local_fb if f.get("feedback_type")=="thumbs_up")
    thumbs_down_count = sum(1 for f in local_fb if f.get("feedback_type")=="thumbs_down")
    edits_count       = sum(1 for f in local_fb if f.get("was_edited"))
    total_fb          = len(local_fb)

    h1,h2,h3,h4 = st.columns(4)
    h1.metric("Total Feedback", total_fb)
    h2.metric("👍 Thumbs Up",   thumbs_up_count)
    h3.metric("👎 Thumbs Down", thumbs_down_count)
    h4.metric("✏️ Edits",       edits_count)

    if total_fb > 0:
        approval_rate = int((thumbs_up_count / total_fb) * 100) if total_fb else 0
        st.progress(approval_rate / 100,
                    text=f"Approval rate: {approval_rate}% — target: 80%+")

    col_heal1, col_heal2 = st.columns(2)

    with col_heal1:
        if st.button("🔄 Run Batch Healer", key="run_healer",
                     use_container_width=True,
                     help="Analyzes feedback patterns and proposes prompt improvements"):
            if total_fb < 3:
                st.warning("Need at least 3 feedback entries to run healer. "
                           "Give thumbs up/down on some drafts first.")
            else:
                with st.spinner("🧠 Analyzing feedback patterns..."):
                    result = run_batch_healer()
                    st.session_state["healer_result"] = result
                    st.success("Analysis complete!")

    with col_heal2:
        # Load last healer report
        healer_path = Path("data/healer_report.json")
        if healer_path.exists():
            try:
                last_run = json.loads(healer_path.read_text())
                run_time = last_run.get("run_at","")[:16]
                st.caption(f"Last run: {run_time}")
                if st.button("📋 View Last Report", key="view_healer",
                             use_container_width=True):
                    st.session_state["healer_result"] = last_run
            except Exception:
                pass

    # Show healer results
    if st.session_state.get("healer_result"):
        result = st.session_state["healer_result"]
        analysis = result.get("analysis",{})

        if analysis.get("top_issues"):
            st.markdown("**🔍 Issues found:**")
            for issue in analysis["top_issues"]:
                st.markdown(f"- {issue}")

        if analysis.get("prompt_fixes"):
            st.markdown("**💡 Suggested improvements:**")
            for fix in analysis["prompt_fixes"]:
                st.markdown(f"""
                <div style="background:#10131e;border:1px solid #1e2235;border-radius:8px;
                            padding:10px 14px;margin-bottom:6px;">
                  <div style="font-size:12px;font-weight:600;color:#6366f1;">
                    {fix.get('area','')}
                  </div>
                  <div style="font-size:11px;color:#ef4444;margin-top:4px;">
                    Problem: {fix.get('current_problem','')}
                  </div>
                  <div style="font-size:11px;color:#22c55e;margin-top:4px;">
                    Fix: {fix.get('suggested_fix','')}
                  </div>
                </div>""", unsafe_allow_html=True)

        if analysis.get("overall_assessment"):
            st.info(f"💬 {analysis['overall_assessment']}")

        st.caption("ℹ️ Prompt improvements require manual update to agent/persona.py for now. "
                   "Auto-apply coming in next version.")



# ════════════════════════════════════════════════════════
# TAB 6 — ZOOM MEETINGS
# ════════════════════════════════════════════════════════
with tab6:
    st.markdown("""
    <div class="g-ch" style="border-bottom:none;margin-bottom:16px;">
      <span style="font-size:18px">🎥</span>
      <span class="g-ct">Zoom Meeting Intelligence</span>
      <span class="g-badge">local transcripts</span>
    </div>""", unsafe_allow_html=True)

    # ── Controls row ──────────────────────────────────────────────────────────
    ctrl1, ctrl2, ctrl3 = st.columns([2, 1, 1], gap="medium")

    with ctrl1:
        st.caption(f"📁 Watching: `~/Documents/Zoom` · Auto-transcript via cloud recording")

    with ctrl2:
        if st.button("🔍 Scan for New Meetings", key="zoom_scan", use_container_width=True):
            new_meetings = get_new_meetings()
            if new_meetings:
                st.info(f"Found {len(new_meetings)} new meeting(s) with transcripts. Click Process to analyze.")
                st.session_state.zoom_loaded = False
            else:
                st.success("No new meetings to process.")

    with ctrl3:
        if st.button("⚡ Process New Meetings", key="zoom_process", use_container_width=True):
            with st.spinner("Analyzing transcripts..."):
                results = process_all_new_meetings()
                if results:
                    st.success(f"Processed {len(results)} meeting(s)!")
                    st.session_state.zoom_loaded = False
                else:
                    st.info("No new meetings found in ~/Documents/Zoom")

    # Upload transcript manually
    with st.expander("📤 Upload a transcript manually", expanded=False):
        up_col1, up_col2 = st.columns([2, 1])
        with up_col1:
            uploaded_file = st.file_uploader(
                "Upload .vtt or .txt transcript",
                type=["vtt", "txt"],
                key="zoom_upload"
            )
            meeting_name = st.text_input("Meeting name", placeholder="e.g. EAI Sprint Planning", key="zoom_mtg_name")
        with up_col2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🧠 Analyze Upload", key="zoom_analyze_upload", use_container_width=True):
                if uploaded_file and meeting_name:
                    with st.spinner("Analyzing transcript..."):
                        from agent.transcript_analyzer import TranscriptAnalyzer
                        from channels.zoom_watcher import parse_vtt
                        import tempfile, os

                        # Save upload to temp file
                        tmp = tempfile.NamedTemporaryFile(
                            delete=False,
                            suffix=f".{uploaded_file.name.split('.')[-1]}"
                        )
                        tmp.write(uploaded_file.read())
                        tmp.close()

                        if uploaded_file.name.endswith(".vtt"):
                            from pathlib import Path as P
                            segs = parse_vtt(P(tmp.name))
                            from channels.zoom_watcher import vtt_to_transcript_text
                            text = vtt_to_transcript_text(segs)
                        else:
                            text = open(tmp.name).read()
                            segs = []

                        os.unlink(tmp.name)

                        analyzer = TranscriptAnalyzer()
                        result   = analyzer.analyze(
                            transcript_text = text,
                            segments        = segs,
                            meeting_title   = meeting_name,
                            meeting_date    = datetime.now().strftime("%Y-%m-%d"),
                        )
                        result["id"] = f"upload_{meeting_name}"
                        result["folder"] = "uploaded"

                        # Add to session
                        existing = st.session_state.zoom_meetings or []
                        st.session_state.zoom_meetings = [result] + existing
                        st.session_state.zoom_active   = result
                        st.success("Analysis complete! See results below.")
                        st.session_state.zoom_loaded = True

    st.divider()

    # ── Load cached meetings ──────────────────────────────────────────────────
    if not st.session_state.zoom_loaded:
        cached = load_meetings_cache()
        if cached:
            st.session_state.zoom_meetings = cached
        st.session_state.zoom_loaded = True

    meetings_data = st.session_state.zoom_meetings or []

    if not meetings_data:
        # Empty state
        st.markdown("""
        <div style="text-align:center;padding:50px 0;color:#3a3f55;">
          <div style="font-size:40px;margin-bottom:12px;">🎥</div>
          <div style="font-family:'DM Mono',monospace;font-size:13px;line-height:1.8;">
            No processed meetings yet.<br>
            Make sure <b>cloud recording</b> is enabled in Zoom,<br>
            then click <b>Scan → Process</b> after your next meeting.<br><br>
            Or upload a .vtt transcript file above.
          </div>
        </div>""", unsafe_allow_html=True)
    else:
        # ── Meeting list + detail view ────────────────────────────────────────
        list_col, detail_col = st.columns([1, 2], gap="large")

        with list_col:
            st.markdown(f"""
            <div style="font-size:11px;font-weight:600;color:#3a3f55;
                        letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px;">
              {len(meetings_data)} meetings analyzed
            </div>""", unsafe_allow_html=True)

            for mtg in meetings_data:
                sentiment = mtg.get("meeting_sentiment", "neutral")
                sent_color = {"productive":"#22c55e","neutral":"#8b8fa8",
                              "tense":"#ef4444","inconclusive":"#f59e0b"}.get(sentiment,"#8b8fa8")
                action_ct = len(mtg.get("action_items", []))
                is_active = (st.session_state.zoom_active and
                             st.session_state.zoom_active.get("id") == mtg.get("id"))
                border = "border-left:3px solid #6366f1;" if is_active else ""

                st.markdown(f"""
                <div class="msg-item" style="{border}cursor:pointer;">
                  <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="msg-sender">{mtg.get("meeting_title","Meeting")[:35]}</span>
                    <span style="font-size:10px;background:{sent_color}22;color:{sent_color};
                                 padding:1px 6px;border-radius:8px;font-family:DM Mono,monospace;">
                      {sentiment}
                    </span>
                  </div>
                  <div style="font-size:11px;color:#5a607a;margin-top:3px;">
                    📅 {mtg.get("meeting_date","?")} &nbsp;·&nbsp;
                    ✅ {action_ct} action item{"s" if action_ct!=1 else ""}
                  </div>
                  <div style="font-size:11px;color:#3a3f55;margin-top:3px;">
                    👥 {", ".join(mtg.get("participants",[])[:3])}{"..." if len(mtg.get("participants",[]))>3 else ""}
                  </div>
                </div>""", unsafe_allow_html=True)

                if st.button("View", key=f"zoom_view_{mtg.get('id','')[:20]}", use_container_width=True):
                    st.session_state.zoom_active = mtg
                    st.session_state.zoom_followup_draft = None
                    st.rerun()

        with detail_col:
            active = st.session_state.zoom_active

            if not active:
                st.markdown("""
                <div style="text-align:center;padding:40px 0;color:#3a3f55;">
                  <div style="font-size:24px;margin-bottom:8px;">👈</div>
                  <div style="font-family:'DM Mono',monospace;font-size:12px;">
                    Select a meeting to view analysis
                  </div>
                </div>""", unsafe_allow_html=True)
            else:
                sentiment = active.get("meeting_sentiment","neutral")
                sent_color = {"productive":"#22c55e","neutral":"#8b8fa8",
                              "tense":"#ef4444","inconclusive":"#f59e0b"}.get(sentiment,"#8b8fa8")

                # Meeting header
                st.markdown(f"""
                <div style="background:#10131e;border:1px solid #1e2235;border-radius:12px;
                            padding:16px;margin-bottom:14px;">
                  <div style="font-family:'Syne',sans-serif;font-size:16px;font-weight:700;
                              color:#e8eaed;">{active.get("meeting_title","Meeting")}</div>
                  <div style="display:flex;gap:12px;margin-top:8px;flex-wrap:wrap;">
                    <span style="font-size:11px;color:#5a607a;font-family:'DM Mono',monospace;">
                      📅 {active.get("meeting_date","?")}
                    </span>
                    <span style="font-size:11px;background:{sent_color}22;color:{sent_color};
                                 padding:1px 8px;border-radius:8px;font-family:'DM Mono',monospace;">
                      {sentiment}
                    </span>
                    <span style="font-size:11px;color:#5a607a;font-family:'DM Mono',monospace;">
                      👥 {", ".join(active.get("participants",[]))}
                    </span>
                  </div>
                </div>""", unsafe_allow_html=True)

                # Summary
                if active.get("summary"):
                    st.markdown(f"""
                    <div style="background:#10131e;border-left:3px solid #6366f1;
                                border-radius:0 8px 8px 0;padding:12px 16px;margin-bottom:12px;
                                font-size:13px;color:#c8cce0;line-height:1.6;">
                      {active["summary"]}
                    </div>""", unsafe_allow_html=True)

                # Tabs within Zoom detail
                zt1, zt2, zt3, zt4 = st.tabs(["✅ Action Items", "🎯 Decisions", "📧 Follow-up Email", "🔍 Topics"])

                with zt1:
                    items = active.get("action_items", [])
                    if not items:
                        st.caption("No action items extracted.")
                    else:
                        for item in items:
                            pri   = item.get("priority","medium")
                            color = {"high":"#ef4444","medium":"#f59e0b","low":"#22c55e"}.get(pri,"#8b8fa8")
                            st.markdown(f"""
                            <div style="background:#10131e;border:1px solid #1e2235;border-radius:8px;
                                        padding:10px 14px;margin-bottom:6px;
                                        border-left:3px solid {color};">
                              <div style="display:flex;justify-content:space-between;align-items:center;">
                                <span style="font-weight:600;font-size:13px;color:#e8eaed;">
                                  {item.get("owner","?")}
                                </span>
                                <span style="font-size:10px;background:{color}22;color:{color};
                                             padding:1px 7px;border-radius:8px;
                                             font-family:'DM Mono',monospace;">{pri}</span>
                              </div>
                              <div style="font-size:12px;color:#8b8fa8;margin-top:4px;">
                                {item.get("action","?")}
                              </div>
                              <div style="font-size:11px;color:#5a607a;margin-top:3px;
                                          font-family:'DM Mono',monospace;">
                                📅 by {item.get("deadline","TBD")}
                              </div>
                            </div>""", unsafe_allow_html=True)

                with zt2:
                    decisions = active.get("decisions", [])
                    if not decisions:
                        st.caption("No decisions extracted.")
                    else:
                        for d in decisions:
                            st.markdown(f"""
                            <div style="background:#10131e;border:1px solid #1e2235;
                                        border-radius:8px;padding:10px 14px;margin-bottom:6px;">
                              <div style="font-size:13px;color:#e8eaed;">
                                ✅ {d.get("decision","?")}
                              </div>
                              <div style="font-size:11px;color:#5a607a;margin-top:3px;">
                                by {d.get("made_by","group")}
                              </div>
                            </div>""", unsafe_allow_html=True)

                with zt3:
                    if st.session_state.zoom_followup_draft is None:
                        draft_email = active.get("followup_email","")
                    else:
                        draft_email = st.session_state.zoom_followup_draft

                    if active.get("followup_email"):
                        edited_email = st.text_area(
                            "Follow-up email draft:",
                            value=draft_email,
                            height=300,
                            key="zoom_email_edit"
                        )
                        fe1, fe2, fe3 = st.columns(3)
                        # Build mailto from zoom follow-up
                        zoom_participants = active.get("participants", [])
                        zoom_to = ""  # user fills recipient
                        zoom_subject = f"Follow-up: {active.get('meeting_title','Meeting')}"
                        zoom_mailto = make_mailto_link(zoom_to, zoom_subject, edited_email)
                        zoom_owa    = make_outlook_web_link(zoom_to, zoom_subject, edited_email)

                        render_feedback_buttons(
                            key          = f"zoom_{active.get('id','')[:15]}",
                            draft        = active.get("followup_email",""),
                            edited_draft = edited_email,
                            input_text   = active.get("summary",""),
                            task_type    = "zoom_followup",
                        )
                        if fe1.button("✅ Approve", key="zoom_email_approve"):
                            log_decision("zoom_followup","approved",
                                        edited_email, active.get("meeting_title",""))
                            was_edited = edited_email.strip() != active.get("followup_email","").strip()
                            log_feedback(run_id=None,
                                         feedback_type="edited" if was_edited else "approved",
                                         draft=active.get("followup_email",""),
                                         edited_draft=edited_email,
                                         input_text=active.get("summary",""),
                                         task_type="zoom_followup")
                            st.success("Approved! Click Send to open Outlook.")
                        fex1, fex2 = st.columns(2)
                        fex1.link_button("📧 Send via Outlook", url=zoom_mailto,
                                         help="Opens Outlook desktop with draft")
                        fex2.link_button("🌐 Send via OWA", url=zoom_owa,
                                         help="Opens Outlook Web App")
                        if fe2.button("🔄 Regenerate", key="zoom_email_regen"):
                            with st.spinner("Regenerating..."):
                                from agent.transcript_analyzer import TranscriptAnalyzer
                                analyzer = TranscriptAnalyzer()
                                from agent.drafter import generate
                                from agent.transcript_analyzer import FOLLOWUP_EMAIL_PROMPT, FOLLOWUP_EMAIL_USER
                                action_items_text = "\n".join([
                                    f"- [{i.get('owner','?')}] {i.get('action','?')} (by {i.get('deadline','TBD')})"
                                    for i in active.get("action_items",[])
                                ])
                                decisions_text = "\n".join([
                                    f"- {d.get('decision','?')}"
                                    for d in active.get("decisions",[])
                                ])
                                new_draft = generate(
                                    FOLLOWUP_EMAIL_PROMPT,
                                    FOLLOWUP_EMAIL_USER.format(
                                        title=active.get("meeting_title",""),
                                        date=active.get("meeting_date",""),
                                        participants=", ".join(active.get("participants",[])),
                                        summary=active.get("summary",""),
                                        action_items_text=action_items_text,
                                        decisions_text=decisions_text,
                                    )
                                )
                                st.session_state.zoom_followup_draft = new_draft
                                st.rerun()
                        if fe3.button("📋 Copy", key="zoom_email_copy"):
                            st.code(edited_email)
                    else:
                        st.caption("No follow-up email generated yet.")

                with zt4:
                    topics = active.get("key_topics", [])
                    if topics:
                        for t in topics:
                            st.markdown(f"""
                            <div style="display:inline-block;background:#1e2235;color:#6366f1;
                                        font-size:12px;padding:4px 12px;border-radius:20px;
                                        margin:3px;font-family:'DM Mono',monospace;">{t}</div>
                            """, unsafe_allow_html=True)
                    else:
                        st.caption("No topics extracted.")

                    if active.get("transcript_preview"):
                        with st.expander("📝 Transcript preview"):
                            st.text(active["transcript_preview"])

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.markdown(
    "<div style='text-align:center;color:#1e2235;font-size:10px;font-family:DM Mono,monospace;'>"
    "Alterus v3.1 · Dashboard · Inbox · Create · People · Log · Calendar · Quick Compose · Tone Dial · Zoom Intelligence"
    "</div>",
    unsafe_allow_html=True
)

