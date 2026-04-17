#!/bin/bash
# start_agent.sh
# Starts all Alterus services in one terminal
# Usage: bash start_agent.sh

cd "$(dirname "$0")"
source venv/bin/activate

echo "╔══════════════════════════════════════════╗"
echo "║   Alterus — Starting All Services   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Kill any existing processes on our ports
lsof -ti:8000 | xargs kill -9 2>/dev/null
lsof -ti:8501 | xargs kill -9 2>/dev/null
sleep 1

# ── Start Ollama ──────────────────────────────────────────────────────────────
echo "🦙 Starting Ollama..."
mkdir -p logs
ollama serve > logs/ollama.log 2>&1 &
OLLAMA_PID=$!
sleep 3
echo "   ✅ Ollama running (pid $OLLAMA_PID)"

# ── Run History Ingest (background, after Ollama is ready) ────────────────────
echo "📚 Running history ingest in background..."
python -m ingest.ingest_history > logs/ingest.log 2>&1 &
INGEST_PID=$!
echo "   ✅ History ingest started (pid $INGEST_PID)"
echo "   📄 Log: logs/ingest.log"

# ── Start Webhook Server ──────────────────────────────────────────────────────
echo "🔗 Starting Webhook Server on port 8000..."
python -m channels.webhook_server > logs/webhook.log 2>&1 &
WEBHOOK_PID=$!
sleep 2
echo "   ✅ Webhook server running (pid $WEBHOOK_PID)"

# ── Start ngrok ───────────────────────────────────────────────────────────────
echo "🌐 Starting ngrok tunnel..."
ngrok http 8000 --log=stdout > logs/ngrok.log 2>&1 &
NGROK_PID=$!
sleep 3

# Extract ngrok URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    tunnels = data.get('tunnels', [])
    for t in tunnels:
        if t.get('proto') == 'https':
            print(t['public_url'])
            break
except:
    print('could not get URL')
" 2>/dev/null)

if [ -n "$NGROK_URL" ] && [ "$NGROK_URL" != "could not get URL" ]; then
    echo "   ✅ ngrok running: $NGROK_URL"
    echo "$NGROK_URL" > logs/ngrok_url.txt
    echo ""
    echo "┌──────────────────────────────────────────────────────┐"
    echo "│  Update Power Automate flows with these URLs:        │"
    echo "│                                                       │"
    echo "│  Email:    $NGROK_URL/webhook/email    │"
    echo "│  Teams:    $NGROK_URL/webhook/teams    │"
    echo "│  Calendar: $NGROK_URL/webhook/calendar │"
    echo "└──────────────────────────────────────────────────────┘"
    echo ""
else
    echo "   ⚠️  ngrok URL not detected — check logs/ngrok.log"
fi

# ── Show ingest status ────────────────────────────────────────────────────────
echo "📊 Current corpus status:"
python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from ingest.embedder import CorpusStore
    from pathlib import Path
    store = CorpusStore(Path('data/chroma_db'))
    stats = store.stats()
    print(f'   Chunks: {stats[\"total_chunks\"]} | Documents: {stats[\"unique_documents\"]}')
except Exception as e:
    print(f'   Could not read corpus: {e}')
" 2>/dev/null
echo ""

# ── Start Streamlit ───────────────────────────────────────────────────────────
echo "🎯 Starting Streamlit UI at http://localhost:8501"
echo ""
echo "════════════════════════════════════════════════════"
echo "  All services running. Press Ctrl+C to stop all."
echo "  Logs: logs/ folder"
echo "  Ingest log: logs/ingest.log"
echo "════════════════════════════════════════════════════"
echo ""

# Trap Ctrl+C to kill all background processes cleanly
cleanup() {
    echo ""
    echo "🛑 Stopping all services..."
    kill $OLLAMA_PID $WEBHOOK_PID $NGROK_PID $INGEST_PID 2>/dev/null
    lsof -ti:8000 | xargs kill -9 2>/dev/null
    lsof -ti:8501 | xargs kill -9 2>/dev/null
    echo "✅ All services stopped."
    exit 0
}
trap cleanup INT TERM

# Run Streamlit in foreground
python -m streamlit run ui/app.py
