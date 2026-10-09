"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";

import { PageTitle } from "@/components/PageTitle";
import { prefersReducedMotion } from "@/lib/atlas";
import { EXAMPLE_SCHEMA, readSchema, type Kind } from "@/lib/schema";

/**
 * Your database, as a map.
 *
 * Paste a schema and see it drawn the way Stratum draws a database it has excavated:
 * live tables above the grid, the ones that look like leftovers sunk below it. It runs
 * entirely in the browser, which is why it can live on a static site, and why it is a
 * smaller thing than excavation: a schema file shows names, not data. The page says what
 * that means rather than letting the map imply more than it knows.
 */

const AtlasScene = dynamic(() => import("@/components/AtlasScene").then((m) => m.AtlasScene), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-[var(--fg-muted)]">
      Loading the map…
    </div>
  ),
});

const KIND_NAMES: Record<Kind, string> = {
  core: "Live",
  archive: "Archive",
  staging: "Staging",
  backup: "Backup",
  dead: "Leftover",
};
const KIND_SWATCH: Record<Kind, string> = {
  core: "#ffb23f",
  archive: "#8a6021",
  staging: "#6b7280",
  backup: "#4b5563",
  dead: "#57606a",
};
const KIND_ORDER: Kind[] = ["core", "archive", "staging", "backup", "dead"];
const MAX_FILE = 5 * 1024 * 1024;

export default function SchemaPage() {
  const [source, setSource] = useState(EXAMPLE_SCHEMA);
  const [applied, setApplied] = useState(EXAMPLE_SCHEMA);
  const [selected, setSelected] = useState<string | null>(null);
  const [reduced, setReduced] = useState(false);
  const [fileError, setFileError] = useState("");

  useEffect(() => setReduced(prefersReducedMotion()), []);

  // Reading a large schema is quick but not free; wait for a pause in typing.
  useEffect(() => {
    const timer = setTimeout(() => setApplied(source), 350);
    return () => clearTimeout(timer);
  }, [source]);

  const map = useMemo(() => readSchema(applied), [applied]);
  const detail = map && selected ? (map.details.get(selected) ?? null) : null;

  const load = (file: File | undefined) => {
    setFileError("");
    if (!file) return;
    if (file.size > MAX_FILE) {
      setFileError("That file is over 5 MB. Paste just the CREATE TABLE statements.");
      return;
    }
    file.text().then((text) => {
      setSelected(null);
      setSource(text);
    });
  };

  return (
    <main className="mx-auto max-w-[76rem] px-5 pb-28 pt-10 sm:px-8">
      <PageTitle
        title="See your database as a map"
        lede="Paste your CREATE TABLE statements and Stratum draws every table, with the ones that look like backups, archives and scratch copies sunk below the rest. It runs in your browser; nothing is uploaded."
      />

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <section aria-labelledby="schema-input" className="order-last min-w-0 lg:order-none">
          <h2 id="schema-input" className="text-[15px] font-medium">Your schema</h2>
          <label htmlFor="schema" className="sr-only">
            CREATE TABLE statements
          </label>
          <textarea
            id="schema"
            value={source}
            onChange={(event) => {
              setSelected(null);
              setSource(event.target.value);
            }}
            spellCheck={false}
            rows={16}
            className="mt-2 w-full border rule bg-[var(--surface)] p-3 font-mono text-[12px] leading-relaxed outline-none focus:border-[var(--accent)]"
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <label className="cursor-pointer border rule px-3 py-1.5 text-[13px] hover:border-[var(--accent)]">
              Upload a .sql file
              <input
                type="file"
                accept=".sql,.ddl,.txt,text/plain"
                className="sr-only"
                onChange={(event) => load(event.target.files?.[0])}
              />
            </label>
            <button
              type="button"
              onClick={() => {
                setSelected(null);
                setSource(EXAMPLE_SCHEMA);
              }}
              className="border rule px-3 py-1.5 text-[13px] hover:border-[var(--accent)]"
            >
              Use the example
            </button>
            <button
              type="button"
              onClick={() => {
                setSelected(null);
                setSource("");
              }}
              className="border rule px-3 py-1.5 text-[13px] hover:border-[var(--accent)]"
            >
              Clear
            </button>
          </div>
          {fileError && (
            <p role="alert" className="mt-3 text-[13px] text-[var(--danger)]">{fileError}</p>
          )}
          <p className="mt-4 text-[13px] leading-relaxed text-[var(--fg-muted)]">
            Works with the output of <code className="font-mono">pg_dump --schema-only</code>,{" "}
            <code className="font-mono">mysqldump --no-data</code>, or SQL Server&rsquo;s
            Generate Scripts. The example is an invented legacy order system, not one of the
            benchmark databases.
          </p>
        </section>

        <section aria-labelledby="schema-map" className="min-w-0">
          <h2 id="schema-map" className="sr-only">The map</h2>
          {map ? (
            <>
              <p aria-live="polite" className="text-[14px] leading-relaxed">
                {map.atlas.nodes.length} tables read: {map.atlas.summary.core} look live,{" "}
                {map.atlas.summary.excluded} look like leftovers. {map.declared} declared{" "}
                {map.declared === 1 ? "join" : "joins"}, {map.guessed} guessed from column names.
                {map.omitted > 0 && (
                  <span className="text-[var(--fg-muted)]">
                    {" "}The first {map.atlas.nodes.length} are drawn; {map.omitted} more were left off.
                  </span>
                )}
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-4 text-[13px]">
                {KIND_ORDER.filter((kind) => map.atlas.summary.by_class[kind]).map((kind) => (
                  <span key={kind} className="flex items-center gap-1.5">
                    <span
                      aria-hidden
                      className="inline-block h-2 w-2 rounded-full"
                      style={{ background: KIND_SWATCH[kind] }}
                    />
                    <span className="text-[var(--fg-muted)]">
                      {KIND_NAMES[kind]} {map.atlas.summary.by_class[kind]}
                    </span>
                  </span>
                ))}
                <span className="ml-auto text-[var(--fg-muted)]">
                  Amber lines are declared keys, green ones are guesses.
                </span>
              </div>
              <div className="mt-3 h-[58vh] min-h-[22rem] border rule">
                <AtlasScene
                  data={map.atlas}
                  selected={selected}
                  onSelect={setSelected}
                  reducedMotion={reduced}
                />
              </div>

              <div aria-live="polite" className="mt-4 min-h-[6rem]">
                {detail ? (
                  <div>
                    <h3 className="font-mono text-[15px]">{detail.name}</h3>
                    <p className="mt-1 text-[14px] text-[var(--fg-muted)]">
                      {KIND_NAMES[detail.kind]}: {detail.reason}.
                    </p>
                    <p className="mt-2 text-[14px]">
                      {detail.columns.length} columns
                      {detail.primaryKey.length > 0 && <>, key {detail.primaryKey.join(", ")}</>}
                    </p>
                    <p className="mt-1 break-words font-mono text-[12px] leading-relaxed text-[var(--fg-muted)]">
                      {detail.columns.slice(0, 30).join("  ")}
                      {detail.columns.length > 30 && "  …"}
                    </p>
                    {detail.joins.length > 0 && (
                      <ul className="mt-3 space-y-1 text-[13px]">
                        {detail.joins.map((join) => (
                          <li key={`${join.direction}-${join.other}`} className="border-l-2 pl-3" style={{ borderColor: join.declared ? "var(--accent)" : "var(--good)" }}>
                            <span className="font-mono">{join.on}</span>
                            <span className="ml-2 text-[var(--fg-muted)]">
                              {join.declared ? "declared" : "guessed from names"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ) : (
                  <p className="text-[14px] leading-relaxed text-[var(--fg-muted)]">
                    Click a sphere to see that table. Drag to turn the map, scroll to zoom.
                  </p>
                )}
              </div>

              <details className="mt-4 border-t rule pt-3">
                <summary className="cursor-pointer text-[14px]">Show the tables as a list</summary>
                <div className="mt-3 max-h-[22rem] overflow-y-auto border rule">
                  <table className="w-full text-left text-[13px]">
                    <thead className="sticky top-0 bg-[var(--bg)]">
                      <tr className="border-b rule text-[var(--fg-muted)]">
                        <th scope="col" className="px-3 py-2 font-normal">Table</th>
                        <th scope="col" className="px-3 py-2 font-normal">Looks like</th>
                        <th scope="col" className="px-3 py-2 text-right font-normal">Columns</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...map.details.values()].map((table) => (
                        <tr key={table.name} className="border-b rule last:border-0">
                          <td className="px-3 py-1.5 font-mono">{table.name}</td>
                          <td className="px-3 py-1.5">{KIND_NAMES[table.kind]}</td>
                          <td className="tabular px-3 py-1.5 text-right">{table.columns.length}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          ) : (
            <div className="border-l-2 py-1 pl-4" style={{ borderColor: "var(--accent-quiet)" }}>
              <p className="text-[16px]">
                {applied.trim() ? "No CREATE TABLE statements found." : "Paste a schema to see its map."}
              </p>
              <p className="mt-1 text-[14px] text-[var(--fg-muted)]">
                Use the output of <code className="font-mono">pg_dump --schema-only</code>,{" "}
                <code className="font-mono">mysqldump --no-data</code>, or a SQL Server
                &ldquo;Script table as CREATE&rdquo;. Or start from the example.
              </p>
            </div>
          )}
        </section>
      </div>

      <section aria-labelledby="limits" className="mt-20 grid grid-cols-1 gap-10 border-t rule pt-10 md:grid-cols-2">
        <div>
          <h2 id="limits" className="display text-[clamp(1.5rem,2.6vw,2rem)] leading-none">
            What a schema can tell you
          </h2>
          <p className="mt-4 max-w-[32rem] text-[15px] leading-relaxed text-[var(--fg-muted)]">
            Tables, columns and declared keys, which is enough to see how a database is shaped,
            spot likely backups and scratch tables by their names, and guess at joins from
            matching column names. The guesses are only guesses.
          </p>
          <p className="mt-4 max-w-[32rem] text-[15px] leading-relaxed text-[var(--fg-muted)]">
            It cannot see the data. Rows marked deleted instead of deleted, a unit that changed
            partway through, one table holding several kinds of record, what a code means: those
            live in the rows, and finding them is what excavation does.
          </p>
        </div>
        <div>
          <h2 className="text-[17px] font-medium">Run the full excavation</h2>
          <p className="mt-3 max-w-[32rem] text-[15px] leading-relaxed text-[var(--fg-muted)]">
            Stratum reads your database directly from your own machine, with a read-only
            connection: it parses every statement and refuses anything but a single SELECT.
          </p>
          <pre className="mt-4 overflow-x-auto border rule bg-[var(--surface)] p-4 text-[13px] leading-relaxed">
            <code className="font-mono">{`make setup
make engine    # in one terminal
make web       # in another, then open Connect`}</code>
          </pre>
        </div>
      </section>
    </main>
  );
}
