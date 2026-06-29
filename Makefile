# Makefile — ambient expense-approval agent
#
# Usage:
#   make install     install dependencies into the local .venv (uv sync)
#   make playground   launch the ADK web UI for interactive testing
#   make ambient      run the ambient web service (Pub/Sub triggers) on :8080
#   make test-event   send a sample Pub/Sub push to the running service
#   make lint         run the agents-cli lint suite
#   make generate-traces  run scenarios through the local runner -> traces JSON
#   make grade        score the generated traces with the LLM-judge metrics
#   make eval         generate-traces then grade, end to end

# The ADK app name is the package dir; the trigger route is /apps/app/...
APP_NAME ?= app
PORT     ?= 8080
BASE_URL ?= http://localhost:$(PORT)

# Eval paths
DATASET   ?= tests/eval/datasets/basic-dataset.json
TRACES    ?= artifacts/traces/generated_traces.json
EVAL_CFG  ?= tests/eval/eval_config.yaml
EVAL_OUT  ?= artifacts/eval_results

.PHONY: install playground ambient test-event lint generate-traces grade eval

install:
	agents-cli install

playground:
	agents-cli playground

# Run the ambient service. main.py sits at the project root (next to
# pyproject.toml), so the module is just `main`, run inside the uv venv.
ambient:
	uv run python main.py

# Send a sample Pub/Sub push message. The base64 data decodes to a $150
# client-dinner expense, which routes to security_screen -> review -> HITL.
# subscription is a fully-qualified path on purpose, to show normalization.
test-event:
	curl -s -X POST $(BASE_URL)/apps/$(APP_NAME)/trigger/pubsub \
	  -H "Content-Type: application/json" \
	  -d '{ \
	    "message": { \
	      "data": "eyJhbW91bnQiOiAxNTAuMCwgInN1Ym1pdHRlciI6ICJ1c2VyQGV4YW1wbGUuY29tIiwgImNhdGVnb3J5IjogIm1lYWxzIiwgImRlc2NyaXB0aW9uIjogIkNsaWVudCBkaW5uZXIiLCAiZGF0ZSI6ICIyMDI2LTA2LTA0In0=", \
	      "attributes": {"source": "expense-portal"} \
	    }, \
	    "subscription": "projects/my-project/subscriptions/expense-sub" \
	  }'
	@echo ""

lint:
	agents-cli lint

# Run the 5 scenarios through the LOCAL ADK runner, automating the HITL step
# (approve clean, reject injections), and write traces for grading.
generate-traces:
	uv run python tests/eval/generate_traces.py

# Score the generated traces with the two LLM-judge metrics. NOTE: grading runs
# against the Vertex AI eval service, so it needs GCP creds (ADC) and a project:
#   gcloud auth application-default login
#   export GOOGLE_CLOUD_PROJECT=<your-project>
grade:
	uv run agents-cli eval grade \
	  --traces $(TRACES) \
	  --config $(EVAL_CFG) \
	  --output $(EVAL_OUT)

# End-to-end: generate then grade.
eval: generate-traces grade