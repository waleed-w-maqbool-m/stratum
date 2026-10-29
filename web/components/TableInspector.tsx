"use client";

import { useEffect, useState } from "react";

import {
  GAP_LABEL,
  ORIGIN_COLOR,
  ORIGIN_LABEL,
  cardinalityText,
  fetchTable,
  originOf,
  type AtlasEdge,
  type AtlasNode,
  type AtlasView,
  type TableDetail,
} from "@/lib/atlas";

/**
 * What you see after clicking a sphere: the table, its columns, where its joins go and why
 * Stratum believes each one, and what it thinks is missing.
 *
 * Every fact here is also reachable without the 3D map, which is the point of a tool for
 * studying a database: the map is for finding a table, this is for reading it.
 */

const TABS = ["Overview", "Columns", "Joins", "Missing"] as const;
type Tab = (typeof TABS)[number];

const CLASS_NAMES: Record<string, string> = {
  core: "Live",
  archive: "Archive",
  staging: "Staging",
  backup: "Backup",
  dead: "Leftover",
};

function JoinCard({
  edge,
  outgoing,
  onGo,
}: {
  edge: AtlasEdge;
  outgoing: boolean;
  onGo: (table: string) => void;
}) {
  const origin = originOf(edge);
  const other = outgoing ? edge.to : edge.from;
  const ev = edge.evidence;
  return (
    <li className="border-l-2 pl-3" style={{ borderColor: ORIGIN_COLOR[origin] }}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span aria-hidden>{outgoing ? "→" : "←"}</span>
        <button
          type="button"
          onClick={() => onGo(other)}
          className="font-mono text-[13px] underline-offset-2 hover:underline"
        >
          {other}
        </button>
        <span className="tabular text-[12px] text-[var(--fg-muted)]">
          {Math.round(edge.confidence * 100)}% sure
        </span>
      </div>
      <p className="mt-0.5 text-[12px]">
        <span style={{ color: ORIGIN_COLOR[origin] }}>●</span> {ORIGIN_LABEL[origin]}
        {edge.corroborated_by && edge.corroborated_by.length > 0 && (
          <span className="text-[var(--fg-muted)]">
            {" "}
            and confirmed by {edge.corroborated_by.map((o) => ORIGIN_LABEL[o].toLowerCase()).join(", ")}
          </span>
        )}
      </p>
      <p className="mt-0.5 break-all font-mono text-[12px] text-[var(--fg-muted)]">{edge.on}</p>
      {edge.reason && (
        <p className="mt-1 text-[12px] leading-relaxed text-[var(--fg-muted)]">{edge.reason}</p>
      )}
      {edge.cardinality && (
        <p className="mt-0.5 text-[12px] text-[var(--fg-muted)]">
          Cardinality: {cardinalityText(edge)}
        </p>
      )}
      {ev?.measured && (
        <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 text-[12px] text-[var(--fg-muted)]">
          <dt>Values matched</dt>
          <dd className="tabular">
            {ev.inclusion === null ? "" : `${(ev.inclusion * 100).toFixed(1)}%`} of {ev.values_tested}
          </dd>
          {!!ev.orphan_count && (
            <>
              <dt>No parent row</dt>
              <dd className="tabular">
                {ev.orphan_count}
                {ev.orphan_samples.length > 0 && ` (e.g. ${ev.orphan_samples.join(", ")})`}
              </dd>
            </>
          )}
          {ev.name_similarity !== null && (
            <>
              <dt>Name match</dt>
              <dd className="tabular">{ev.name_similarity.toFixed(2)}</dd>
            </>
          )}
        </dl>
      )}
      {edge.conditional && (
        <p className="mt-0.5 text-[12px] text-[var(--danger)]">
          Only when {edge.condition || "a condition holds"}
        </p>
      )}
    </li>
  );
}

export function TableInspector({
  databaseId,
  node,
  edges,
  view,
  onSelect,
}: {
  databaseId: string;
  node: AtlasNode;
  edges: AtlasEdge[];
  view: AtlasView;
  onSelect: (table: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("Overview");
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let live = true;
    setLoaded(false);
    setDetail(null);
    fetchTable(databaseId, node.id, view).then((d) => {
      if (live) {
        setDetail(d);
        setLoaded(true);
      }
    });
    return () => {
      live = false;
    };
  }, [databaseId, node.id, view]);

  const outgoing = edges.filter((e) => e.from === node.id);
  const incoming = edges.filter((e) => e.to === node.id && e.from !== node.id);
  const gapCount = detail?.gaps.length ?? 0;
  const renamed = detail?.columns.filter((c) => c.logical && c.logical.toLowerCase() !== c.physical.toLowerCase()).length ?? 0;

  return (
    <div>
      <h2 className="text-[15px] font-medium">{node.entity ?? node.id}</h2>
      <p className="font-mono text-[13px] text-[var(--fg-muted)]">{node.id}</p>

      <div role="tablist" aria-label="Table details" className="mt-3 flex gap-1 border-b rule">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            onClick={() => setTab(name)}
            className="px-2 pb-1.5 pt-1 text-[13px]"
            style={{
              borderBottom: tab === name ? "2px solid var(--accent)" : "2px solid transparent",
              color: tab === name ? "var(--fg)" : "var(--fg-muted)",
            }}
          >
            {name}
            {name === "Joins" && (
              <span className="tabular ml-1 text-[11px]">{outgoing.length + incoming.length}</span>
            )}
            {name === "Missing" && gapCount > 0 && (
              <span className="tabular ml-1 text-[11px]">{gapCount}</span>
            )}
          </button>
        ))}
      </div>

      <div role="tabpanel" className="mt-3 max-h-[48vh] overflow-y-auto pr-1 text-[13px]">
        {tab === "Overview" && (
          <div className="space-y-3">
            <dl className="space-y-1 text-[14px]">
              {[
                ["Kind", view === "submitted" ? "Not judged yet" : (CLASS_NAMES[node.class] ?? node.class)],
                ["Rows", node.rows.toLocaleString()],
                ["Columns", String(node.columns || detail?.columns.length || "none mapped")],
                ...(view === "refined"
                  ? [
                      ["Rules applied to every query", String(node.filters)],
                      ["Columns still to review", String(node.unreviewed)],
                      ["Columns renamed", detail ? String(renamed) : "…"],
                    ]
                  : []),
              ].map(([label, value]) => (
                <div key={label} className="flex justify-between border-b rule py-0.5">
                  <dt className="text-[var(--fg-muted)]">{label}</dt>
                  <dd className="tabular">{value}</dd>
                </div>
              ))}
            </dl>
            {detail?.reason && (
              <p className="leading-relaxed text-[var(--fg-muted)]">
                <span className="text-[var(--fg)]">Why {CLASS_NAMES[detail.kind]?.toLowerCase() ?? detail.kind}:</span>{" "}
                {detail.reason}
              </p>
            )}
            {detail && detail.primary_key.length > 0 && (
              <p className="text-[var(--fg-muted)]">
                Key: <span className="font-mono">{detail.primary_key.join(", ")}</span>
              </p>
            )}
            {detail?.description && <p className="leading-relaxed">{detail.description}</p>}
            {detail && detail.filters.length > 0 && (
              <div>
                <h3 className="font-medium">Rules applied to every query</h3>
                <ul className="mt-1 space-y-1.5">
                  {detail.filters.map((f) => (
                    <li key={f.expr} className="border-l-2 pl-2" style={{ borderColor: "var(--accent-quiet)" }}>
                      <span className="font-mono text-[12px]">{f.expr}</span>
                      <p className="text-[12px] text-[var(--fg-muted)]">
                        {f.reason}
                        {f.rows_excluded > 0 && ` Hides ${f.rows_excluded.toLocaleString()} rows.`}
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {loaded && !detail && (
              <p className="text-[var(--fg-muted)]">
                Column details are not available for this recording. Run the engine to see them.
              </p>
            )}
          </div>
        )}

        {tab === "Columns" && (
          <div>
            {!loaded && <p className="text-[var(--fg-muted)]">Loading…</p>}
            {loaded && !detail && (
              <p className="text-[var(--fg-muted)]">Column details are not available here.</p>
            )}
            {detail && (
              <table className="w-full text-left text-[12px]">
                <thead className="sticky top-0 bg-[var(--bg)] text-[var(--fg-muted)]">
                  <tr className="border-b rule">
                    <th scope="col" className="py-1 pr-2 font-normal">As stored</th>
                    {view === "refined" && <th scope="col" className="py-1 pr-2 font-normal">Means</th>}
                    <th scope="col" className="py-1 font-normal">Type</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.columns.map((c) => (
                    <tr key={c.physical + c.logical} className="border-b rule align-top last:border-0">
                      <td className="py-1 pr-2 font-mono">
                        {c.physical || <span className="text-[var(--fg-muted)]">derived</span>}
                        {c.is_primary_key && <span className="ml-1 text-[var(--accent)]" title="Primary key">key</span>}
                        {c.references && (
                          <div className="text-[11px] text-[var(--fg-muted)]">
                            → {c.references}
                          </div>
                        )}
                      </td>
                      {view === "refined" && (
                        <td className="py-1 pr-2">
                          {c.logical && c.logical.toLowerCase() !== c.physical.toLowerCase() ? (
                            <span className="font-mono">{c.logical}</span>
                          ) : (
                            <span className="text-[var(--fg-muted)]">same</span>
                          )}
                          {c.derived && (
                            <div className="break-all font-mono text-[11px] text-[var(--fg-muted)]">
                              {c.expr}
                            </div>
                          )}
                          {Object.keys(c.decoded).length > 0 && (
                            <div className="text-[11px] text-[var(--fg-muted)]">
                              {Object.entries(c.decoded)
                                .slice(0, 6)
                                .map(([k, v]) => `${k} = ${v}`)
                                .join(", ")}
                            </div>
                          )}
                          {c.status === "hypothesis" && (
                            <div className="text-[11px] text-[var(--fg-muted)]">to review</div>
                          )}
                        </td>
                      )}
                      <td className="py-1 font-mono text-[11px] text-[var(--fg-muted)]">{c.type}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {tab === "Joins" && (
          <div className="space-y-4">
            {outgoing.length + incoming.length === 0 && (
              <p className="text-[var(--fg-muted)]">
                {view === "submitted"
                  ? "This table declares no foreign keys and none point at it."
                  : "No relationship was found to or from this table."}
              </p>
            )}
            {outgoing.length > 0 && (
              <div>
                <h3 className="font-medium">Points at</h3>
                <ul className="mt-2 space-y-3">
                  {outgoing.map((e, i) => (
                    <JoinCard key={i} edge={e} outgoing onGo={onSelect} />
                  ))}
                </ul>
              </div>
            )}
            {incoming.length > 0 && (
              <div>
                <h3 className="font-medium">Pointed at by</h3>
                <ul className="mt-2 space-y-3">
                  {incoming.map((e, i) => (
                    <JoinCard key={i} edge={e} outgoing={false} onGo={onSelect} />
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {tab === "Missing" && (
          <div className="space-y-4">
            {view === "submitted" && (
              <p className="text-[var(--fg-muted)]">
                Switch to Refined to see what Stratum thinks this table is missing.
              </p>
            )}
            {view === "refined" && loaded && gapCount === 0 && (
              <p className="text-[var(--fg-muted)]">Nothing is suggested for this table.</p>
            )}
            {detail?.gaps.map((g) => (
              <div key={g.id}>
                <h3 className="font-medium">{GAP_LABEL[g.gap_type].title}</h3>
                <p className="mt-0.5 leading-relaxed">{g.reason}</p>
                {g.blockers.length > 0 && (
                  <ul className="mt-1 list-disc pl-5 text-[var(--fg-muted)]">
                    {g.blockers.map((b) => (
                      <li key={b}>{b}</li>
                    ))}
                  </ul>
                )}
                {g.fix && (
                  <pre className="mt-2 overflow-x-auto border rule bg-[var(--surface)] p-2 text-[11px] leading-relaxed">
                    <code className="font-mono">{g.fix}</code>
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
