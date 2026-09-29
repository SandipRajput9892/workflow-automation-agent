import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { BookOpen, FileText, Loader2, Pencil, Plus, RefreshCw, Save, Search, Trash, Upload, X } from "lucide-react";
import {
  deleteKnowledgeDoc,
  errorMessage,
  getKnowledgeDoc,
  listKnowledge,
  saveKnowledgeDoc,
  searchKnowledge,
} from "../api/client";
import { formatDateTime } from "../lib/format";

// Must match the backend's rule (src/memory/knowledge_base.py NAME_RE).
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(md|txt)$/;
const TEMPLATE = "# Document title\n\nWho this applies to and who owns it.\n\n## First section\n\n- A rule the agent should follow.\n";

const btn =
  "inline-flex items-center gap-2 rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-sm font-medium text-stone-700 hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200 dark:hover:bg-stone-800";
const primaryBtn =
  "inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50";
const input =
  "w-full rounded-lg border border-stone-300 bg-white px-3 py-2 text-sm outline-none placeholder:text-stone-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-stone-700 dark:bg-stone-900";

/** "Refund Policy (v2).MD" -> "refund_policy_v2.md"; null if not a .md/.txt file. */
function fileNameFor(upload) {
  const m = upload.match(/^(.*)\.(md|markdown|txt)$/i);
  if (!m) return null;
  const ext = m[2].toLowerCase() === "txt" ? "txt" : "md";
  const base = m[1].toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^[^a-z0-9]+|_+$/g, "").slice(0, 80);
  return `${base || "document"}.${ext}`;
}

function withExtension(name) {
  const n = name.trim();
  return /\.(md|txt)$/i.test(n) ? n : `${n}.md`;
}

function formatSize(bytes) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function Card({ children, className = "" }) {
  return (
    <section className={`rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900 ${className}`}>
      {children}
    </section>
  );
}

function ErrorNote({ children }) {
  return (
    <p role="alert" className="rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
      {children}
    </p>
  );
}

/** "Which passages would a task retrieve?" */
function RetrievalTester({ onOpen }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async (e) => {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setError("");
    try {
      setHits(await searchKnowledge(q.trim()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-5">
      <h2 className="text-sm font-semibold">Test retrieval</h2>
      <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
        Enter a task to see which passages the agent would retrieve for it.
      </p>
      <form onSubmit={run} className="mt-3 flex gap-2">
        <label htmlFor="kb-search" className="sr-only">Task to test</label>
        <input
          id="kb-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="e.g. Send Priya a follow-up email after our meeting and tell #sales"
          className={input}
        />
        <button type="submit" disabled={!q.trim() || busy} className={primaryBtn}>
          {busy ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : <Search className="size-4" aria-hidden="true" />}
          Search
        </button>
      </form>
      {error && <div className="mt-3"><ErrorNote>{error}</ErrorNote></div>}
      {hits && (
        hits.length === 0 ? (
          <p className="mt-3 text-sm text-stone-500">No relevant passages. The agent would plan without company knowledge.</p>
        ) : (
          <ol className="mt-3 space-y-2">
            {hits.map((h, i) => (
              <li key={i} className="rounded-lg border border-stone-200 p-3 dark:border-stone-800">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <button type="button" onClick={() => onOpen(h.source)} className="text-left text-sm font-medium text-blue-700 hover:underline dark:text-blue-300">
                    {h.title}{h.section && <span className="text-stone-500 dark:text-stone-400"> › {h.section}</span>}
                  </button>
                  <span className="text-xs tabular-nums text-stone-500" title={`cosine distance ${h.distance.toFixed(3)}`}>
                    {Math.max(0, Math.round((1 - h.distance) * 100))}% match
                  </span>
                </div>
                <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs text-stone-600 dark:text-stone-400">{h.text}</p>
              </li>
            ))}
          </ol>
        )
      )}
    </Card>
  );
}

/** View / edit one document. `name` is null when creating a new one. */
function DocumentPanel({ name, existing, onSaved, onDeleted, onCancelNew }) {
  const isNew = name == null;
  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(!isNew);
  const [editing, setEditing] = useState(isNew);
  const [draftName, setDraftName] = useState("");
  const [draft, setDraft] = useState(isNew ? TEMPLATE : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (isNew) return;
    let alive = true;
    setLoading(true);
    setEditing(false);
    setError("");
    getKnowledgeDoc(name)
      .then((d) => alive && (setDoc(d), setDraft(d.content)))
      .catch((err) => alive && setError(errorMessage(err)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [name, isNew]);

  const save = async () => {
    const target = isNew ? withExtension(draftName) : name;
    if (!NAME_RE.test(target)) {
      setError("Use letters, digits, '-' and '_' for the file name, e.g. refund_policy.md");
      return;
    }
    if (isNew && existing.includes(target) && !window.confirm(`${target} already exists. Replace it?`)) return;
    setBusy(true);
    setError("");
    try {
      const saved = await saveKnowledgeDoc(target, draft);
      setDoc(saved);
      setEditing(false);
      onSaved(saved.name);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Delete ${name}? The agent will stop using it immediately.`)) return;
    setBusy(true);
    try {
      await deleteKnowledgeDoc(name);
      onDeleted(name);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <Card className="flex items-center gap-2 p-5 text-sm text-stone-500">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Loading…
      </Card>
    );
  }

  return (
    <Card className="flex min-w-0 flex-col">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-stone-200 px-5 py-4 dark:border-stone-800">
        <div className="min-w-0">
          {isNew ? (
            <>
              <label htmlFor="kb-name" className="text-xs font-medium text-stone-500">File name</label>
              <input
                id="kb-name"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                placeholder="refund_policy.md"
                className={`${input} mt-1 w-64 max-w-full font-mono`}
                autoFocus
              />
            </>
          ) : (
            <>
              <h2 className="truncate font-semibold">{doc?.title}</h2>
              <p className="mt-0.5 text-xs text-stone-500 dark:text-stone-400">
                <code>{name}</code>
                {doc && <> · {doc.chunks} passage{doc.chunks === 1 ? "" : "s"} · {formatSize(doc.size)} · updated {formatDateTime(doc.updated_at)}</>}
              </p>
            </>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {editing ? (
            <>
              <button type="button" className={btn} disabled={busy} onClick={() => (isNew ? onCancelNew() : (setEditing(false), setDraft(doc.content), setError("")))}>
                <X className="size-4" aria-hidden="true" /> Cancel
              </button>
              <button type="button" className={primaryBtn} disabled={busy || !draft.trim() || (isNew && !draftName.trim())} onClick={save}>
                {busy ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : <Save className="size-4" aria-hidden="true" />} Save
              </button>
            </>
          ) : (
            <>
              <button type="button" className={btn} disabled={busy || !doc} onClick={() => setEditing(true)}>
                <Pencil className="size-4" aria-hidden="true" /> Edit
              </button>
              <button
                type="button"
                className={`${btn} text-red-700 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950`}
                disabled={busy || !doc}
                onClick={remove}
              >
                <Trash className="size-4" aria-hidden="true" /> Delete
              </button>
            </>
          )}
        </div>
      </header>

      {error && <div className="px-5 pt-4"><ErrorNote>{error}</ErrorNote></div>}

      <div className="p-5">
        {editing ? (
          <>
            <label htmlFor="kb-content" className="sr-only">Content</label>
            <textarea
              id="kb-content"
              rows={22}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "s" && (e.preventDefault(), save())}
              className={`${input} resize-y font-mono text-[13px] leading-relaxed`}
            />
            <p className="mt-2 text-xs text-stone-500 dark:text-stone-400">
              Markdown. Start with <code># Title</code> and split topics with <code>## Section</code> headings: each section is
              retrieved on its own, so keep related rules together. Ctrl+S saves.
            </p>
          </>
        ) : (
          doc && <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-stone-800 dark:text-stone-200">{doc.content}</pre>
        )}
      </div>
    </Card>
  );
}

export default function Knowledge() {
  const [docs, setDocs] = useState(null);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [params, setParams] = useSearchParams();
  const fileRef = useRef(null);
  const selected = params.get("doc");
  const creating = params.get("new") === "1";

  const select = useCallback((name) => setParams(name ? { doc: name } : {}), [setParams]);

  const load = useCallback(() => {
    setError("");
    return listKnowledge()
      .then(setDocs)
      .catch((err) => setError(errorMessage(err)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Open the first document when nothing is selected.
  useEffect(() => {
    if (docs?.length && !selected && !creating) setParams({ doc: docs[0].name }, { replace: true });
  }, [docs, selected, creating, setParams]);

  const upload = async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    if (!files.length) return;
    setUploading(true);
    setError("");
    const names = docs?.map((d) => d.name) ?? [];
    const problems = [];
    let last = null;
    for (const file of files) {
      const name = fileNameFor(file.name);
      if (!name) {
        problems.push(`${file.name}: only .md and .txt files are supported`);
        continue;
      }
      if (names.includes(name) && !window.confirm(`${name} already exists. Replace it with ${file.name}?`)) continue;
      try {
        await saveKnowledgeDoc(name, await file.text());
        last = name;
      } catch (err) {
        problems.push(`${file.name}: ${errorMessage(err)}`);
      }
    }
    await load();
    if (last) select(last);
    if (problems.length) setError(problems.join(" · "));
    setUploading(false);
  };

  const names = docs?.map((d) => d.name) ?? [];
  const totalChunks = docs?.reduce((n, d) => n + d.chunks, 0) ?? 0;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Company knowledge</h1>
          <p className="mt-1 max-w-2xl text-sm text-stone-500 dark:text-stone-400">
            Policies, SOPs and guidelines. For every task, the agent retrieves the most relevant passages and follows them
            when it plans, and the safety review checks plans against them. Changes apply to the next workflow.
          </p>
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={load} className={btn} aria-label="Refresh">
            <RefreshCw className="size-4" aria-hidden="true" />
          </button>
          <input ref={fileRef} type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" multiple hidden onChange={upload} />
          <button type="button" className={btn} disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : <Upload className="size-4" aria-hidden="true" />} Upload
          </button>
          <button type="button" className={primaryBtn} onClick={() => setParams({ new: "1" })}>
            <Plus className="size-4" aria-hidden="true" /> New document
          </button>
        </div>
      </header>

      {error && <div className="mt-4"><ErrorNote>{error}</ErrorNote></div>}

      <div className="mt-6">
        <RetrievalTester onOpen={select} />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <Card className="h-fit p-2">
          <h2 className="px-3 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide text-stone-500">
            Documents{docs && ` · ${docs.length}`}
          </h2>
          {docs == null ? (
            !error && (
              <p className="flex items-center gap-2 px-3 py-2 text-sm text-stone-500">
                <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Loading…
              </p>
            )
          ) : docs.length === 0 ? (
            <p className="px-3 py-2 text-sm text-stone-500">No documents yet. Upload a .md or .txt file, or create one.</p>
          ) : (
            <ul className="space-y-0.5">
              {docs.map((d) => {
                const active = d.name === selected && !creating;
                return (
                  <li key={d.name}>
                    <button
                      type="button"
                      onClick={() => select(d.name)}
                      aria-current={active ? "page" : undefined}
                      className={`flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left ${
                        active ? "bg-blue-50 dark:bg-blue-950" : "hover:bg-stone-100 dark:hover:bg-stone-800"
                      }`}
                    >
                      <FileText className={`mt-0.5 size-4 shrink-0 ${active ? "text-blue-600 dark:text-blue-400" : "text-stone-400"}`} aria-hidden="true" />
                      <span className="min-w-0">
                        <span className={`block truncate text-sm font-medium ${active ? "text-blue-700 dark:text-blue-300" : ""}`}>{d.title}</span>
                        <span className="block truncate text-xs text-stone-500 dark:text-stone-400">
                          {d.name} · {d.chunks} passage{d.chunks === 1 ? "" : "s"}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {docs?.length > 0 && (
            <p className="border-t border-stone-200 px-3 pb-1 pt-2 text-xs text-stone-500 dark:border-stone-800">
              {totalChunks} passages indexed
            </p>
          )}
        </Card>

        {creating ? (
          <DocumentPanel
            key="new"
            name={null}
            existing={names}
            onSaved={(n) => load().then(() => select(n))}
            onCancelNew={() => select(docs?.[0]?.name)}
          />
        ) : selected ? (
          <DocumentPanel
            key={selected}
            name={selected}
            existing={names}
            onSaved={() => load()}
            onDeleted={() => load().then(() => select(null))}
          />
        ) : (
          docs?.length === 0 && (
            <Card className="flex flex-col items-center justify-center p-10 text-center">
              <BookOpen className="size-8 text-stone-400" aria-hidden="true" />
              <p className="mt-2 text-sm text-stone-500">Add your first policy or SOP to guide the agent.</p>
            </Card>
          )
        )}
      </div>
    </div>
  );
}
