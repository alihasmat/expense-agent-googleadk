"""Configuration for the ambient expense-approval agent.

Everything that a non-engineer might want to tune — the dollar threshold that
decides auto-approve vs. human review, and the LLM used for risk judgment —
lives here, deliberately kept out of the graph logic in agent.py.
"""

# Expenses strictly under this amount are auto-approved in pure Python, no LLM.
# Expenses >= this amount go to the LLM reviewer + human-in-the-loop.
AUTO_APPROVE_THRESHOLD = 100.0

# The model is ONLY used for the risk judgment on flagged expenses.
# NOTE: verify this model string resolves in your environment. If it 404s,
# fall back to "gemini-flash-latest" (used in the ADK 2.0 docs examples).
RISK_REVIEW_MODEL = "gemini-3.1-flash-lite"
