"""Generate evaluation traces for the expense agent.

Runs each scenario in ``basic-dataset.json`` through the *local* ADK Workflow
runner, automates the human-in-the-loop approval step, and serializes the
resulting traces into ``artifacts/traces/generated_traces.json`` in the shape
``agents-cli eval grade`` consumes (a Vertex ``EvaluationDataset`` with one
``eval_case`` per scenario, each carrying the full event trail under
``agent_data``).

HITL automation policy:
  * If the run paused because of a prompt-injection escalation, REJECT.
  * Otherwise (clean high-value review), APPROVE.
We tell the two apart by inspecting the interrupt payload, which carries the
``security_flag`` set by ``security_screen`` on the injection branch.

Run:  uv run python tests/eval/generate_traces.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

# Import the workflow. The script is run from the project root, and the agent
# package is ``app`` (adjust if your package dir differs).
from app.agent import root_agent
from google.adk.runners import InMemoryRunner
from google.genai import types
from vertexai._genai.types.common import (
    EvalCase,
    EvaluationDataset,
    ResponseCandidate,
)
from vertexai._genai.types.evals import (
    AgentData,
    AgentEvent,
    ConversationTurn,
)

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "tests" / "eval" / "datasets" / "basic-dataset.json"
OUT = ROOT / "artifacts" / "traces" / "generated_traces.json"

APP_NAME = "expense_eval"
USER_ID = "eval-runner"


# --------------------------------------------------------------------------- #
# HITL decision policy                                                         #
# --------------------------------------------------------------------------- #
def decide(interrupt_payload: Any) -> str:
    """Approve clean requests; reject anything flagged as prompt injection."""
    payload = interrupt_payload or {}
    if isinstance(payload, dict) and payload.get("security_flag"):
        return "reject"
    return "approve"


# --------------------------------------------------------------------------- #
# Event serialization                                                          #
# --------------------------------------------------------------------------- #
def event_to_dict(event: Any) -> dict[str, Any]:
    """Flatten an ADK event into a JSON-safe trace turn.

    The very first node (``parse_event``) emits the *un-scrubbed* Expense, so we
    redact PII from serialized output here too — otherwise the trace artifact on
    disk would leak the raw SSN/card even though the model never saw it.
    """
    turn: dict[str, Any] = {"author": getattr(event, "author", None)}

    # The node output / convenience fields we attached via Event(...).
    output = getattr(event, "output", None)
    if output is not None:
        turn["output"] = _redact(_jsonable(output))

    # Routing tag, if this event carried one.
    actions = getattr(event, "actions", None)
    route = getattr(actions, "route", None) if actions else None
    if route:
        turn["route"] = route

    # Any human-readable text content.
    content = getattr(event, "content", None)
    if content and getattr(content, "parts", None):
        texts = [p.text for p in content.parts if getattr(p, "text", None)]
        if texts:
            turn["text"] = _redact(" ".join(texts))

    # Flag long-running / interrupt turns (the HITL pause).
    if getattr(event, "long_running_tool_ids", None):
        turn["interrupt"] = True

    return turn


# Reuse the agent's own PII scrubber so the trace redaction matches production.
def _redact(obj: Any) -> Any:
    from app.agent import _scrub_pii

    if isinstance(obj, str):
        return _scrub_pii(obj)[0]
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# Run a single scenario, automating the HITL resume                           #
# --------------------------------------------------------------------------- #
async def run_case(runner: InMemoryRunner, case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["eval_case_id"]
    prompt_text = case["prompt"]["parts"][0]["text"]

    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )

    turns: list[dict[str, Any]] = []
    final_text = ""

    async def drive(message: types.Content):
        """Run/resume the agent, collecting events; return the interrupt (if any)."""
        nonlocal final_text
        pending = None
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session.id, new_message=message
        ):
            turns.append(event_to_dict(event))
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                t = " ".join(p.text for p in content.parts if getattr(p, "text", None))
                if t:
                    final_text = t
            # Detect a HITL pause: a long-running event carries the interrupt.
            if getattr(event, "long_running_tool_ids", None):
                pending = event
        return pending

    # First pass: feed the expense JSON as the user message.
    first_msg = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    pending = await drive(first_msg)

    # If the workflow paused for human input, automate the decision and resume.
    decision = None
    if pending is not None:
        interrupt_id, payload = _extract_interrupt(pending)
        decision = decide(payload)
        turns.append({"author": "eval-harness", "human_decision": decision})

        resume_msg = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=interrupt_id,
                        name="request_human",
                        response={"decision": decision},
                    )
                )
            ],
        )
        await drive(resume_msg)

    return {
        "eval_case_id": case_id,
        "prompt": case["prompt"],
        "agent_data": {
            "turns": turns,
            "human_decision": decision,
        },
        "response": final_text,
    }


def _extract_interrupt(event: Any) -> tuple[str, Any]:
    """Pull the interrupt id and its payload from a paused event."""
    interrupt_id = None
    payload = None
    content = getattr(event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            fc = getattr(part, "function_call", None)
            if fc is not None:
                interrupt_id = getattr(fc, "id", None)
                args = getattr(fc, "args", None) or {}
                payload = args.get("payload", args)
                break
    # Fallback to the long-running id set on the event.
    if interrupt_id is None:
        ids = getattr(event, "long_running_tool_ids", None) or set()
        interrupt_id = next(iter(ids), None)
    return interrupt_id, payload


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
async def main() -> None:
    data = json.loads(DATASET.read_text())
    cases = data["eval_cases"]

    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)

    eval_cases = []
    for case in cases:
        print(f"  running {case['eval_case_id']} ...")
        result = await run_case(runner, case)
        eval_cases.append(_to_vertex_eval_case(result))

    dataset = EvaluationDataset(eval_cases=eval_cases)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(dataset.model_dump_json(indent=2))
    print(f"\nWrote {len(eval_cases)} traces -> {OUT.relative_to(ROOT)}")


def _to_vertex_eval_case(result: dict[str, Any]) -> EvalCase:
    """Serialize one run into the Vertex ``EvalCase`` shape ``grade`` expects.

    The flat trace turns become ``agent_data.turns[0].events`` (an
    ``AgentEvent`` each), with routing tags / interrupt flags carried in
    ``state_delta`` and human-readable content in ``content``. PII was already
    redacted upstream in ``event_to_dict``.
    """
    events = []
    for t in result["agent_data"]["turns"]:
        bits = []
        if t.get("text"):
            bits.append(t["text"])
        if t.get("output") is not None:
            bits.append("output=" + json.dumps(t["output"]))
        if t.get("human_decision"):
            bits.append("human_decision=" + t["human_decision"])
        content = types.Content(
            role="model",
            parts=[types.Part(text=" | ".join(bits) or "(no text)")],
        )
        state_delta: dict[str, Any] = {}
        if t.get("route"):
            state_delta["route"] = t["route"]
        if t.get("interrupt"):
            state_delta["interrupt"] = True
        events.append(
            AgentEvent(
                author=str(t.get("author") or "workflow"),
                content=content,
                state_delta=state_delta or None,
            )
        )

    # Redact PII from the prompt text too — the stored artifact should never
    # carry raw SSNs/cards, even in the original user input.
    prompt_dict = json.loads(json.dumps(result["prompt"]))
    for part in prompt_dict.get("parts", []):
        if part.get("text"):
            part["text"] = _redact(part["text"])

    return EvalCase(
        eval_case_id=result["eval_case_id"],
        prompt=types.Content(**prompt_dict),
        responses=[
            ResponseCandidate(
                response=types.Content(
                    role="model",
                    parts=[types.Part(text=result["response"] or "(no response)")],
                )
            )
        ],
        agent_data=AgentData(turns=[ConversationTurn(turn_index=0, events=events)]),
    )


if __name__ == "__main__":
    # Allow running from the project root regardless of cwd.
    os.chdir(ROOT)
    asyncio.run(main())