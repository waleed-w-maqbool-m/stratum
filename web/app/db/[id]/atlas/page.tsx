"use client";

import dynamic from "next/dynamic";
import { use, useEffect, useMemo, useState } from "react";

import { PageTitle } from "@/components/PageTitle";
import { fetchAtlas, prefersReducedMotion, type AtlasData } from "@/lib/atlas";

/**
 * The Atlas (PLAN.md 5.3).
 *
 * three.js is loaded only on this route (5.4), so the rest of the app pays nothing for
 * it. Under `prefers-reduced-motion` the 3D scene is not rendered at all — a paused
 * orbiting galaxy is still a lot of moving parts — and a table is shown instead, which
 * carries the same information in a form that is easier to read anyway.
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

export default function AtlasPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [data, setData] = useState<AtlasData | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [reduced, setReduced] = useState(false);
  const [show3D, setShow3D] = useState(true);

  useEffect(() => {
    const isReduced = prefersReducedMotion();
    setReduced(isReduced);
    setShow3D(!isReduced);
    fetchAtlas(id).then(setData).catch((exception) => setError(String(exception)));
  }, [id]);

  const selectedNode = useMemo(
    () => data?.nodes.find((node) => node.id === selected) ?? null,
    [data, selected],
  );

  const selectedEdges = useMemo(
    () =>
      data?.edges.filter(
        (edge) => edge.from === selected || edge.to === selected,
      ) ?? [],
    [data, selected],
  );

  return (
    <main className="mx-auto max-w-[76rem] px-5 py-10 sm:px-8">
      <PageTitle
        title="Map of the schema"
        lede={
          data
            ? `${data.summary.core} live tables stand above the grid; ${data.summary.excluded} archived, staged, backed-up or leftover tables sink below it, and no answer draws from them. Height is row count, and a brighter line is a join Stratum trusts more.`
            : "Every table, where excavation placed it, and the joins between them."
        }
      />

      {error && (
        <p
          className="border-l-2 px-3 py-2 text-sm"
          style={{ borderColor: "var(--danger)" }}
        >
          The map could not be loaded. If {id} has not been excavated yet, excavate it first.
        </p>
      )}

      {data && (
        <div className="grid gap-6 lg:grid-cols-[1fr_300px]">
          <section>
            <div className="mb-2 flex flex-wrap items-center gap-4 text-[13px]">
              {CLASS_ORDER.filter((klass) => data.summary.by_class[klass]).map(
                (klass) => (
                  <span key={klass} className="flex items-center gap-1.5">
                    <span
                      className="inline-block h-2 w-2 rounded-full"
                      style={{ background: CLASS_SWATCH[klass] }}
                    />
                    <span className="text-[var(--fg-muted)]">
                      {CLASS_NAMES[klass]} {data.summary.by_class[klass]}
                    </span>
                  </span>
                ),
              )}
              <button
                onClick={() => setShow3D((v) => !v)}
                className="ml-auto border rule px-3 py-1"
              >
                {show3D ? "Show as a table" : "Show the map"}
              </button>
            </div>

            {show3D ? (
              <div className="h-[62vh] border rule">
                <AtlasScene
                  data={data}
                  selected={selected}
                  onSelect={setSelected}
                  reducedMotion={reduced}
                />
              </div>
            ) : (
              <div className="max-h-[62vh] overflow-y-auto border rule">
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
                          onClick={() =>
                            setSelected(node.id === selected ? null : node.id)
                          }
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
                            background:
                              node.id === selected
                                ? "var(--surface)"
                                : undefined,
                            opacity: node.class === "core" ? 1 : 0.55,
                          }}
                        >
                          <td className="px-3 py-1.5 font-mono text-[13px]">{node.id}</td>
                          <td className="px-3 py-1.5 text-[13px]">
                            {node.entity ?? (
                              <span className="sr-only">none</span>
                            )}
                          </td>
                          <td className="px-3 py-1">
                            <span
                              className="text-[13px]"
                              style={{ color: CLASS_TEXT[node.class] }}
                            >
                              {CLASS_NAMES[node.class] ?? node.class}
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
                Your system asks for reduced motion, so the table is shown first and the
                map does not drift.
              </p>
            )}
          </section>

          <aside>
            <h2 className="text-[15px] font-medium">
              {selectedNode ? selectedNode.entity ?? selectedNode.id : "Pick a table"}
            </h2>
            {selectedNode ? (
              <div className="mt-3">
                <p className="font-mono text-[13px] text-[var(--fg-muted)]">{selectedNode.id}</p>
                <dl className="mt-3 space-y-1 text-[14px]">
                  {[
                    ["Kind", CLASS_NAMES[selectedNode.class] ?? selectedNode.class],
                    ["Rows", selectedNode.rows.toLocaleString()],
                    ["Columns", String(selectedNode.columns || "none mapped")],
                    ["Rules applied to every query", String(selectedNode.filters)],
                    ["Columns still to review", String(selectedNode.unreviewed)],
                  ].map(([label, value]) => (
                    <div
                      key={label}
                      className="flex justify-between border-b rule py-0.5"
                    >
                      <dt className="text-[var(--fg-muted)]">{label}</dt>
                      <dd className="tabular">{value}</dd>
                    </div>
                  ))}
                </dl>

                {selectedEdges.length > 0 && (
                  <>
                    <h3 className="mt-6 text-[14px] font-medium">Joins</h3>
                    <ul className="mt-2 space-y-3 text-[13px]">
                      {selectedEdges.map((edge, index) => (
                        <li
                          key={index}
                          className="border-l-2 pl-2"
                          style={{
                            borderColor: edge.inferred
                              ? "var(--good)"
                              : "var(--accent)",
                          }}
                        >
                          <span className="font-mono">
                            {edge.from === selectedNode.id ? "→ " : "← "}
                            {edge.from === selectedNode.id ? edge.to : edge.from}
                          </span>
                          <span className="tabular ml-2 text-[var(--fg-muted)]">
                            {Math.round(edge.confidence * 100)}% sure
                          </span>
                          <p className="mt-0.5 break-all font-mono text-[12px] text-[var(--fg-muted)]">
                            {edge.on}
                          </p>
                          {!edge.inferred && (
                            <p className="text-[12px] text-[var(--accent)]">
                              Read from the database&apos;s own views and procedures
                            </p>
                          )}
                          {edge.conditional && (
                            <p className="text-[12px] text-[var(--danger)]">
                              Only under a condition: the column points at different tables per row
                            </p>
                          )}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            ) : (
              <p className="mt-3 text-[14px] leading-relaxed text-[var(--fg-muted)]">
                Click a sphere or a row to see its joins. Drag to turn the map, scroll to
                zoom. Amber lines were read from the database&apos;s own views and procedures;
                green ones were inferred from the data.
              </p>
            )}
          </aside>
        </div>
      )}
    </main>
  );
}
