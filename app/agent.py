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
      |    security_screen  (Python: scrub PII + detect prompt injection)
      |       |       \
      |     "SAFE"   "INJECTION"
      |       |          \
      |  to_review_input   \      (Python: unwrap to clean Expense)
      |       |             \
      |  review_agent        \    (LLM: risk judgment, sees scrubbed text only)
      |       |             /
 auto_approve  request_human       (HITL: RequestInput pauses the workflow)
      |             |
      |        record_decision     (Python: persist approve/reject outcome)
       \           /
        (leaf nodes -> workflow ends)

Only the risk judgment touches the model. Threshold + routing are deterministic
Python, so the cheap/common path (under $100) never spends an LLM call. The
security checkpoint runs only on the REVIEW branch, BEFORE the model: it scrubs
SSNs / card numbers from the description and, on detecting prompt-injection,
routes straight to a human without ever showing the text to the LLM.
"""

import base64
import json
import re
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


class ScreenedExpense(BaseModel):
    """An expense after the security checkpoint.

    Carries the (possibly redacted) expense plus an audit trail of what the
    screen did, so downstream nodes — the LLM, the human payload, and the
    final record — all see the same clean, annotated version.
    """

    expense: Expense  # description already scrubbed of PII
    redacted: list[str]  # categories redacted, e.g. ["SSN", "CREDIT_CARD"]
    security_flag: str | None = None  # set when injection is detected


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
# Node 2.5 — security checkpoint (pure Python, NO LLM)                         #
#   Runs on the REVIEW branch only, BEFORE the model sees anything.            #
#   1. Scrubs SSNs / credit-card numbers from the description.                 #
#   2. Detects prompt-injection; if found, routes straight to a human.        #
# --------------------------------------------------------------------------- #

# PII patterns. Kept deliberately conservative — better to over-redact a
# description than to leak a real SSN or PAN into the model or the logs.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# 13-16 digit card numbers, allowing spaces or hyphens between groups.
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")

# Prompt-injection signals: attempts to override the rules, force approval,
# or impersonate system/instruction text inside a free-text description.
_INJECTION_PATTERNS = [
    r"ignore (?:all |any |previous |prior )?instructions",
    r"disregard (?:all |the )?(?:above|previous|prior|rules)",
    r"bypass (?:all |any |the )?(?:rules|checks|approval|review)",
    r"auto[- ]?approve",
    r"approve (?:this )?(?:immediately|now|without review)",
    r"you are now",
    r"system prompt",
    r"\bact as\b",
    r"override",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def _scrub_pii(text: str) -> tuple[str, list[str]]:
    """Redact SSNs and card numbers. Returns (clean_text, categories)."""
    redacted: list[str] = []
    if _SSN_RE.search(text):
        text = _SSN_RE.sub("[REDACTED_SSN]", text)
        redacted.append("SSN")
    if _CC_RE.search(text):
        text = _CC_RE.sub("[REDACTED_CREDIT_CARD]", text)
        redacted.append("CREDIT_CARD")
    # Collapse any doubled/again-missing spacing left by substitutions.
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\](\w)", r"] \1", text)
    return text.strip(), redacted


def security_screen(node_input: Expense) -> Event:
    """Sanitize and triage an expense before it can reach the LLM.

    Order matters: scrub PII FIRST, so that even the injection-flag message
    and the human payload only ever contain the cleaned description.
    """
    clean_desc, redacted = _scrub_pii(node_input.description)

    # Work on a copy with the scrubbed description.
    clean_expense = node_input.model_copy(update={"description": clean_desc})

    if _INJECTION_RE.search(clean_desc):
        # Do NOT let the model see this. Route straight to human review.
        screened = ScreenedExpense(
            expense=clean_expense,
            redacted=redacted,
            security_flag=(
                "Prompt-injection patterns detected in the description. "
                "Routed to human review without LLM analysis."
            ),
        )
        return Event(route=["INJECTION"], output=screened)

    # Clean: continue to the LLM reviewer.
    screened = ScreenedExpense(expense=clean_expense, redacted=redacted)
    return Event(route=["SAFE"], output=screened)


# --------------------------------------------------------------------------- #
# Node 3b — LLM risk reviewer (the ONLY model call)                           #
# --------------------------------------------------------------------------- #
def to_review_input(node_input: ScreenedExpense) -> Event:
    """Unwrap the screened payload to the clean Expense the LLM should see."""
    return Event(output=node_input.expense)


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


def request_human(node_input: Any):
    """Pause the graph and wait for a human approve/reject decision.

    Two kinds of input arrive here:
      * RiskReview        — the clean path, after the LLM scored risk.
      * ScreenedExpense   — the injection path, which skipped the LLM.
    Both are normalized into one human-facing prompt.
    """
    raw = node_input
    if isinstance(raw, dict):
        # Reconstruct whichever model this dict represents.
        raw = RiskReview(**raw) if "risk_level" in raw else ScreenedExpense(**raw)

    if isinstance(raw, RiskReview):
        exp = raw.expense
        risk_line = f"Risk: {raw.risk_level} — {raw.alert}"
        security_line = ""
        redacted = []
        payload = raw.model_dump()
    else:  # ScreenedExpense (injection branch)
        exp = raw.expense
        risk_line = "Risk: NOT ASSESSED (LLM bypassed for security)"
        security_line = f"\n⚠ SECURITY: {raw.security_flag}"
        redacted = raw.redacted
        payload = raw.model_dump()

    redaction_line = (
        f"\nRedacted from description: {', '.join(redacted)}" if redacted else ""
    )

    yield RequestInput(
        message=(
            f"Review required — ${exp.amount:.2f} {exp.category} "
            f"from {exp.submitter} ({exp.date}).\n"
            f"{risk_line}"
            f"{security_line}"
            f"{redaction_line}\n"
            f'Reply "approve" or "reject".'
        ),
        payload=payload,
        response_schema=HumanDecision,
    )


# --------------------------------------------------------------------------- #
# Node 5 — record the human outcome (pure Python)                             #
# --------------------------------------------------------------------------- #
def record_decision(node_input: Any) -> Event:
    """Whatever the human submitted arrives here; normalize and persist it."""
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
    # Carry the security flag into the audit record if this came via the
    # injection branch (the resume payload preserves it).
    flag = raw.get("security_flag")
    if flag:
        record["security_flag"] = flag

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
                # Anything needing review goes through the security
                # checkpoint FIRST — the LLM is never the first to see it.
                "REVIEW": security_screen,
            },
        ),
        # security_screen branches on its own SAFE / INJECTION tag
        (
            security_screen,
            {
                # Clean: unwrap to the scrubbed Expense, then the LLM scores it.
                "SAFE": to_review_input,
                # Injection detected: skip the model entirely, go to a human.
                "INJECTION": request_human,
            },
        ),
        # clean path runs the LLM, then joins the human step
        (to_review_input, review_agent, request_human),
        # both paths converge here: human decision -> record outcome
        (request_human, record_decision),
    ],
)