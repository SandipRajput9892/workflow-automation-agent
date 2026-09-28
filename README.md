# Workflow Automation Agent

[![CI](https://github.com/SandipRajput9892/workflow-automation-agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SandipRajput9892/workflow-automation-agent/actions/workflows/ci.yml)

An autonomous AI agent that takes a high-level natural-language task, like *"Add Priya from Acme as a lead, mark her contacted, email her a follow-up and tell #sales"*. It plans a multi-step workflow and runs it with email, calendar, CRM and Slack tools, then checks the result against the original task and runs corrective steps until the goal is actually achieved.

- **Reasoning:** Anthropic Claude API (`claude-opus-5`)
- **Orchestration:** LangGraph
- **Long-term memory:** ChromaDB

## Architecture

```
                         ┌──────────────┐
                ┌───────▶│   planner    │◀── similar past runs (ChromaDB)
                │        └──────┬───────┘
 task ──▶  ┌────┴─────┐◀────────┘
           │supervisor│   safety gate ──▶ ┌──────────────┐
           │  (hub)   │──────────────────▶│   executor   │──▶ tools (email, calendar, CRM, Slack)
           │          │◀──────────────────└──────────────┘
           │          │   ┌──────────────┐
           │          │──▶│  reflection  │
           └────┬─────┘◀──└──────────────┘
                ▼
            finalize ──▶ report + memory
```

Every agent reports back to the supervisor, which decides what happens next:

1. **Guardrails in code.** `SupervisorAgent.allowed_moves()` works out which moves are allowed. It never executes without a pending plan, and it enforces the correction and planning budgets.
2. **Claude picks only when there's a real choice.** For example, after reflection proposes a fix, the supervisor can run the corrective plan, replan from scratch, or stop. When only one move is allowed, no Claude call is made.
3. **Every plan passes the safety gate before it runs.** That includes the original plan, corrective plans and replans. The gate has three layers:
   - **Policy checks in code.** A step that exactly repeats an action that already succeeded is blocked. Bulk recipients and bulk messaging raise the risk to high. External recipients are flagged.
   - **Claude's review.** Does the plan do what the task asks and nothing more? It can approve, send the plan back for revision (the planner is told why), or reject it.
   - **Human approval.** `APPROVAL_MODE` controls when it's needed: `never`, `high_risk` (the default) or `always`. On the CLI you're asked in the terminal; `--yes` auto-approves. When no human can be asked, high-risk plans are denied. If a reviewer denies a plan with a comment, the comment becomes the guidance for a revised plan.

| Agent | Role | Claude call |
|---|---|---|
| **Supervisor** | Intake (approve or reject the task, rate its risk), routing between agents, the pre-execution safety gate, and the final report | structured output |
| **Planner** | Breaks the task into tool-grounded steps, using similar past runs from memory | structured output |
| **Executor** | Calls each step's tool directly with the planned input, filling in `<field from step N>` placeholders from earlier results. If a call fails, Claude adjusts the input and the step is retried once | structured output (only on failure) |
| **Reflection** | After a plan has run, reviews the original task against the full execution history. Checks whether the goal was actually achieved (not just that nothing errored), and returns `workflow_complete` or a corrective mini-plan of new steps | structured output |

Hard limits (`EXECUTOR_MAX_RETRIES`, `MAX_CORRECTION_ROUNDS`, `MAX_PLAN_REQUESTS`, `MAX_STEPS`, `MAX_OUTPUT_ATTEMPTS`) guarantee the loop stops.

## Project layout

```
src/
  agents/        supervisor, planner, executor, reflection + base.py (Claude client wrapper)
  tools/         email, calendar, crm, slack tools + tool_registry.py (names, descriptions, input schemas)
  memory/        vector_store.py (ChromaDB workflow memory), history_manager.py (save/find past workflows, run logs, audit log)
  orchestrator.py  LangGraph graph + CLI
  config.py      settings from env/.env
  schemas.py     Pydantic models and graph state
frontend/         React + Vite + Tailwind web UI (see frontend/README.md)
backend/
  main.py        FastAPI app (CORS, lifespan wiring)
  routes/        workflow_routes.py, metrics_routes.py
  db/            database.py (SQLAlchemy engine/sessions), models.py (WorkflowRun, StepLog), crud.py
  services/      workflow_service.py (runs workflows in worker threads, mirrors progress to DB + WebSocket)
  websocket/     live_updates.py (/ws/workflows/{id} + event broker)
  schemas.py     API request/response models
data/
  sample_workflows.json
  mock_apis/     JSON files the mock tools write to (emails, calendar, crm, slack)
tests/           offline tests (scripted fake LLM, no API calls)
logs/            agent.log, audit_log.json (every executor action), runs/<run_id>.jsonl
```

## Setup

Requires Python 3.10+ (the venv here uses 3.12).

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then set ANTHROPIC_API_KEY
```

`requirements.txt` pins the direct dependencies. `requirements.lock` is the full `pip freeze` for exact reproduction.

## Run with Docker

Runs the backend API and the web UI together; only Docker is required.

```powershell
copy .env.example .env      # set ANTHROPIC_API_KEY
docker compose up --build   # add -d to run in the background
```

- **Web UI:** http://localhost:8080
- **API:** http://localhost:8000 (docs at `/docs`)

How it's set up:

- **One origin.** The `frontend` container serves the built UI with nginx and forwards `/api/*`, including the WebSocket, to the `backend` container. The browser talks to a single origin, so no CORS setup is needed.
- **Startup order.** The UI waits until the backend's health check passes.
- **Settings.** The backend reads its settings from `.env`, the same variables as a local run.
- **Persistent data.** It lives in named volumes and survives `docker compose down`:
  - `backend-data`: workflow database, run checkpoints, ChromaDB memory, mock API files.
  - `backend-logs`: `agent.log`, `audit_log.json`, per-run traces.
  - `model-cache`: ChromaDB's embedding model, downloaded on first use.
- **Reset everything:** `docker compose down -v`.
- **Logs:** `docker compose logs -f backend`.
- **Resume an interrupted run:** `docker compose exec backend python -m src.orchestrator --resume <run_id>`.

## Usage

```powershell
python -m src.orchestrator --list                              # sample workflows
python -m src.orchestrator --workflow new_lead_onboarding      # run a sample, with live progress
python -m src.orchestrator "Add Priya (priya@acme.com, Acme) as a lead and tell #sales"

# plans that need human approval (APPROVAL_MODE)
python -m src.orchestrator "..."            # asks in the terminal
python -m src.orchestrator "..." --yes      # auto-approve
python -m src.orchestrator "..." --no-wait  # pause the run and exit; decide later:
python -m src.orchestrator --pending
python -m src.orchestrator --approve <run_id>
python -m src.orchestrator --reject <run_id> --comment "post in #leads instead"   # revise
python -m src.orchestrator --reject <run_id>                                      # cancel

# a run that was interrupted by an error or killed
python -m src.orchestrator --resume <run_id>

# plan only (no tools executed) - prints the Plan as JSON
python -m src.agents.planner_agent "onboard new client Rohan (rohan@nimbus.io, Nimbus Labs): send welcome email, add to CRM, schedule kickoff call on 2026-10-05 at 11:00"
```

From Python:

```python
from src.orchestrator import WorkflowOrchestrator
from src.schemas import ApprovalDecision

orch = WorkflowOrchestrator()                     # no approver: high-risk plans pause
for event in orch.stream("Add Priya ... and tell #sales"):
    print(event.kind, event.message)              # intake, plan_created, gate, step_finished, reflection, ...
run = event.run                                   # the final event carries the WorkflowRun

if run.status == "awaiting_approval":
    run = orch.resume(run.run_id, ApprovalDecision(approved=True), on_event=print)
```

**Checkpointing.** The LangGraph state is saved to SQLite (`data/checkpoints.sqlite`) after every node, keyed by run id. The executor runs one step per node, so a run that crashes or is killed mid-plan resumes at the next unfinished step, and completed tool calls aren't executed again. When a run pauses for approval, the gate's Claude review isn't repeated on resume; only the approval step runs.

The planner checks every plan before returning it: each `tool_input` must be valid JSON, the tool must exist, the arguments must match its input schema, and placeholders like `"<lead_id from step 1>"` may only refer to earlier steps. If the output can't be parsed or any check fails, the planner asks Claude again with a corrective prompt listing the exact problems (up to `MAX_OUTPUT_ATTEMPTS`, default 3). After that it raises `PlanningError`, and the run ends as `failed`. The reflection agent's corrective steps go through the same checks and retries.

Each run prints a final report. The full trace of every plan, tool call and reflection is written to `logs/runs/<run_id>.jsonl`. Every action the executor takes (tool calls, input adjustments, skipped steps) is also appended to `logs/audit_log.json` with a timestamp, step, tool, input and result. Every finished run is saved with `save_workflow_run(task, plan, result)`. It is embedded in ChromaDB (`data/chroma/`) as the task, the final plan (the steps that actually ran, with real IDs and any retry fixes) and the outcome. Before planning, the planner calls `get_similar_past_workflows(task, top_k=3)`. Similar *successful* workflows within `MEMORY_MAX_DISTANCE` are shown to Claude as examples to reuse, with every value taken from the new task.

## Web UI

```powershell
.\run_backend.ps1                         # terminal 1: API on :8000
cd frontend; npm install; npm run dev       # terminal 2: UI on http://localhost:5173
```

Submit tasks, watch them run step by step, approve high-risk plans, browse history and check metrics. Details: [frontend/README.md](frontend/README.md).

## Backend API

```powershell
.\run_backend.ps1          # Windows    (= uvicorn backend.main:app --reload --port 8000)
./run_backend.sh           # macOS/Linux/Git Bash
```

Interactive docs: http://localhost:8000/docs

| Endpoint | Description |
|---|---|
| `POST /workflows` `{"task": "..."}` | Start a workflow. By default the request waits until the run finishes (or pauses for approval) and returns `workflow_id` plus the full result. With `?wait=false` it returns `202` with the id immediately; follow progress over the WebSocket. |
| `GET /workflows/{id}` | Full status: every step (plan round, tool, input, result, attempts, timings), the final report, and the plan waiting for approval, if any. |
| `GET /workflows?status=&from=&to=&limit=&offset=` | Past workflows, newest first. `from` / `to` are dates (inclusive, UTC). |
| `POST /workflows/{id}/approve` `{"approved": true, "comment": "", "approver": "alice"}` | Answer a paused workflow's approval request and resume it from its checkpoint. Declining with a comment asks for a revised plan; declining without one cancels. Supports `?wait=false`. |
| `GET /metrics` | % auto-completed (finished runs that completed with no human approval), % needing approval, average executed steps per finished workflow, average completion time (wall clock, including approval wait). |
| `WS /ws/workflows/{id}` | Sends a `snapshot` of the workflow, then every progress event as JSON (`intake`, `plan_created`, `gate`, `awaiting_approval`, `step_started`, `step_finished`, `reflection`, `run_completed`, ...). Events emitted before you connected are replayed first. The socket closes after a finished run's final event and stays open while a run is paused. Unknown ids are closed with code 4404. |

How it fits together:

- **Threads.** Workflows run in a thread pool (`BACKEND_MAX_CONCURRENT_RUNS`, default 4), never on the event loop. Extra requests queue.
- **Approvals.** The API never prompts anyone. Plans that need approval pause, and `/approve` resumes them.
- **Database.** Progress is written to SQLite (`data/backend.sqlite`) as it happens: a `StepLog` row per step as it starts and finishes, and the `WorkflowRun` row is re-synced from the orchestrator's final state.
- **Restarts.** Workflows left `running` by a previous server process are marked `interrupted` at startup. The CLI can continue them with `python -m src.orchestrator --resume <id>`.

## Tools

| Tool | Arguments | Writes to |
|---|---|---|
| `send_email` | `to`, `subject`, `body` | `emails.json` |
| `create_event` | `title`, `attendees`, `datetime` | `calendar.json` |
| `create_lead` | `name`, `email`, `company` | `crm.json` |
| `update_status` | `lead_id`, `status` | `crm.json` |
| `send_message` | `channel`, `text` | `slack.json` |

Every tool returns a `ToolResult` (`success`, `tool`, `data`, `error`) and never raises. `tool_registry.py` registers each tool with its name, description and a Claude tool-use `input_schema`. The planner reads these schemas to write plans in which each step is a concrete `tool_name` + `tool_input`. The executor calls tools through `ToolRegistry.execute`. Unknown tools, missing, unexpected or mistyped arguments come back as failed `ToolResult`s.

## Tests

```powershell
pytest -q
```

The tests use a scripted fake LLM and offline hash embeddings, so they need no API key or network access.

## Going to production

The mock tools append to the JSON files in `data/mock_apis/`, so you can inspect what a run "did". Reset them by writing `[]` (or `{"leads": []}` for `crm.json`) back into the files.

To connect real services, replace the function bodies in `src/tools/*_tool.py`, for example with the Gmail API, Google Calendar, HubSpot/Salesforce and the Slack Web API. Keep the signatures and the `ToolResult` return type and nothing else needs to change. `ToolRegistry.execute` is the single point every tool call passes through, which makes it the place to add a human-approval gate.

## Notes

- Server-side refusal fallback (`ENABLE_REFUSAL_FALLBACK`) is on by default. If Claude declines a request, the API retries it on a fallback model within the same call. Set it to `false` to use the plain (non-beta) Messages API.
