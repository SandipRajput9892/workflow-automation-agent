import { AlertTriangle, Ban, CheckCircle2, Hourglass, Loader2, PauseCircle, XCircle } from "lucide-react";

// Status is always icon + label, never color alone.
const STATUS = {
  running: { label: "Running", icon: Loader2, spin: true, cls: "bg-blue-50 text-blue-700 ring-blue-600/20 dark:bg-blue-950 dark:text-blue-300 dark:ring-blue-400/30" },
  awaiting_approval: { label: "Awaiting approval", icon: PauseCircle, cls: "bg-amber-50 text-amber-800 ring-amber-600/25 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-400/30" },
  completed: { label: "Completed", icon: CheckCircle2, cls: "bg-green-50 text-green-800 ring-green-600/20 dark:bg-green-950 dark:text-green-300 dark:ring-green-400/30" },
  failed: { label: "Failed", icon: XCircle, cls: "bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-950 dark:text-red-300 dark:ring-red-400/30" },
  rejected: { label: "Rejected", icon: Ban, cls: "bg-stone-100 text-stone-700 ring-stone-500/25 dark:bg-stone-800 dark:text-stone-300 dark:ring-stone-400/30" },
  interrupted: { label: "Interrupted", icon: AlertTriangle, cls: "bg-orange-50 text-orange-800 ring-orange-600/25 dark:bg-orange-950 dark:text-orange-300 dark:ring-orange-400/30" },
};

export function statusLabel(status) {
  return STATUS[status]?.label ?? status;
}

export default function StatusBadge({ status }) {
  const meta = STATUS[status] ?? { label: status ?? "Unknown", icon: Hourglass, cls: "bg-stone-100 text-stone-700 ring-stone-500/25" };
  const Icon = meta.icon;
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${meta.cls}`}>
      <Icon className={`size-3.5 ${meta.spin ? "animate-spin" : ""}`} aria-hidden="true" />
      {meta.label}
    </span>
  );
}
