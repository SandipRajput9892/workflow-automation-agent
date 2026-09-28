// The last workflow opened, so the sidebar can link back to its live view.
// Wrapped in try/catch: storage can be unavailable (private mode, blocked site data).
const KEY = "wfa:lastWorkflowId";
export const RECENT_EVENT = "wfa:recent-workflow";

export function lastWorkflowId() {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function rememberWorkflow(id) {
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* non-essential */
  }
  window.dispatchEvent(new CustomEvent(RECENT_EVENT, { detail: id }));
}
