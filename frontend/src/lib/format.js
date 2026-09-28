export function formatDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function formatDateTime(value) {
  if (!value) return "—";
  // The API returns UTC; SQLite may drop the offset, so treat offset-less values as UTC.
  const iso = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`;
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatTime(value) {
  if (!value) return "";
  const iso = /[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`;
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatPct(value) {
  return value == null ? "—" : `${value}%`;
}

export const WORKFLOW_STATUSES = [
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "rejected",
  "interrupted",
];

export const FINAL_STATUSES = ["completed", "failed", "rejected"];
