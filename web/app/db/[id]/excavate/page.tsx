"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";

import { PageTitle } from "@/components/PageTitle";
import { api, stream, type TraceEvent } from "@/lib/api";
import { DEMO } from "@/lib/demo";

/**
 * The live excavation view (PLAN.md 5.3).
 *
 * Every detector is laid out before it runs, as an empty layer saying what it will look
 * for, and fills when its event arrives with what it found in a sentence. The order is
 * the order they run in, and the one after the last filled layer is the one working,
 * so the page answers "what is it doing now" without a spinner.
 */

type Detail = Record<string, number | string>;

const STAGES: { stage: string; looks: string; found: (d: Detail) => string }[] = [
  { stage: "introspect", looks: "Reading the catalogue", found: (d) => `${n(d.tables)} tables, ${n(d.views)} views and ${n(d.columns)} columns; ${n(d.declared_foreign_keys)} declared foreign keys` },
  { stage: "profile", looks: "Sampling every table", found: (d) => `Sampled ${n(d.tables)} tables${Number(d.skipped) ? `, skipped ${n(d.skipped)} too slow to read` : ""}` },
  { stage: "artifacts", looks: "Telling live tables from backups and leftovers", found: (d) => `${n(d.core)} tables hold live data; ${n(d.excluded)} are backups, archives or leftovers` },
  { stage: "procmine", looks: "Reading views and procedures for undocumented rules", found: (d) => `${n(d.routines_parsed)} views and procedures read: ${n(d.joins)} joins, ${n(d.enums)} code lists, ${n(d.filters)} filters` },
  { stage: "godtable", looks: "Finding tables that hold several kinds of record", found: (d) => count(d.god_tables, "table holds", "tables hold", "several kinds of record") },
  { stage: "joins", looks: "Rebuilding the relationships nothing declares", found: (d) => count(d.relationships, "relationship", "relationships", "found in the data") },
  { stage: "keys", looks: "Finding the keys that identify rows", found: (d) => count(d.keys, "key", "keys", "identified") },
  { stage: "softdelete", looks: "Finding rows marked deleted instead of removed", found: (d) => count(d.rules, "deletion marker", "deletion markers", "found") },
  { stage: "enums", looks: "Decoding the magic numbers and letter codes", found: (d) => count(d.enums, "code column", "code columns", "found") },
  { stage: "formats", looks: "Finding XML, JSON and key=value inside text columns", found: (d) => count(d.columns, "column holds", "columns hold", "structured text") },
  { stage: "overloaded", looks: "Finding columns that mean different things per row", found: (d) => count(d.columns, "column means", "columns mean", "two things") },
  { stage: "drift", looks: "Finding where the schema changed under the data", found: (d) => count(d.drifts, "change", "changes", "over time") },
  { stage: "types", looks: "Finding facts stored in the wrong or mixed types", found: (d) => count(d.mismatches, "type problem", "type problems", "found") },
  { stage: "quality", looks: "Finding padding, null markers and spelling variants", found: (d) => count(d.issues, "messy column", "messy columns", "found") },
  { stage: "perf", looks: "Finding tables that will be slow to query", found: (d) => count(d.traps, "slow table", "slow tables", "found") },
  { stage: "enrich", looks: "Asking a language model what the data could not say", found: (d) => Number(d.calls) ? `${n(d.findings)} names and meanings suggested in ${n(d.calls)} calls` : `${n(d.findings)} names and meanings, reused from the last run` },
];

function n(value: unknown): string {
  return Number(value ?? 0).toLocaleString();
}

function count(value: unknown, one: string, many: string, rest: string): string {
  const k = Number(value ?? 0);
  if (!k) return "None found";
  return `${k.toLocaleString()} ${k === 1 ? one : many} ${rest}`;
}

type Done = {
  version: number;
  summary: { findings: number; total_ms: number; total_tokens: number };
  model: { entities: number; relationships: number; open_hypotheses: number };
  decisions_kept?: number;
};

export default function ExcavatePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [events, setEvents] = useState<Record<string, TraceEvent>>({});
  const [running, setRunning] = useState(false);
  const [done, setDone] = useState<Done | null>(null);
  const [error, setError] = useState("");
  const [excavated, setExcavated] = useState<boolean | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    api
      .connections()
      .then((all) => setExcavated(all.find((c) => c.id === id)?.excavated ?? false))
      .catch(() => setExcavated(false));
    return () => abort.current?.abort();
  }, [id]);

  const run = useCallback(async () => {
    abort.current?.abort();
    abort.current = new AbortController();
    setRunning(true);
    setEvents({});
    setDone(null);
    setError("");
    await stream(
      `/databases/${id}/excavate`,
      {},
      {
        onEvent: (name, data) => {
          if (name !== "stage.end") return;
          const event = data as TraceEvent;
          setEvents((prior) => ({ ...prior, [event.stage]: event }));
        },
        onDone: (data) => {
          setDone(data as Done);
          setExcavated(true);
          setRunning(false);
        },
        onError: (message) => {
          setError(message);
          setRunning(false);
        },
      },
      abort.current.signal,
    ).catch((exception) => {
      if ((exception as Error).name !== "AbortError") setError(String(exception));
      setRunning(false);
    });
  }, [id]);

  const working = running ? STAGES.find((s) => !events[s.stage])?.stage : undefined;

  return (
    <main className="mx-auto max-w-[76rem] px-5 py-10 sm:px-8">
      <PageTitle
        title="Work out what the database means"
        lede="Sixteen steps read the schema and sample the data. Most answers are computed; a language model is asked only about what the data cannot say, such as what a code means. Everything they conclude goes to Review with its evidence."
        aside={
          <div className="flex flex-col items-start gap-2">
            <button
              type="button"
              onClick={run}
              disabled={running}
              className="px-5 py-3 text-[15px] font-medium disabled:opacity-50"
              style={{ background: "var(--accent)", color: "var(--on-accent)" }}
            >
              {running ? "Excavating…" : excavated ? "Excavate again" : "Excavate"}
            </button>
            {excavated && !running && !done && (
              <p className="max-w-[18rem] text-[13px] text-[var(--fg-muted)]">
                {DEMO
                  ? "This replays a recorded excavation of this database, at the speed it ran."
                  : "Already excavated. Running again builds a new model version and keeps the decisions you made in Review."}
              </p>
            )}
          </div>
        }
      />

      {error && (
        <p role="alert" className="mb-6 border-l-2 pl-3 text-[15px]" style={{ borderColor: "var(--danger)" }}>
          Excavation stopped: {error}
        </p>
      )}

      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <ol aria-label="Detectors, in the order they run" aria-busy={running} className="border-t rule">
          {STAGES.map(({ stage, looks, found }) => {
            const event = events[stage];
            const active = stage === working;
            const nothing = event && /^None found/.test(found(event.detail as Detail));
            return (
              <li
                key={stage}
                className="grid grid-cols-[3px_minmax(0,1fr)_auto] items-baseline gap-x-4 border-b rule py-3"
                aria-current={active ? "step" : undefined}
              >
                <span
                  aria-hidden
                  className={`self-stretch ${event ? "stratum" : ""} ${active ? "animate-pulse" : ""}`}
                  style={{
                    background: event
                      ? nothing ? "var(--rule)" : "var(--accent)"
                      : active ? "var(--accent-quiet)" : "transparent",
                  }}
                />
                <div className="min-w-0">
                  <p className="text-[14px] text-[var(--fg-muted)]">{looks}</p>
                  <p
                    className="mt-0.5 text-[16px]"
                    style={{ color: event ? (nothing ? "var(--fg-muted)" : "var(--fg)") : "var(--fg-muted)" }}
                  >
                    {event ? found(event.detail as Detail) : active ? "Working…" : ""}
                  </p>
                </div>
                <span className="tabular text-[12px] text-[var(--fg-muted)]">
                  {event && (
                    <>
                      {event.ms >= 1000 ? `${(event.ms / 1000).toFixed(1)} s` : `${event.ms} ms`}
                      {event.tokens > 0 && <span className="ml-2 text-[var(--accent)]">{event.tokens.toLocaleString()} tokens</span>}
                    </>
                  )}
                </span>
              </li>
            );
          })}
        </ol>

        <aside aria-live="polite" className="lg:sticky lg:top-6 lg:self-start">
          {done ? (
            <div>
              <p className="display text-[3.5rem] leading-none text-[var(--accent)]">
                {done.summary.findings.toLocaleString()}
              </p>
              <p className="mt-2 text-[16px]">things worked out about {id}</p>
              <dl className="mt-5 space-y-2 text-[15px]">
                <Fact label="Entities in the model" value={done.model.entities} />
                <Fact label="Relationships" value={done.model.relationships} />
                <Fact label="Seconds" value={Number((done.summary.total_ms / 1000).toFixed(1))} />
                <Fact label="Language model tokens" value={done.summary.total_tokens} />
                {done.decisions_kept ? <Fact label="Earlier decisions kept" value={done.decisions_kept} /> : null}
              </dl>
              <div className="mt-6 flex flex-wrap gap-2">
                <Link
                  href={`/db/${id}/workbench`}
                  className="px-4 py-2 text-[14px]"
                  style={{ background: "var(--accent)", color: "var(--on-accent)" }}
                >
                  Review what it found
                </Link>
                <Link href={`/db/${id}/ask`} className="border rule px-4 py-2 text-[14px]">
                  Ask a question
                </Link>
              </div>
            </div>
          ) : (
            <p className="text-[14px] leading-relaxed text-[var(--fg-muted)]">
              {running
                ? "Each layer fills as its detector finishes. Sampling is the slow part on a large database."
                : "Nothing is written to the database. Every probe is a read-only SELECT with a timeout, and the language model sees names and a few sample values, never whole tables or credentials."}
            </p>
          )}
        </aside>
      </div>
    </main>
  );
}

function Fact({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b rule pb-2">
      <dt className="text-[var(--fg-muted)]">{label}</dt>
      <dd className="tabular">{value.toLocaleString()}</dd>
    </div>
  );
}
