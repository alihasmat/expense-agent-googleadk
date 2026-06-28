r"""Ambient expense-approval agent — ADK 2.0 graph Workflow.

Graph shape:

    START
      |
   parse_event            (Python: decode Pub/Sub-style envelope -> Expense)
      |
     route                (Python: the $100 rule lives HERE, not in the LLM)
      |  \
   "AUTO"  "REVIEW"
      |        \
 auto_approve   review_agent      (LLM: risk judgment only)
      |             |
      |        request_human       (HITL: RequestInput pauses the workflow)
      |             |
      |        record_decision     (Python: persist approve/reject outcome)
       \           /
        (leaf nodes -> workflow ends)

Only the risk judgment touches the model. Threshold + routing are deterministic
Python, so the cheap/common path (under $100) never spends an LLM call.
"""

import base64
import json
from datetime import UTC, datetime
from typing import Any

from google.adk import Agent, Event, Workflow
from google.adk.events import RequestInput
from pydantic import BaseModel

from .config import AUTO_APPROVE_THRESHOLD, RISK_REVIEW_MODEL


# --------------------------------------------------------------------------- #
# Data models                                                                 #
# --------------------------------------------------------------------------- #
class Expense(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str


class RiskReview(BaseModel):
    """Structured output from the LLM risk reviewer."""

    risk_level: str  # "LOW" | "MEDIUM" | "HIGH"
    alert: str  # short human-readable flag
    expense: Expense  # carried forward so later nodes keep the details


# --------------------------------------------------------------------------- #
# Node 1 — parse the incoming event (pure Python)                             #
# --------------------------------------------------------------------------- #
def parse_event(node_input: str) -> Expense:
    """Pull the expense out of a JSON event.

    ``node_input`` is annotated ``str`` so the ADK graph runtime coerces the
    incoming message (a ``types.Content`` in the playground, or a raw string
    from an ambient/Pub/Sub trigger) into the text that was supplied.

    The payload sits under "data", which is either:
      * base64-encoded JSON  (real Pub/Sub push), or
      * a plain dict / JSON string (local testing).
    We try base64 first, then fall back to plain JSON.
    """
    event = node_input
    if isinstance(event, (bytes, str)):
        event = json.loads(event)

    data = event["data"]

    if isinstance(data, dict):
        # Local testing: already a dict.
        payload = data
    else:
        # Could be base64 (Pub/Sub) or a plain JSON string.
        try:
            decoded = base64.b64decode(data, validate=True).decode("utf-8")
            payload = json.loads(decoded)
        except Exception:
            payload = json.loads(data)

    return Expense(**payload)


# --------------------------------------------------------------------------- #
# Node 2 — the routing rule (pure Python, NO LLM)                             #
# --------------------------------------------------------------------------- #
def route(node_input: Expense) -> Event:
    """The whole business rule. Tag the event so the graph branches on it."""
    tag = "AUTO" if node_input.amount < AUTO_APPROVE_THRESHOLD else "REVIEW"
    # Pass the Expense through unchanged; just attach the route tag.
    return Event(route=[tag], output=node_input)


# --------------------------------------------------------------------------- #
# Node 3a — auto-approve branch (pure Python)                                 #
# --------------------------------------------------------------------------- #
def auto_approve(node_input: Expense) -> Event:
    record = {
        "decision": "AUTO_APPROVED",
        "amount": node_input.amount,
        "submitter": node_input.submitter,
        "category": node_input.category,
        "decided_at": datetime.now(UTC).isoformat(),
        "decided_by": "system",
    }
    return Event(
        message=f"Auto-approved ${node_input.amount:.2f} for {node_input.submitter}.",
        output=record,
    )


# --------------------------------------------------------------------------- #
# Node 3b — LLM risk reviewer (the ONLY model call)                           #
# --------------------------------------------------------------------------- #
review_agent = Agent(
    name="review_agent",
    model=RISK_REVIEW_MODEL,
    input_schema=Expense,
    output_schema=RiskReview,
    instruction="""You are an expense-risk reviewer. You are given a single
expense (amount, submitter, category, description, date).

Judge it for risk factors only — do NOT decide approval. Look for things like:
round-number padding, vague or mismatched descriptions, categories that don't
fit the amount, weekend/holiday dates, or anything that warrants a closer look.

Return a RiskReview:
  - risk_level: "LOW", "MEDIUM", or "HIGH"
  - alert: one short sentence a human approver should read first
  - expense: echo the input expense back unchanged
""",
)


# --------------------------------------------------------------------------- #
# Node 4 — human-in-the-loop (RequestInput pauses the workflow)              #
# --------------------------------------------------------------------------- #
class HumanDecision(BaseModel):
    decision: str  # expected: "approve" or "reject"


def request_human(node_input: RiskReview):
    """Pause the graph and wait for a human approve/reject decision."""
    exp = node_input.expense
    yield RequestInput(
        message=(
            f"Review required — ${exp.amount:.2f} {exp.category} "
            f"from {exp.submitter} ({exp.date}).\n"
            f"Risk: {node_input.risk_level} — {node_input.alert}\n"
            f'Reply "approve" or "reject".'
        ),
        payload=node_input.model_dump(),
        response_schema=HumanDecision,
    )


# --------------------------------------------------------------------------- #
# Node 5 — record the human outcome (pure Python)                             #
# --------------------------------------------------------------------------- #
def record_decision(node_input: Any) -> Event:
    """Whatever the human typed arrives here; normalize and persist it."""
    raw = node_input
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    if isinstance(raw, str):
        raw = {"decision": raw}

    decision = str(raw.get("decision", "")).strip().lower()
    outcome = "APPROVED" if decision == "approve" else "REJECTED"

    record = {
        "decision": outcome,
        "decided_at": datetime.now(UTC).isoformat(),
        "decided_by": "human",
    }
    return Event(message=f"Human {outcome.lower()} the expense.", output=record)


# --------------------------------------------------------------------------- #
# The graph                                                                   #
# --------------------------------------------------------------------------- #
root_agent = Workflow(
    name="expense_approval_workflow",
    edges=[
        # START -> parse -> route
        ("START", parse_event, route),
        # route branches on the tag set in route()
        (
            route,
            {
                "AUTO": auto_approve,
                "REVIEW": review_agent,
            },
        ),
        # the review branch continues: LLM -> human pause -> record
        (review_agent, request_human, record_decision),
    ],
)