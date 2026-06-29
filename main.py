"""Ambient entry point — runs the expense workflow as a web service.

Instead of a chat UI, events drive the agent: Pub/Sub push messages POST to
``/apps/app/trigger/pubsub`` and each one is fed straight into the graph.

ADK's trigger endpoint already handles the Pub/Sub envelope for us — base64
decoding the ``message.data``, creating a fresh session per event, running the
agent, and returning 200/500 so Pub/Sub knows whether to retry. We only add two
things on top:

  1. Short session ids. Pub/Sub puts the *fully-qualified* subscription path
     (``projects/<p>/subscriptions/<sub>``) in the request, and ADK uses that as
     the ``user_id`` on the session — which makes session records noisy. A small
     middleware rewrites it to just the trailing ``<sub>`` before ADK sees it.
  2. Plain console logging + Cloud telemetry off, per the brief.
"""

import json
import logging
import os

import uvicorn
from google.adk.cli.fast_api import get_fast_api_app
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# --- Logging: standard Python logging to the console -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("expense_agent")

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8080))


def _short_subscription(full: str) -> str:
    """projects/p/subscriptions/expenses-sub -> expenses-sub."""
    return full.rsplit("/", 1)[-1] if full else full


class NormalizeSubscriptionMiddleware(BaseHTTPMiddleware):
    """Rewrite the Pub/Sub ``subscription`` path to a short name in-flight.

    ADK derives the session ``user_id`` from the ``subscription`` field of the
    Pub/Sub push body. We shorten it here, before the trigger route parses the
    request, so session records read e.g. ``expenses-sub`` instead of the full
    ``projects/<proj>/subscriptions/expenses-sub`` path.
    """

    async def dispatch(self, request: Request, call_next):
        is_pubsub = request.url.path.endswith("/trigger/pubsub")
        if is_pubsub and request.method == "POST":
            body = await request.body()
            try:
                payload = json.loads(body)
                full = payload.get("subscription")
                if isinstance(full, str) and "/" in full:
                    short = _short_subscription(full)
                    payload["subscription"] = short
                    new_body = json.dumps(payload).encode("utf-8")

                    # Replace the request stream so downstream sees the edit.
                    async def receive():
                        return {"type": "http.request", "body": new_body}

                    request._receive = receive
                    logger.info("Pub/Sub event: subscription %s -> %s", full, short)
            except (json.JSONDecodeError, AttributeError):
                # Not JSON we can rewrite; let ADK handle/reject it.
                logger.warning("Could not parse Pub/Sub body for normalization")

        return await call_next(request)


# Build the ADK FastAPI app with the Pub/Sub trigger enabled.
#   web=False           -> no chat dev-UI; this is an ambient service
#   trigger_sources     -> mount /apps/{app}/trigger/pubsub
#   otel_to_cloud=False -> no Cloud telemetry export (per the brief)
app = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=False,
    trigger_sources=["pubsub"],
    otel_to_cloud=False,
)

app.add_middleware(NormalizeSubscriptionMiddleware)


if __name__ == "__main__":
    logger.info("Starting ambient expense agent on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)