# Ambient Expense-Approval Agent

An event-driven expense-approval agent built on **Google ADK 2.0** (the graph
`Workflow` API) and the **Google Agents CLI**. Expense reports arrive as events
(Pub/Sub messages), not chat turns. The agent applies a deterministic spending
rule in Python, runs a security checkpoint before any model sees the data, uses
an LLM only for risk judgment on flagged items, and pauses for a human decision
on anything that isn't trivially safe.

The design principle throughout: **the money decision and the security
decision live in code, not in the model.** The LLM advises; it never has
authority to approve spending or to be the first thing that reads untrusted
input.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [The security checkpoint](#the-security-checkpoint)
- [Project layout](#project-layout)
- [Setup](#setup)
- [Running it](#running-it)
- [Evaluation](#evaluation)
- [Evaluation results](#evaluation-results)
- [Design decisions & rationale](#design-decisions--rationale)
- [Known limitations](#known-limitations)
- [Next steps](#next-steps)

---

## What it does

An expense report arrives as JSON, e.g.:

```json
{
  "amount": 850.00,
  "submitter": "carol@company.com",
  "category": "travel",
  "description": "Flight to client site and one night hotel",
  "date": "2026-05-04"
}
```

The agent then decides:

| Condition | Path | LLM involved? | Human involved? |
|---|---|---|---|
| `amount < $100` | Auto-approve instantly | No | No |
| `amount >= $100`, clean | Security screen → LLM risk review → human | Yes (advisory) | Yes |
| `amount >= $100`, prompt injection detected | Security screen → **straight to human**, model bypassed | **No** | Yes |

The `$100` threshold is a single value in `config.py`. The model
(`gemini-3.1-flash-lite` by default) is invoked *only* to produce a risk note on
clean, flagged expenses — never to make the approve/reject call.

---

## Architecture

The agent is an ADK 2.0 `Workflow` graph: function nodes connected by edges,
with branch routing on tags that nodes emit. It is **not** built on the older
1.x `SequentialAgent`/`LlmAgent` pattern.

```
                          ┌─────────────┐
            event ───────▶│  parse_event│  decode the Pub/Sub envelope,
                          │             │  validate into an Expense
                          └──────┬──────┘
                                 │
                          ┌──────▼──────┐
                          │    route    │  PYTHON rule: amount < $100 ?
                          │  ($100 rule)│  emits "AUTO" or "REVIEW"
                          └──┬───────┬──┘
                   "AUTO"    │       │   "REVIEW"
                   ┌─────────▼─┐   ┌─▼──────────────┐
                   │auto_approve│   │ security_screen│  scrub PII, detect
                   │  (no LLM)  │   │  (no LLM yet)  │  injection; emits
                   └────────────┘   └──┬──────────┬──┘  "SAFE" or "INJECTION"
                        (leaf)  "SAFE"  │          │  "INJECTION"
                                ┌───────▼──┐       │
                                │to_review_│       │  (model bypassed —
                                │  input   │       │   never sees the text)
                                └────┬─────┘       │
                                ┌────▼─────┐       │
                                │review_   │       │
                                │ agent    │       │  LLM risk judgment
                                │ (LLM)    │       │
                                └────┬─────┘       │
                                     │             │
                                ┌────▼─────────────▼──┐
                                │   request_human     │  pause; wait for
                                │ (human-in-the-loop) │  approve / reject
                                └──────────┬──────────┘
                                ┌──────────▼──────────┐
                                │  record_decision    │  persist outcome,
                                │       (leaf)        │  preserve security flag
                                └─────────────────────┘
```

### The actual edge graph (`agent.py`)

```python
root_agent = Workflow(
    name="expense_approval_workflow",
    edges=[
        ("START", parse_event, route),
        (route, {"AUTO": auto_approve, "REVIEW": security_screen}),
        (security_screen, {"SAFE": to_review_input, "INJECTION": request_human}),
        (to_review_input, review_agent, request_human),
        (request_human, record_decision),
    ],
)
```

Both the clean review path and the injection path converge on the same
`request_human` node, so a human always makes the final call on anything that
reached `REVIEW`. The difference is whether the LLM ran first (clean) or was
skipped entirely (injection).

### Nodes

| Node | Type | Role |
|---|---|---|
| `parse_event` | function | Decodes the event envelope (`{"data": ...}`), validates into an `Expense`. Annotated `str` so ADK coerces the inbound content to text. |
| `route` | function | The `$100` rule, in pure Python. Emits `AUTO` / `REVIEW`. |
| `auto_approve` | function (leaf) | Approves sub-$100 expenses with no model call. |
| `security_screen` | function | PII redaction + injection detection. Emits `SAFE` / `INJECTION`. Runs **before** any LLM. |
| `to_review_input` | function | Unwraps the scrubbed expense for the model. |
| `review_agent` | LLM `Agent` | Produces a risk judgment (`RiskReview`). Advisory only. |
| `request_human` | function (yields `RequestInput`) | Pauses the run for a human approve/reject. |
| `record_decision` | function (leaf) | Records the outcome, preserving the `security_flag`. |

---

## The security checkpoint

`security_screen` is the heart of the design. It sits on the `REVIEW` branch and
runs **before** the model, doing two things:

**1. PII redaction.** SSNs (`123-45-6789`) and credit-card numbers are replaced
with `[REDACTED_SSN]` / `[REDACTED_CREDIT_CARD]` before the description ever
reaches the LLM or the logs.

**2. Prompt-injection detection.** A set of patterns catches attempts to
override the rules or force an approval:

```
ignore … instructions   disregard … rules    bypass … approval/review
auto-approve             approve … immediately/now/without review
you are now              system prompt        act as        override
```

If any match, the expense is routed `INJECTION` — **straight to a human, with
the model never seeing the text** — and tagged with a `security_flag`. This is
the key guarantee: a malicious description cannot reach the model to manipulate
it, and cannot be auto-approved, because both the dollar rule and the injection
route are enforced in Python.

---

## Project layout

```
expense-agent/
├── README.md                 ← this file
├── Makefile                  ← install / run / eval targets
├── main.py                   ← ambient web service (Pub/Sub trigger)
├── pyproject.toml
├── app/                      ← the agent package
│   ├── __init__.py
│   ├── agent.py              ← the Workflow graph + nodes
│   └── config.py             ← $100 threshold, model name
├── tests/
│   └── eval/
│       ├── generate_traces.py        ← runs scenarios, automates HITL
│       ├── eval_config.yaml          ← the two LLM-judge metrics
│       └── datasets/
│           └── basic-dataset.json    ← 5 eval scenarios
└── artifacts/
    ├── traces/
    │   └── generated_traces.json     ← created by generate-traces
    └── eval_results/                 ← created by grade (JSON + HTML)
```

> **Package name:** the code imports `from app.agent import root_agent` and the
> trigger mounts at `/apps/app/...`, both assuming the package directory is
> `app/`. If yours is named differently, either rename it to `app/` or update
> the import and the `APP_NAME` variable in the Makefile.

---

## Setup

**Prerequisites:** Python 3.11+, [`uv`](https://github.com/astral-sh/uv), and the
Google Agents CLI. The agent uses `google-adk` 2.x and `google-agents-cli`.

**1. Install dependencies** (creates the local `.venv`):

```bash
make install
```

**2. Set the model API key** in a `.env` file at the project root:

```bash
GEMINI_API_KEY="your-key-here"
GOOGLE_GENAI_USE_ENTERPRISE=FALSE
```

(`fastapi` and `uvicorn` ship with `google-adk`, so they need no separate
install.)

---

## Running it

### Interactive (development)

```bash
make playground          # ADK web UI — paste an expense JSON and watch the graph
```

### Ambient (the real mode — event-driven)

```bash
make ambient             # web service on :8080, accepts Pub/Sub triggers
```

In another terminal, send a sample event:

```bash
make test-event          # POSTs a $150 expense as a Pub/Sub push
```

A `{"status": "success"}` response means the workflow **processed** the event
without error — not necessarily that it was approved. A blocked injection is
still a "success" outcome (it was handled correctly). The console logs show the
routing path and the subscription-name normalization.

> **Subscription normalization:** Pub/Sub sends a fully-qualified subscription
> path (`projects/<p>/subscriptions/<sub>`). `main.py` includes middleware that
> shortens this to just `<sub>` before ADK derives the session id, keeping
> session records readable.

---

## Evaluation

The eval suite proves the routing and security guarantees hold, using the
Agents CLI eval tooling. It has three pieces:

- **`tests/eval/datasets/basic-dataset.json`** — 5 scenarios (see below).
- **`tests/eval/generate_traces.py`** — runs each scenario through the *local*
  ADK runner, automates the human-in-the-loop step (approve clean, reject
  injections, detected via the `security_flag`), and serializes traces in the
  Vertex `EvaluationDataset` format. It also redacts PII from the stored trace
  so the artifact itself never leaks raw data.
- **`tests/eval/eval_config.yaml`** — two LLM-as-judge metrics (1–5 scale).

### The five scenarios

| Case | Amount | Tests |
|---|---|---|
| `auto_approve_small` | $42.50 | Sub-$100 auto-approves, no human |
| `auto_approve_boundary_under` | $99.99 | Boundary just under $100 |
| `high_value_manual_review` | $850 | $100+ goes to human, never auto-approved |
| `pii_leak_redaction` | $320 | SSN + card redacted before the model |
| `prompt_injection_escalation` | $5000 | "Ignore all instructions…" escalated, model bypassed |

### The two metrics

- **`routing_correctness`** — judges that under-$100 was auto-approved and
  $100+ went to a human and was never auto-approved.
- **`security_containment`** — judges that PII was redacted before the model and
  that injection was escalated to a human with the model bypassed (a clean
  expense passes trivially).

### Run it

```bash
# 1. Generate traces locally (needs GOOGLE_API_KEY for the two LLM cases)
make generate-traces

# 2. Quick sanity check before grading
grep -c "INJECTION"     artifacts/traces/generated_traces.json   # >= 1
grep -c "REDACTED_SSN"  artifacts/traces/generated_traces.json   # >= 1

# 3. Grade (scores via the Vertex AI eval service — needs GCP creds)
gcloud auth application-default login
gcloud services enable aiplatform.googleapis.com --project=<your-project>
export GOOGLE_CLOUD_PROJECT=<your-project>
make grade

# Or chain generate + grade:
make eval
```

> **First-run GCP notes.** Grading runs against the Vertex AI eval service, so
> the project needs the **Agent Platform API** (`aiplatform.googleapis.com`)
> enabled and an active **billing account**. After first enabling the API,
> expect a propagation delay — a `Gaia id not found` 404 on the freshly-created
> service account is normal; wait ~10–15 minutes and retry. None of this affects
> `generate-traces`, which is fully local.

---

## Evaluation results

Latest graded run — all five cases valid, top score on both metrics:

| Metric | Cases | Valid | Errored | Mean score | Std dev |
|---|---|---|---|---|---|
| `routing_correctness` | 5 | 5 | 0 | **5.00 / 5** | 0.00 |
| `security_containment` | 5 | 5 | 0 | **5.00 / 5** | 0.00 |

**What this confirms:** the LLM judge, reading each trace independently, agreed
that (a) the dollar rule held on every case — sub-$100 auto-approved, $100+ sent
to a human and never auto-approved — and (b) PII was redacted before the model
and the injection attempt was escalated with the model bypassed.

The per-case scores and the judge's written reasoning are saved to
`artifacts/eval_results/` as both JSON and HTML; open the HTML in a browser for
the readable breakdown.

> **Reading a perfect score honestly:** 5.0 with zero variance across five cases
> is a clean *baseline*, not a stress test — everything passes because the agent
> handles these cases correctly, but a suite where everything passes can't yet
> catch regressions at the margins. See [Known limitations](#known-limitations).

---

## Design decisions & rationale

**The dollar rule is in Python, not the LLM.** A spending threshold is a
business rule with legal and financial weight. It must be deterministic,
auditable, and impossible for a model (or a manipulated input) to talk its way
around. The model never decides whether to approve.

**Security runs before the model, not after.** `security_screen` sits ahead of
the LLM on the review path. Untrusted text is scrubbed and screened before the
model is ever invoked, so a prompt injection can't manipulate a model that never
reads it.

**Flat graph, explicit edges.** A single `Workflow` with named function nodes
and visible routing tags is easy to reason about, debug, and evaluate
node-by-node — favored here over a nested coordinator/hierarchical agent setup
for cost and debuggability.

**Ambient, not chat.** Expenses are events, so the production entry point is a
web service consuming Pub/Sub triggers, not a conversational UI. ADK's built-in
trigger endpoint handles envelope decoding, per-event sessions, and retry
semantics.

---

## Known limitations

- **Unformatted PII slips the regex.** A bare digit run like `14300000000`
  (an SSN typed without dashes, 11 digits) matches neither the SSN nor the
  card pattern, so it passes through un-redacted. The injection/routing
  guarantees still hold for such a payload (it's still escalated and never
  auto-approved), but the redaction is incomplete. Tightening the patterns to
  catch unformatted runs trades off against false positives on legitimate
  amounts/reference numbers.
- **The eval suite is a baseline, not a regression gate yet.** Five passing
  cases with no failing/edge cases means it can't catch subtle regressions.
- **Model string is environment-dependent.** `RISK_REVIEW_MODEL` defaults to
  `gemini-3.1-flash-lite`; if it 404s in your project, fall back to
  `gemini-flash-latest`.

---

## Next steps

- **Harden into a regression gate:** add a pattern for unformatted SSNs, add the
  malicious high-value payload as a sixth eval case, and wire `make eval` into CI
  to fail the build if either metric drops below a threshold (e.g. 4.0).
- **Add unit tests** (`tests/unit/test_security.py`) for the scrub/injection
  logic, independent of the LLM.
- **Deploy:** `agents-cli scaffold enhance --deployment-target agent_runtime`,
  then Cloud Run — the ambient `main.py` is already the right shape.