"""
graph.py
LangGraph agent with ReAct reasoning loop + LangSmith tracing.

Flow:
  INPUT → CLASSIFY → RETRIEVE → REASON → DRAFT → CRITIQUE → HITL / RETRY
"""

import os
import sys
import uuid
from pathlib import Path
from typing import TypedDict, Annotated, Optional
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from langgraph.graph import StateGraph, END
from langsmith import Client as LangSmithClient

from agent.classifier import classify, Classification
from agent.persona    import build_system_prompt
from agent.drafter    import draft_response, check_ollama
from agent.critic     import evaluate_draft, format_critique_for_langsmith, CritiqueResult
from retrieval.retriever import Retriever

# ── LangSmith setup ───────────────────────────────────────────────────────────
os.environ.setdefault("LANGSMITH_TRACING",  os.getenv("LANGSMITH_TRACING", "false"))
os.environ.setdefault("LANGSMITH_PROJECT",  os.getenv("LANGSMITH_PROJECT", "alterus"))

_ls_key = os.getenv("LANGSMITH_API_KEY", "")
try:
    ls_client = LangSmithClient(api_key=_ls_key) if _ls_key else None
except Exception:
    ls_client = None

MAX_RETRIES = 2   # max critique-retry loops before escalating to HITL


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    # Input
    input_text:     str
    source:         str           # manual / outlook / teams
    sender:         str
    subject:        str

    # Classification
    classification: Optional[dict]

    # Retrieval
    retrieved_context: str
    retrieval_queries: list[str]

    # Reasoning
    reasoning_steps: list[str]
    retry_count:     int

    # Draft
    current_draft:  str
    draft_history:  list[str]

    # Critique
    critique:       Optional[dict]

    # Output
    final_draft:    str
    status:         str           # drafting / needs_review / approved / rejected
    run_id:         str


# ── Node functions ────────────────────────────────────────────────────────────

def classify_node(state: AgentState) -> AgentState:
    """Classify the input to determine task type, audience, urgency."""
    print("\n🔍 [CLASSIFY] Analyzing input...")

    c = classify(
        text    = state["input_text"],
        source  = state.get("source", "manual"),
        sender  = state.get("sender", ""),
        subject = state.get("subject", ""),
    )

    print(f"   Task: {c.task_type} | Audience: {c.audience} | "
          f"Urgency: {c.urgency} | Confidence: {c.confidence:.2f}")

    return {
        **state,
        "classification": {
            "task_type":  c.task_type,
            "source":     c.source,
            "urgency":    c.urgency,
            "audience":   c.audience,
            "confidence": c.confidence,
            "reasoning":  c.reasoning,
        }
    }


def retrieve_node(state: AgentState) -> AgentState:
    """Generate retrieval queries and fetch relevant context from corpus."""
    print("\n📚 [RETRIEVE] Searching corpus...")

    task_type = state["classification"]["task_type"]
    audience  = state["classification"]["audience"]

    # Generate multiple targeted queries (ReAct: think about what context you need)
    queries = [
        state["input_text"],
        f"{task_type} {audience} communication style",
        f"stakeholder {audience} update format",
    ]

    # Add task-specific query
    if task_type == "prd":
        queries.append("PRD structure goals success metrics requirements")
    elif task_type == "email":
        queries.append("email tone VP leadership direct concise")
    elif task_type == "teams":
        queries.append("Teams message quick response direct")

    retriever = Retriever()
    results   = retriever.multi_search(queries, top_k=3)
    context   = retriever.format_context(results, max_chars=2500)

    print(f"   Queries: {len(queries)} | Results: {len(results)} chunks retrieved")

    return {
        **state,
        "retrieved_context": context,
        "retrieval_queries": queries,
    }


def reason_node(state: AgentState) -> AgentState:
    """
    ReAct reasoning step — think through the task before drafting.
    Adds reasoning steps that are visible in LangSmith traces.
    """
    print("\n🧠 [REASON] Thinking through the task...")

    c          = state["classification"]
    task_type  = c["task_type"]
    audience   = c["audience"]
    urgency    = c["urgency"]
    retry      = state.get("retry_count", 0)

    steps = []

    # Step 1: Understand the task
    steps.append(f"Task type: {task_type} | Audience: {audience} | Urgency: {urgency}")

    # Step 2: Consider audience calibration
    audience_notes = {
        "vp":       "VP = concise, strategic, no fluff. Max 150 words.",
        "director": "Director = balanced detail and strategy. Max 200 words.",
        "engineer": "Engineer = direct, technical ok, action-oriented.",
        "peer":     "Peer = collaborative, can be conversational.",
        "external": "External = professional, no internal jargon.",
    }
    steps.append(f"Audience calibration: {audience_notes.get(audience, 'Standard professional tone')}")

    # Step 3: Consider retry feedback
    if retry > 0 and state.get("critique"):
        feedback = state["critique"].get("feedback", "")
        steps.append(f"Retry #{retry} — addressing: {feedback}")

    # Step 4: Retrieval assessment
    has_context = bool(state.get("retrieved_context", "").strip())
    steps.append(f"Context available: {'Yes — using corpus examples' if has_context else 'No — using persona card only'}")

    for i, step in enumerate(steps, 1):
        print(f"   {i}. {step}")

    return {
        **state,
        "reasoning_steps": state.get("reasoning_steps", []) + steps,
    }


def draft_node(state: AgentState) -> AgentState:
    """Generate draft using LLM."""
    print("\n✍️  [DRAFT] Generating draft...")

    c         = state["classification"]
    task_type = c["task_type"]
    audience  = c["audience"]
    retry     = state.get("retry_count", 0)

    # Build system prompt with persona + style examples
    system_prompt = build_system_prompt(task_type, state.get("retrieved_context", ""))

    # If retrying, add critique feedback to the user message
    retry_instruction = ""
    if retry > 0 and state.get("critique"):
        feedback = state["critique"].get("feedback", "")
        retry_instruction = f"\n\nPREVIOUS DRAFT FEEDBACK (fix these issues):\n{feedback}"

    draft = draft_response(
        input_text    = state["input_text"] + retry_instruction,
        system_prompt = system_prompt,
        context       = state.get("retrieved_context", ""),
        task_type     = task_type,
    )

    print(f"   Draft generated ({len(draft.split())} words)")

    history = state.get("draft_history", [])
    history.append(draft)

    return {
        **state,
        "current_draft": draft,
        "draft_history": history,
    }


def critique_node(state: AgentState) -> AgentState:
    """Score the draft on style, tone, completeness, conciseness."""
    print("\n🔎 [CRITIQUE] Evaluating draft quality...")

    c      = state["classification"]
    result = evaluate_draft(
        draft      = state["current_draft"],
        task_type  = c["task_type"],
        audience   = c["audience"],
        input_text = state["input_text"],
    )

    critique_dict = format_critique_for_langsmith(result)

    print(f"   Overall: {result.overall:.2f} | "
          f"Passed: {result.passed} | "
          f"Feedback: {result.feedback}")

    # Log to LangSmith
    if ls_client and state.get("run_id"):
        try:
            ls_client.create_feedback(
                run_id  = state["run_id"],
                key     = "critic_score",
                score   = result.overall,
                comment = result.feedback,
            )
        except Exception:
            pass

    return {
        **state,
        "critique": critique_dict,
    }


def router(state: AgentState) -> str:
    """
    Route after critique:
    - Pass + retry limit not hit → hitl
    - Fail + retries remaining   → retry (back to reason)
    - Fail + no retries left     → hitl (escalate with warning)
    """
    critique    = state.get("critique", {})
    retry_count = state.get("retry_count", 0)
    passed      = critique.get("passed", False)

    if passed:
        return "hitl"
    elif retry_count < MAX_RETRIES:
        return "retry"
    else:
        print(f"\n⚠️  Max retries reached — escalating to HITL with low-confidence draft")
        return "hitl"


def retry_node(state: AgentState) -> AgentState:
    """Increment retry counter and loop back to reasoning."""
    return {
        **state,
        "retry_count": state.get("retry_count", 0) + 1,
    }


def hitl_node(state: AgentState) -> AgentState:
    """Prepare draft for human review."""
    print("\n👤 [HITL] Draft ready for your review")

    critique = state.get("critique", {})
    status   = "needs_review"

    return {
        **state,
        "final_draft": state["current_draft"],
        "status":      status,
    }


# ── Build the graph ───────────────────────────────────────────────────────────

def build_graph():
    g = StateGraph(AgentState)

    g.add_node("classify", classify_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("reason",   reason_node)
    g.add_node("draft",    draft_node)
    g.add_node("critique", critique_node)
    g.add_node("retry",    retry_node)
    g.add_node("hitl",     hitl_node)

    g.set_entry_point("classify")
    g.add_edge("classify", "retrieve")
    g.add_edge("retrieve", "reason")
    g.add_edge("reason",   "draft")
    g.add_edge("draft",    "critique")
    g.add_conditional_edges("critique", router, {
        "hitl":  "hitl",
        "retry": "retry",
    })
    g.add_edge("retry",    "reason")
    g.add_edge("hitl",     END)

    return g.compile()


# ── Run the agent ─────────────────────────────────────────────────────────────

def run_agent(
    input_text: str,
    source:     str = "manual",
    sender:     str = "",
    subject:    str = "",
) -> dict:
    """Run the full agent pipeline and return the result."""
    if not check_ollama():
        return {"error": "Ollama not running"}

    graph  = build_graph()
    run_id = str(uuid.uuid4())

    initial_state = AgentState(
        input_text        = input_text,
        source            = source,
        sender            = sender,
        subject           = subject,
        classification    = None,
        retrieved_context = "",
        retrieval_queries = [],
        reasoning_steps   = [],
        retry_count       = 0,
        current_draft     = "",
        draft_history     = [],
        critique          = None,
        final_draft       = "",
        status            = "drafting",
        run_id            = run_id,
    )

    print(f"\n{'═' * 60}")
    print(f"🤖 Ganesh Agent — Run ID: {run_id[:8]}")
    print(f"   Input: {input_text[:80]}")
    print(f"{'═' * 60}")

    result = graph.invoke(initial_state)

    print(f"\n{'═' * 60}")
    print(f"✅ Agent complete | Status: {result['status']}")
    print(f"{'═' * 60}\n")

    return result


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    result = run_agent(
        input_text = "Draft an email to Jason Wong with a status update on Customer Engine — we fixed the dual-agent firing bug by updating the semantic router",
        source     = "manual",
        sender     = "",
        subject    = "",
    )

    print("\n── FINAL DRAFT ───────────────────────────────")
    print(result["final_draft"])
    print("\n── CRITIQUE SCORES ───────────────────────────")
    for k, v in result.get("critique", {}).items():
        print(f"   {k}: {v}")
