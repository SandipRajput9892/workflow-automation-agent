# Workflow Agent: web UI

React 19 + Vite 6 + Tailwind CSS 4 front end for the workflow-automation backend. With it you can:

- submit a task in plain language;
- watch it execute live over a WebSocket;
- approve or reject high-risk plans;
- browse history;
- check automation metrics.

## Setup

Requirements: **Node.js 18, 20 or 22** (an LTS release is recommended) and the backend running on `http://localhost:8000`.

```powershell
# 1. start the backend (from the project root, in another terminal)
.\run_backend.ps1            # or ./run_backend.sh

# 2. install and start the UI
cd frontend
npm install
npm run dev                  # http://localhost:5173
```

The UI talks to `http://localhost:8000` by default. To point it elsewhere, copy `.env.example` to `.env` and set `VITE_API_URL`. The WebSocket URL is derived from it (`http` → `ws`, `https` → `wss`).

If you restrict the backend's `CORS_ORIGINS`, include the UI's origin (e.g. `["http://localhost:5173"]`).

Production build: `npm run build` (output in `dist/`), preview with `npm run preview`.

With Docker (from the project root): `docker compose up --build`, then open http://localhost:8080. The image builds the UI with `VITE_API_URL=/api`, and its nginx forwards `/api` (including the WebSocket) to the backend container. `VITE_API_URL` can be absolute (`http://host:8000`) or relative to the page (`/api`).

## Pages

| Route | Page | What it does |
|---|---|---|
| `/` | **Dashboard** | Metric cards (auto-completed %, needed approval %, avg steps, avg completion time) and a bar chart of workflows by outcome: auto-completed, needed approval, failed. The chart has a table view. Data from `GET /metrics`, refreshed every 30s. |
| `/submit` | **New task** | Textarea (Ctrl+Enter submits) plus example tasks. Posts to `POST /workflows?wait=false`, shows the new `workflow_id`, then redirects to the live view. |
| `/workflows` | **History** | Table of workflows (task, status, steps, duration, start time) from `GET /workflows`. Has a status filter and pagination, both kept in the URL. |
| `/workflows/:id` | **Live workflow** | Connects to `WS /ws/workflows/{id}` and renders a `StepTimeline`: pending / running / success / failed / skipped, each with its own icon and label, grouped by plan round. Expand a step to see its exact tool input and result. There's also an activity feed and the final report. When the run pauses for approval, the `ApprovalModal` opens. |

The sidebar's **Live workflow** link goes to the most recently opened workflow.

### Approvals

When a plan needs human sign-off, the workflow pauses and the modal shows:

- the risk level and the safety review's concerns;
- every planned step with its exact input.

Then choose:

- **Approve & run**: the paused run resumes from its checkpoint.
- **Reject with a comment**: the planner writes a revised plan, which may need approval again.
- **Reject without a comment**: the workflow is cancelled.

The buttons call `POST /workflows/{id}/approve?wait=false`, and progress continues on the same WebSocket.

### Live updates

The server sends a `snapshot` of the workflow and then replays every event emitted so far, so opening the page mid-run, or reloading it, shows the complete picture. If the connection drops before the run finishes, the page reconnects with backoff and rebuilds its state from the new snapshot, so steps are never duplicated.

## Structure

```
src/
  api/client.js             axios instance + API calls, WebSocket URL helper
  hooks/useWorkflowSocket.js  WebSocket connection + reducer building the live step view
  components/
    Sidebar.jsx             navigation + backend health indicator
    StepTimeline.jsx        per-round step timeline with status icons
    ApprovalModal.jsx       approve / reject a paused plan
    StatusBadge.jsx         workflow status pill (icon + label)
  pages/                    Dashboard, SubmitTask, WorkflowDetail, WorkflowHistory (lazy-loaded)
  lib/                      formatting helpers, "last opened workflow" storage
```

## Notes

- **Vite 6, not Vite 8:** Vite 8 needs Node ≥ 20.19 / 22.12. It's pinned to Vite 6 (with `@vitejs/plugin-react` 4) so the UI also builds on Node 18 and 21. After upgrading Node you can move to Vite 8 and plugin-react 6.
- **Dark mode:** follows the operating system setting. The chart colors are validated for both themes.
- **Dates:** times are shown in your local timezone. The history filter's dates are UTC, as in the API.
