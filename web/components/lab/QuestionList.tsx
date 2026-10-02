"use client";

import { useMemo, useState } from "react";

import { TAG_NAMES, type Preview, type QuestionRow } from "@/lib/eval";

/**
 * Every question in the suite, with both systems' verdicts, and — opened — the gold
 * answer next to Stratum's so a failure can be read rather than taken on trust.
 */

type Filter = "all" | "wrong" | "fixed" | "both-wrong";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All questions" },
  { id: "wrong", label: "Stratum got wrong" },
  { id: "fixed", label: "Only Stratum got right" },
  { id: "both-wrong", label: "Both got wrong" },
];

function Verdict({ row }: { row?: QuestionRow }) {
  if (!row || !row.answered || row.correct === undefined) {
    return <span className="text-[13px] text-[var(--fg-muted)]">not run</span>;
  }
  return row.correct ? (
    <span className="text-[13px] text-[var(--good)]">
      <span aria-hidden>✓ </span>right
    </span>
  ) : (
    <span className="text-[13px] text-[var(--danger)]">
      <span aria-hidden>✗ </span>wrong
    </span>
  );
}

function ResultTable({ title, preview }: { title: string; preview?: Preview | { error: string } }) {
  if (!preview) {
    return (
      <div>
        <p className="mb-2 text-[13px] text-[var(--fg-muted)]">{title}</p>
        <p className="text-[13px] text-[var(--fg-muted)]">No result.</p>
      </div>
    );
  }
  if ("error" in preview) {
    return (
      <div>
        <p className="mb-2 text-[13px] text-[var(--fg-muted)]">{title}</p>
        <p className="text-[13px] text-[var(--danger)]">{preview.error}</p>
      </div>
    );
  }
  return (
    <div className="min-w-0">
      <p className="mb-2 text-[13px] text-[var(--fg-muted)]">
        {title}
        <span className="tabular ml-2">
          {preview.total} row{preview.total === 1 ? "" : "s"}
        </span>
      </p>
      <div className="overflow-x-auto border rule">
        <table className="w-full text-[13px]">
          {preview.columns.length > 0 && !preview.columns[0].startsWith("col") && (
            <thead>
              <tr className="border-b rule text-left text-[var(--fg-muted)]">
                {preview.columns.map((column, index) => (
                  <th key={`${column}-${index}`} scope="col" className="px-2.5 py-1.5 font-normal">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {preview.rows.map((row, index) => (
              <tr key={index} className="border-b rule last:border-0">
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className="tabular px-2.5 py-1 whitespace-nowrap">
                    {cell === null ? <span className="text-[var(--fg-muted)]">null</span> : String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {preview.total > preview.rows.length && (
        <p className="mt-1 text-[12px] text-[var(--fg-muted)]">
          first {preview.rows.length} of {preview.total}
        </p>
      )}
    </div>
  );
}

function Detail({ row, baseline }: { row: QuestionRow; baseline?: QuestionRow }) {
  const [view, setView] = useState<"logical" | "physical">("physical");
  const sql = view === "logical" ? row.logical_sql : row.physical_sql;

  return (
    <div className="space-y-6 border-l-2 py-2 pl-5" style={{ borderColor: "var(--accent-quiet)" }}>
      {row.reason && row.correct === false && (
        <p className="text-[14px]">
          <span className="text-[var(--fg-muted)]">Why it was marked wrong: </span>
          {row.reason}
        </p>
      )}
      {row.expect && (
        <p className="text-[14px] text-[var(--fg-muted)]">
          This question has no single answer: it checks that Stratum{" "}
          {row.expect === "cost_guard" ? "stops an expensive query before running it" : "declines to answer"}.
          It {row.correct ? "did" : "did not"}.
        </p>
      )}

      {!row.expect && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          <ResultTable title="Correct answer, from the clean database" preview={row.gold} />
          <ResultTable title="Stratum’s answer, from the messy one" preview={row.ours} />
        </div>
      )}

      {row.assumptions && row.assumptions.length > 0 && (
        <div>
          <p className="mb-2 text-[13px] text-[var(--fg-muted)]">Rules the compiler applied</p>
          <ul className="space-y-1 text-[14px]">
            {row.assumptions.map((text, index) => (
              <li key={index} className="pl-3" style={{ borderLeft: "2px solid var(--rule)" }}>
                {text}
              </li>
            ))}
          </ul>
        </div>
      )}

      {sql && (
        <div>
          <div className="mb-2 flex items-center gap-3">
            <p className="text-[13px] text-[var(--fg-muted)]">Stratum’s SQL</p>
            <div role="group" aria-label="SQL view" className="flex border rule text-[12px]">
              {(["logical", "physical"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={view === option}
                  onClick={() => setView(option)}
                  className="px-2.5 py-0.5"
                  style={view === option ? { background: "var(--accent)", color: "var(--on-accent)" } : undefined}
                >
                  {option === "logical" ? "What the model wrote" : "What ran"}
                </button>
              ))}
            </div>
          </div>
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words border rule bg-[var(--surface)] p-4 text-[13px] leading-relaxed">
            <code className="font-mono">{sql}</code>
          </pre>
        </div>
      )}

      {baseline?.physical_sql && (
        <details>
          <summary className="cursor-pointer text-[13px] text-[var(--fg-muted)]">
            The baseline’s SQL ({baseline.correct ? "right" : "wrong"})
          </summary>
          <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-words border rule bg-[var(--surface)] p-4 text-[13px] leading-relaxed">
            <code className="font-mono">{baseline.physical_sql}</code>
          </pre>
        </details>
      )}
    </div>
  );
}

export function QuestionList({
  stratum,
  baseline,
}: {
  stratum: QuestionRow[];
  baseline: QuestionRow[];
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<string | null>(null);
  const theirs = useMemo(() => new Map(baseline.map((q) => [q.id, q])), [baseline]);

  const rows = stratum.filter((row) => {
    const base = theirs.get(row.id);
    if (filter === "wrong") return row.answered && row.correct === false;
    if (filter === "fixed") return row.correct === true && base?.answered && base.correct === false;
    if (filter === "both-wrong") return row.correct === false && base?.answered && base.correct === false;
    return true;
  });

  return (
    <div>
      <div role="group" aria-label="Filter questions" className="mb-5 flex flex-wrap gap-2">
        {FILTERS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            aria-pressed={filter === id}
            onClick={() => setFilter(id)}
            className="border rule px-3 py-1.5 text-[13px]"
            style={filter === id ? { background: "var(--fg)", color: "var(--bg)" } : undefined}
          >
            {label}
          </button>
        ))}
        <span className="tabular self-center pl-2 text-[13px] text-[var(--fg-muted)]">
          {rows.length} shown
        </span>
      </div>

      <ol className="border-t rule">
        {rows.map((row) => {
          const expanded = open === row.id;
          return (
            <li key={row.id} className="border-b rule">
              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setOpen(expanded ? null : row.id)}
                className="grid w-full grid-cols-[1fr_auto] gap-x-6 gap-y-1 py-3.5 text-left sm:grid-cols-[3.5rem_1fr_5rem_5rem]"
              >
                <span className="tabular hidden pt-0.5 text-[13px] text-[var(--fg-muted)] sm:block">
                  {row.id}
                </span>
                <span className="min-w-0">
                  <span className="block text-[15px]">{row.question}</span>
                  <span className="mt-1 block text-[12px] text-[var(--fg-muted)]">
                    {row.tags.map((tag) => TAG_NAMES[tag] ?? tag).join(", ")}
                  </span>
                </span>
                <span className="sm:text-right">
                  <span className="sr-only">Baseline: </span>
                  <Verdict row={theirs.get(row.id)} />
                </span>
                <span className="sm:text-right">
                  <span className="sr-only">Stratum: </span>
                  <Verdict row={row} />
                </span>
              </button>
              {expanded && (
                <div className="pb-6 sm:pl-[4.5rem]">
                  <Detail row={row} baseline={theirs.get(row.id)} />
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
