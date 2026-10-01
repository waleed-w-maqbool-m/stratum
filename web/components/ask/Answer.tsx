"use client";

import { useState } from "react";

import type { Answer } from "@/lib/api";

/**
 * An answer, and everything needed to check it.
 *
 * Order matters here. The result first, because that is what was asked for; then how
 * confident Stratum is and why; then the rules it applied, because an answer that came
 * back lower than expected is only a mystery if the excluded rows are invisible; then
 * the SQL, in both the form the model wrote and the form that actually ran.
 */

const BANDS = {
  show: { label: "Confident", tone: "var(--good)" },
  assumptions: { label: "Shown with the assumptions below", tone: "var(--accent)" },
  abstain: { label: "Not confident enough to rely on", tone: "var(--danger)" },
} as const;

// A fractional number keeps two places: 177.6 in a column of money reads as a typo.
const DECIMALS = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString()
      : value.toLocaleString(undefined, DECIMALS);
  }
  const numeric = typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value.trim());
  if (numeric) {
    const n = Number(value);
    return Number.isInteger(n) ? n.toLocaleString() : n.toLocaleString(undefined, DECIMALS);
  }
  return String(value);
}

/** The explanation's paragraphs that say something the page does not already show. The
 * engine's explanation also lists the rules and the row count, which sit above it. */
function plainWords(explanation: string): string[] {
  return (explanation || "")
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(
      (paragraph) =>
        paragraph &&
        !/^assumptions applied/i.test(paragraph) &&
        !/^returned \d/i.test(paragraph),
    );
}

function isNumeric(value: unknown): boolean {
  return typeof value === "number" || (typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value.trim()));
}

function humanise(column: string): string {
  const text = column.replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function Confidence({ answer }: { answer: Answer }) {
  const confidence = answer.confidence;
  if (!confidence) return null;
  const band = BANDS[confidence.band] ?? BANDS.assumptions;
  const score = Math.max(0, Math.min(1, confidence.score));

  return (
    <div className="border-t rule pt-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <p className="text-[15px]">
          <span style={{ color: band.tone }}>{band.label}</span>
          {confidence.verified && (
            <span className="ml-3 text-[13px] text-[var(--good)]">
              <span aria-hidden>✓ </span>a person verified this query
            </span>
          )}
        </p>
        <p className="tabular text-[13px] text-[var(--fg-muted)]">
          confidence {score.toFixed(2)}
        </p>
      </div>
      {/* The needle settles where the score is, with a little overshoot: the motion
        * says "this was measured", and it lands on the band the colour names. */}
      <div
        role="meter"
        aria-label="Answer confidence"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(score * 100)}
        aria-valuetext={`${Math.round(score * 100)} percent, ${band.label}`}
        className="relative mt-3 h-[6px]"
        style={{
          background:
            "linear-gradient(to right, color-mix(in oklab, var(--danger) 45%, transparent) 0 50%, color-mix(in oklab, var(--accent) 45%, transparent) 50% 80%, color-mix(in oklab, var(--good) 45%, transparent) 80% 100%)",
        }}
      >
        <span
          className="confidence-needle absolute -top-[5px] h-4 w-[3px]"
          style={{ left: `calc(${score * 100}% - 1.5px)`, background: "var(--fg)" }}
        />
      </div>
      {confidence.reasons.length > 0 && (
        <ul className="mt-3 space-y-1 text-[14px] text-[var(--fg-muted)]">
          {confidence.reasons.slice(0, 3).map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Result({ answer }: { answer: Answer }) {
  const single = answer.rows.length === 1 && answer.columns.length === 1;
  if (single) {
    const value = answer.rows[0][0];
    return (
      <div>
        <p className="text-[14px] text-[var(--fg-muted)]">{humanise(answer.columns[0])}</p>
        <p className="display count-in mt-1 text-[clamp(3rem,8vw,5.5rem)] leading-none text-[var(--accent)]">
          {formatCell(value)}
        </p>
      </div>
    );
  }
  if (!answer.rows.length) {
    return <p className="text-[16px]">The query ran and returned no rows.</p>;
  }
  const shown = answer.rows.slice(0, 30);
  return (
    <div>
      <div className="overflow-x-auto border rule">
        <table className="w-full text-[14px]">
          <caption className="sr-only">
            {`Result: ${answer.row_count} rows, columns ${answer.columns.join(", ")}`}
          </caption>
          <thead>
            <tr className="border-b rule text-left">
              {answer.columns.map((column, index) => (
                <th
                  key={`${column}-${index}`}
                  scope="col"
                  className={`px-3 py-2 text-[13px] font-normal text-[var(--fg-muted)] ${
                    isNumeric(answer.rows[0]?.[index]) ? "text-right" : ""
                  }`}
                >
                  {humanise(column)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, rowIndex) => (
              <tr
                key={rowIndex}
                className="row-in border-b rule last:border-0"
                style={{ animationDelay: `${Math.min(rowIndex, 20) * 18}ms` }}
              >
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    className={`px-3 py-1.5 ${isNumeric(cell) ? "tabular text-right" : ""}`}
                  >
                    {cell === null ? <span className="text-[var(--fg-muted)]">empty</span> : formatCell(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[13px] text-[var(--fg-muted)]">
        {answer.row_count > shown.length
          ? `Showing ${shown.length} of ${answer.row_count.toLocaleString()} rows`
          : `${answer.row_count.toLocaleString()} row${answer.row_count === 1 ? "" : "s"}`}
      </p>
    </div>
  );
}

export function AnswerView({ answer }: { answer: Answer }) {
  const [view, setView] = useState<"logical" | "physical">("physical");
  const sql = view === "logical" ? answer.logical_sql : answer.physical_sql;

  return (
    <div className="space-y-8">
      <Result answer={answer} />
      <Confidence answer={answer} />

      {answer.assumptions.length > 0 && (
        <section aria-labelledby="rules" className="border-t rule pt-5">
          <h2 id="rules" className="text-[15px] font-medium">
            Rules applied to get this answer
          </h2>
          <ul className="mt-3 space-y-3">
            {answer.assumptions.map((assumption, index) => (
              <li key={index} className="border-l-2 pl-4" style={{ borderColor: "var(--accent-quiet)" }}>
                <p className="text-[15px]">
                  {assumption.text}
                  {assumption.rows_excluded > 0 && (
                    <span className="tabular ml-2 text-[13px] text-[var(--fg-muted)]">
                      {assumption.rows_excluded.toLocaleString()} rows left out
                    </span>
                  )}
                </p>
                {assumption.expr && (
                  <code className="mt-1 block font-mono text-[12px] text-[var(--fg-muted)]">
                    {assumption.expr}
                  </code>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {answer.physical_sql && (
        <section aria-labelledby="sql" className="border-t rule pt-5">
          <div className="flex flex-wrap items-center gap-3">
            <h2 id="sql" className="text-[15px] font-medium">The SQL</h2>
            <div role="group" aria-label="Which SQL to show" className="flex border rule text-[13px]">
              {(["logical", "physical"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-pressed={view === option}
                  onClick={() => setView(option)}
                  className="px-3 py-1"
                  style={view === option ? { background: "var(--fg)", color: "var(--bg)" } : undefined}
                >
                  {option === "logical" ? "What the model wrote" : "What ran on the database"}
                </button>
              ))}
            </div>
          </div>
          <pre className="mt-3 max-h-[26rem] overflow-auto whitespace-pre-wrap break-words border rule bg-[var(--surface)] p-4 text-[13px] leading-relaxed">
            <code className="font-mono">{sql}</code>
          </pre>
          <p className="tabular mt-2 text-[13px] text-[var(--fg-muted)]">
            {answer.tokens.toLocaleString()} tokens, {(answer.elapsed_ms / 1000).toFixed(1)} s
            {answer.repairs > 0 && `, fixed itself ${answer.repairs} time${answer.repairs === 1 ? "" : "s"}`}
          </p>
        </section>
      )}

      {plainWords(answer.explanation).length > 0 && (
        <section aria-labelledby="explanation" className="border-t rule pt-5">
          <h2 id="explanation" className="text-[15px] font-medium">In plain words</h2>
          {plainWords(answer.explanation).map((paragraph) => (
            <p key={paragraph} className="mt-2 text-[15px] leading-relaxed text-[var(--fg-muted)]">
              {paragraph}
            </p>
          ))}
        </section>
      )}
    </div>
  );
}
