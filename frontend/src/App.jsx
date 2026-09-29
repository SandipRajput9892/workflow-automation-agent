import { Suspense, lazy } from "react";
import { Link, Route, Routes } from "react-router-dom";
import { Loader2 } from "lucide-react";
import Sidebar from "./components/Sidebar";

// Pages load on demand; the dashboard's charting library stays out of the other pages' bundle.
const Dashboard = lazy(() => import("./pages/Dashboard"));
const SubmitTask = lazy(() => import("./pages/SubmitTask"));
const WorkflowDetail = lazy(() => import("./pages/WorkflowDetail"));
const WorkflowHistory = lazy(() => import("./pages/WorkflowHistory"));
const Knowledge = lazy(() => import("./pages/Knowledge"));

function NotFound() {
  return (
    <div className="py-16 text-center">
      <h1 className="text-lg font-semibold">Page not found</h1>
      <Link to="/" className="mt-2 inline-block text-sm text-blue-600 hover:underline">Go to the dashboard</Link>
    </div>
  );
}

function PageLoading() {
  return (
    <div className="flex items-center gap-2 py-16 text-sm text-stone-500">
      <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Loading…
    </div>
  );
}

export default function App() {
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <Sidebar />
      <main className="min-w-0 flex-1 px-4 py-6 md:h-screen md:overflow-y-auto md:px-8 md:py-8">
        <Suspense fallback={<PageLoading />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/submit" element={<SubmitTask />} />
            <Route path="/workflows" element={<WorkflowHistory />} />
            <Route path="/workflows/:id" element={<WorkflowDetail />} />
            <Route path="/knowledge" element={<Knowledge />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}
