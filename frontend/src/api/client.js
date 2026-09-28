import axios from "axios";

// Absolute (http://localhost:8000) or relative to this page (/api, when a reverse
// proxy such as the Docker nginx serves the UI and API from the same origin).
export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");

export const api = axios.create({ baseURL: API_URL, timeout: 30000 });

/** ws(s)://… URL for a backend path, derived from API_URL (http→ws, https→wss). */
export function wsUrl(path) {
  const url = new URL(API_URL + path, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

// wait=false: the backend returns immediately with the id; progress arrives over the WebSocket.
export const createWorkflow = (task) =>
  api.post("/workflows", { task }, { params: { wait: false } }).then((r) => r.data);

export const getWorkflow = (id) => api.get(`/workflows/${encodeURIComponent(id)}`).then((r) => r.data);

export const listWorkflows = (params) => api.get("/workflows", { params }).then((r) => r.data);

export const approveWorkflow = (id, { approved, comment = "", approver = "web" }) =>
  api
    .post(`/workflows/${encodeURIComponent(id)}/approve`, { approved, comment, approver }, { params: { wait: false } })
    .then((r) => r.data);

export const getMetrics = () => api.get("/metrics").then((r) => r.data);

export const getHealth = () => api.get("/health", { timeout: 5000 }).then((r) => r.data);

/** Human-readable message from an axios error (FastAPI puts it in `detail`). */
export function errorMessage(err) {
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  if (err?.code === "ERR_NETWORK") return `Can't reach the backend at ${API_URL}. Is it running?`;
  return err?.message || "Something went wrong";
}
