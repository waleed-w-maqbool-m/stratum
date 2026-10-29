"use client";

import dynamic from "next/dynamic";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { GapsSection } from "@/components/GapsSection";
import { PageTitle } from "@/components/PageTitle";
import { TableInspector } from "@/components/TableInspector";
import {
  ORIGIN_COLOR,
  ORIGIN_LABEL,
  fetchAtlas,
  fetchGaps,
  prefersReducedMotion,
  type AtlasData,
  type AtlasView,
  type GapsResponse,
  type Origin,
} from "@/lib/atlas";

/**
 * The Atlas (PLAN.md 5.3).
 *
 * three.js is loaded only on this route (5.4), so the rest of the app pays nothing for
 * it. Under `prefers-reduced-motion` the 3D scene is not rendered at all — a paused
 * orbiting galaxy is still a lot of moving parts — and a table is shown instead, which
 * carries the same information in a form that is easier to read anyway.
 *
 * Two views of the same database: as submitted (only what its own catalog declares) and
 * refined (what excavation made of it). The difference between them is what Stratum adds,
 * so the page says so in numbers rather than leaving it to be noticed.
 */

const AtlasScene = dynamic(
  () => import("@/components/AtlasScene").then((m) => m.AtlasScene),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center text-sm text-[var(--fg-muted)]">
        Loading the map…
      </div>
    ),
  },
);

const CLASS_ORDER = ["core", "archive", "staging", "backup", "dead"] as const;

const CLASS_NAMES: Record<string, string> = {
  core: "Live",
  archive: "Archive",
  staging: "Staging",
  backup: "Backup",
  dead: "Leftover",
};

/** Swatch fills for the legend. These are decorative blocks, not text, so the 3:1
 * non-text threshold applies and the dark-theme hues clear it on either background. */
const CLASS_SWATCH: Record<string, string> = {
  core: "#ffb23f",
  archive: "#8a6021",
  staging: "#6b7280",
  backup: "#4b5563",
  dead: "#57606a",
};

/** Text colours for the same classes. A swatch may be any hue; the *label* has to be
 * readable on whichever background the viewer is using, so it goes through the theme
 * tokens instead of reusing the fill. */
const CLASS_TEXT: Record<string, string> = {
  core: "var(--accent)",
  archive: "var(--accent-quiet)",
  staging: "var(--fg-muted)",
  backup: "var(--fg-muted)",
  dead: "var(--fg-muted)",
};

const ORIGIN_ORDER: Origin[] = ["declared", "shipped_sql", "data", "llm", "conditional"];

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

export default function AtlasPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [datas, setDatas] = useState<Partial<Record<AtlasView, AtlasData>>>({});
  const [view, setView] = useState<AtlasView>("refined");
  const [error, setError] = useState("");
  const [viewError, setViewError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [reduced, setReduced] = useState(false);
  const [show3D, setShow3D] = useState(true);
  const [gaps, setGaps] = useState<GapsResponse | null>(null);
  const [showGhosts, setShowGhosts] = useState(false);

  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const fullButton = useRef<HTMLButtonElement>(null);
  const [native, setNative] = useState(false);
  const [overlay, setOverlay] = useState(false);
  const isFull = native || overlay;

  const data = datas[view] ?? null;

  useEffect(() => {
    const isReduced = prefersReducedMotion();
    setReduced(isReduced);
    setShow3D(!isReduced);
    fetchAtlas(id, "refined")
      .then((refined) => setDatas((d) => ({ ...d, refined })))
      .catch((exception) => setError(String(exception)));
    fetchGaps(id).then(setGaps);
  }, [id]);

  const chooseView = useCallback(
    (next: AtlasView) => {
      setViewError("");
      if (datas[next]) {
        setView(next);
        return;
      }
      fetchAtlas(id, next)
        .then((loaded) => {
          setDatas((d) => ({ ...d, [next]: loaded }));
          setView(next);
        })
        .catch(() =>
          setViewError(
            next === "submitted"
              ? "The database as submitted was not recorded for this model. Excavate it again to keep a copy."
              : "That view could not be loaded.",
          ),
        );
    },
    [datas, id],
  );

  // A table that exists in one view may not in the other.
  useEffect(() => {
    if (selected && data && !data.nodes.some((node) => node.id === selected)) setSelected(null);
  }, [data, selected]);

  // Full screen: the real API where there is one, a fixed overlay where there is not
  // (iPhone Safari has no element fullscreen). The container goes full screen, not the
  // canvas, so the inspector comes along.
  const toggleFull = useCallback(() => {
    const element = frame.current;
    if (!element) return;
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else if (overlay) {
      setOverlay(false);
    } else if (element.requestFullscreen) {
      element.requestFullscreen().catch(() => setOverlay(true));
    } else {
      setOverlay(true);
    }
  }, [overlay]);

  useEffect(() => {
    const onChange = () => {
      const active = Boolean(document.fullscreenElement);
      setNative((was) => {
        if (was && !active) fullButton.current?.focus();
        return active;
      });
    };
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
      if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "/") {
        event.preventDefault();
        search.current?.focus();
      } else if (event.key === "f" || event.key === "F") {
        event.preventDefault();
        toggleFull();
      } else if (event.key === "Escape" && overlay) {
        setOverlay(false);
        fullButton.current?.focus();
      }
    };
    document.addEventListener("fullscreenchange", onChange);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      document.removeEventListener("keydown", onKey);
    };
  }, [toggleFull, overlay]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle || !data) return [];
    return data.nodes
      .filter((n) => n.id.toLowerCase().includes(needle) || n.entity?.toLowerCase().includes(needle))
      .sort((a, b) => b.rows - a.rows)
      .slice(0, 8);
  }, [query, data]);

  const selectedNode = useMemo(
    () => data?.nodes.find((node) => node.id === selected) ?? null,
    [data, selected],
  );

  const selectedEdges = useMemo(
    () => data?.edges.filter((edge) => edge.from === selected || edge.to === selected) ?? [],
    [data, selected],
  );

  const originCounts = data?.summary.relationships_by_origin ?? {};
  const refined = datas.refined;
  const declaredCount = refined?.summary.relationships_by_origin?.declared ?? 0;
  const foundCount = refined ? refined.edges.length - declaredCount : 0;

  return (
    <main className="mx-auto max-w-[76rem] px-5 py-10 sm:px-8">
      <PageTitle
        title="Map of the schema"
        lede={
          data
            ? view === "submitted"
              ? `${plural(data.summary.core, "table")} exactly as the database describes itself: nothing judged, nothing renamed, and only the foreign keys it declares. ${
                  data.edges.length === 0
                    ? "It declares none, so every table stands alone."
                    : `${plural(data.edges.length, "relationship")} declared.`
                }`
              : `${data.summary.core} live tables stand above the grid; ${data.summary.excluded} archived, staged, backed-up or leftover tables sink below it, and no answer draws from them. Height is row count, and a brighter line is a join Stratum trusts more.`
            : "Every table, where excavation placed it, and the joins between them."
        }
      />

      {error && (
        <p className="border-l-2 px-3 py-2 text-sm" style={{ borderColor: "var(--danger)" }}>
          The map could not be loaded. If {id} has not been excavated yet, excavate it first.
        </p>
      )}

      {data && (
        <div
          ref={frame}
          className={isFull ? "fixed inset-0 z-50 overflow-auto bg-[var(--bg)] p-4" : ""}
        >
          <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px]">
            <div role="group" aria-label="Which view of the database" className="flex border rule">
              {(["submitted", "refined"] as const).map((name) => (
                <button
                  key={name}
                  type="button"
                  aria-pressed={view === name}
                  onClick={() => chooseView(name)}
                  className="px-3 py-1"
                  style={{
                    background: view === name ? "var(--surface)" : "transparent",
                    color: view === name ? "var(--fg)" : "var(--fg-muted)",
                    borderRight: name === "submitted" ? "1px solid var(--rule, currentColor)" : undefined,
                  }}
                >
                  {name === "submitted" ? "As submitted" : "Refined"}
                </button>
              ))}
            </div>
            {view === "refined" &&
              CLASS_ORDER.filter((klass) => data.summary.by_class[klass]).map((klass) => (
                <span key={klass} className="flex items-center gap-1.5">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ background: CLASS_SWATCH[klass] }}
                  />
                  <span className="text-[var(--fg-muted)]">
                    {CLASS_NAMES[klass]} {data.summary.by_class[klass]}
                  </span>
                </span>
              ))}
            {ORIGIN_ORDER.filter((o) => originCounts[o]).map((o) => (
              <span key={o} className="flex items-center gap-1.5">
                <span
                  aria-hidden
                  className="inline-block h-0.5 w-4"
                  style={{ background: ORIGIN_COLOR[o] }}
                />
                <span className="text-[var(--fg-muted)]">
                  {ORIGIN_LABEL[o]} {originCounts[o]}
                </span>
              </span>
            ))}
            {view === "refined" && (data.ghost_edges?.length ?? 0) > 0 && show3D && (
              <label className="flex cursor-pointer items-center gap-1.5 text-[var(--fg-muted)]">
                <input
                  type="checkbox"
                  checked={showGhosts}
                  onChange={(event) => setShowGhosts(event.target.checked)}
                />
                Show {data.ghost_edges?.length} missing links (dashed)
              </label>
            )}
            <span className="relative">
              <label htmlFor="table-search" className="sr-only">
                Find a table
              </label>
              <input
                id="table-search"
                ref={search}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && matches[0]) {
                    setSelected(matches[0].id);
                    setQuery("");
                  } else if (event.key === "Escape") {
                    setQuery("");
                    event.currentTarget.blur();
                  }
                }}
                placeholder="Find a table  /"
                autoComplete="off"
                className="w-44 border rule bg-transparent px-2 py-1 text-[13px] outline-none focus:border-[var(--accent)]"
              />
              {matches.length > 0 && (
                <ul className="absolute left-0 top-full z-10 mt-1 w-64 border rule bg-[var(--bg)]">
                  {matches.map((n) => (
                    <li key={n.id}>
                      <button
                        type="button"
                        onClick={() => {
                          setSelected(n.id);
                          setQuery("");
                        }}
                        className="flex w-full justify-between gap-3 px-2 py-1 text-left hover:bg-[var(--surface)]"
                      >
                        <span className="truncate font-mono text-[12px]">{n.id}</span>
                        {n.entity && <span className="truncate text-[12px] text-[var(--fg-muted)]">{n.entity}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </span>
            <span className="ml-auto flex items-center gap-2">
              <button type="button" onClick={() => setShow3D((v) => !v)} className="border rule px-3 py-1">
                {show3D ? "Show as a table" : "Show the map"}
              </button>
              <button
                ref={fullButton}
                type="button"
                onClick={toggleFull}
                aria-pressed={isFull}
                title="Full screen (F)"
                className="border rule px-3 py-1"
              >
                {isFull ? "Exit full screen" : "Full screen"}
              </button>
            </span>
          </div>

          {viewError && (
            <p role="alert" className="mb-3 border-l-2 px-3 py-1 text-[13px]" style={{ borderColor: "var(--danger)" }}>
              {viewError}
            </p>
          )}

          {view === "refined" && refined && (
            <p className="mb-3 max-w-[60rem] text-[13px] leading-relaxed text-[var(--fg-muted)]" aria-live="polite">
              Compared with the database as submitted, Stratum found{" "}
              <strong className="font-medium text-[var(--fg)]">
                {plural(foundCount, "relationship")}
              </strong>{" "}
              the schema does not declare
              {declaredCount > 0 ? ` (and kept the ${declaredCount} it does)` : ""}, set aside{" "}
              <strong className="font-medium text-[var(--fg)]">
                {plural(refined.summary.excluded, "table")}
              </strong>
              {refined.summary.columns_renamed !== undefined && (
                <>
                  , gave plain names to{" "}
                  <strong className="font-medium text-[var(--fg)]">
                    {plural(refined.summary.columns_renamed, "column")}
                  </strong>
                  , decoded{" "}
                  <strong className="font-medium text-[var(--fg)]">
                    {plural(refined.summary.columns_decoded ?? 0, "coded column")}
                  </strong>
                  , found{" "}
                  <strong className="font-medium text-[var(--fg)]">
                    {plural(refined.summary.soft_delete_rules ?? 0, "soft-delete rule")}
                  </strong>
                </>
              )}
              {gaps && gaps.total > 0 && (
                <>
                  {" "}
                  and has{" "}
                  <strong className="font-medium text-[var(--fg)]">
                    {plural(gaps.total, "suggestion")}
                  </strong>{" "}
                  for what may be missing
                </>
              )}
              .
            </p>
          )}

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
            <section>
              {show3D ? (
                <div className={`${isFull ? "h-[calc(100vh-9rem)]" : "h-[62vh]"} border rule`}>
                  <AtlasScene
                    key={view}
                    data={data}
                    selected={selected}
                    onSelect={setSelected}
                    reducedMotion={reduced}
                    showGhosts={showGhosts && view === "refined"}
                  />
                </div>
              ) : (
                <div className={`${isFull ? "max-h-[calc(100vh-9rem)]" : "max-h-[62vh]"} overflow-y-auto border rule`}>
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-[var(--bg)]">
                      <tr className="border-b rule text-left">
                        {["Table", "Entity", "Kind", "Rows", "Columns"].map((head, index) => (
                          <th
                            key={head}
                            scope="col"
                            className={`px-3 py-2 text-[13px] font-normal text-[var(--fg-muted)] ${index > 2 ? "text-right" : ""}`}
                          >
                            {head}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {[...data.nodes]
                        .sort(
                          (a, b) =>
                            CLASS_ORDER.indexOf(a.class) - CLASS_ORDER.indexOf(b.class) ||
                            b.rows - a.rows,
                        )
                        .map((node) => (
                          <tr
                            key={node.id}
                            onClick={() => setSelected(node.id === selected ? null : node.id)}
                            onKeyDown={(event) => {
                              if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                setSelected(node.id === selected ? null : node.id);
                              }
                            }}
                            tabIndex={0}
                            aria-selected={node.id === selected}
                            className="cursor-pointer border-b rule last:border-0"
                            style={{
                              background: node.id === selected ? "var(--surface)" : undefined,
                              opacity: node.class === "core" ? 1 : 0.55,
                            }}
                          >
                            <td className="px-3 py-1.5 font-mono text-[13px]">{node.id}</td>
                            <td className="px-3 py-1.5 text-[13px]">
                              {node.entity ?? <span className="sr-only">none</span>}
                            </td>
                            <td className="px-3 py-1">
                              <span className="text-[13px]" style={{ color: CLASS_TEXT[node.class] }}>
                                {view === "submitted" ? "" : (CLASS_NAMES[node.class] ?? node.class)}
                              </span>
                            </td>
                            <td className="tabular px-3 py-1.5 text-right text-[13px]">
                              {node.rows.toLocaleString()}
                            </td>
                            <td className="tabular px-3 py-1.5 text-right text-[13px]">
                              {node.columns || ""}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}

              {reduced && (
                <p className="mt-2 text-[13px] text-[var(--fg-muted)]">
                  Your system asks for reduced motion, so the table is shown first and the map does
                  not drift.
                </p>
              )}
            </section>

            <aside className={isFull ? "max-h-[calc(100vh-9rem)] overflow-y-auto" : ""}>
              {selectedNode ? (
                <TableInspector
                  databaseId={id}
                  node={selectedNode}
                  edges={selectedEdges}
                  view={view}
                  onSelect={setSelected}
                />
              ) : (
                <>
                  <h2 className="text-[15px] font-medium">Pick a table</h2>
                  <p className="mt-3 text-[14px] leading-relaxed text-[var(--fg-muted)]">
                    Click a sphere or a row to read that table: its columns, what each join points
                    at and why Stratum believes it, and what it thinks is missing. Drag to turn the
                    map, scroll to zoom, press F for full screen.
                  </p>
                </>
              )}
            </aside>
          </div>
        </div>
      )}

      {data && view === "refined" && gaps && gaps.total > 0 && (
        <GapsSection
          gaps={gaps}
          onSelect={(table) => {
            setSelected(table);
            window.scrollTo({ top: 0, behavior: reduced ? "auto" : "smooth" });
          }}
        />
      )}
    </main>
  );
}
