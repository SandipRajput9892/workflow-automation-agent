import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { Activity, History, LayoutDashboard, SendHorizontal, Workflow } from "lucide-react";
import { API_URL, getHealth } from "../api/client";
import { RECENT_EVENT, lastWorkflowId } from "../lib/recent";

function useBackendHealth() {
  const [ok, setOk] = useState(null);
  useEffect(() => {
    let alive = true;
    const check = () => getHealth().then(() => alive && setOk(true)).catch(() => alive && setOk(false));
    check();
    const t = setInterval(check, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return ok;
}

function useRecentWorkflow() {
  const [id, setId] = useState(lastWorkflowId);
  useEffect(() => {
    const onRecent = (e) => setId(e.detail);
    window.addEventListener(RECENT_EVENT, onRecent);
    return () => window.removeEventListener(RECENT_EVENT, onRecent);
  }, []);
  return id;
}

const linkCls = ({ isActive }) =>
  `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
    isActive
      ? "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
      : "text-stone-600 hover:bg-stone-100 hover:text-stone-900 dark:text-stone-400 dark:hover:bg-stone-800 dark:hover:text-stone-100"
  }`;

export default function Sidebar() {
  const healthy = useBackendHealth();
  const detailId = useRecentWorkflow();

  const items = [
    { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
    { to: "/submit", label: "New task", icon: SendHorizontal },
    { to: "/workflows", label: "History", icon: History, end: true },
  ];

  return (
    <aside className="flex shrink-0 flex-col border-stone-200 bg-white md:h-screen md:w-60 md:border-r dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center gap-2 px-4 py-4">
        <Workflow className="size-6 text-blue-600 dark:text-blue-400" aria-hidden="true" />
        <span className="font-semibold">Workflow Agent</span>
      </div>

      <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:pb-0" aria-label="Main">
        {items.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={linkCls}>
            <Icon className="size-4 shrink-0" aria-hidden="true" /> {label}
          </NavLink>
        ))}
        {detailId ? (
          <NavLink to={`/workflows/${detailId}`} className={linkCls} title={detailId}>
            <Activity className="size-4 shrink-0" aria-hidden="true" /> Live workflow
          </NavLink>
        ) : (
          <span className="flex cursor-not-allowed items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium text-stone-400 dark:text-stone-600" title="Open a workflow first">
            <Activity className="size-4 shrink-0" aria-hidden="true" /> Live workflow
          </span>
        )}
      </nav>

      <div className="mt-auto hidden border-t border-stone-200 px-4 py-3 text-xs text-stone-500 md:block dark:border-stone-800 dark:text-stone-400">
        <div className="flex items-center gap-2">
          <span
            className={`size-2 rounded-full ${healthy == null ? "bg-stone-300" : healthy ? "bg-green-500" : "bg-red-500"}`}
            aria-hidden="true"
          />
          {healthy == null ? "Checking backend…" : healthy ? "Backend connected" : "Backend unreachable"}
        </div>
        <div className="mt-1 truncate" title={API_URL}>{API_URL}</div>
      </div>
    </aside>
  );
}
