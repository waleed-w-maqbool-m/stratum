"use client";

import { useState } from "react";

import { GAP_LABEL, type Gap, type GapType, type GapsResponse } from "@/lib/atlas";

/**
 * What Stratum thinks the database is missing.
 *
 * Kept apart from the map's joins on purpose: everything here is a suggestion, none of it
 * is used to answer a question, and the SQL is for a person to review, never run by this
 * tool. The wording says so rather than letting a list of "problems" imply certainty.
 */

const ORDER: GapType[] = [
  "undeclared_fk",
  "broken_relationship",
  "points_outside_core",
  "dangling_reference",
  "missing_unique",
  "missing_lookup",
  "isolated_table",
];

const PER_GROUP = 8;

function where(gap: Gap): string {
  const columns = gap.columns.length ? `.${gap.columns.join(", ")}` : "";
  const target = gap.to_table
    ? ` → ${gap.to_table}${gap.to_columns.length ? `.${gap.to_columns.join(", ")}` : ""}`
    : "";
  return `${gap.table}${columns}${target}`;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="border rule px-2 py-0.5 text-[12px] hover:border-[var(--accent)]"
    >
      {copied ? "Copied" : "Copy SQL"}
    </button>
  );
}

export function GapsSection({
  gaps,
  onSelect,
}: {
  gaps: GapsResponse;
  onSelect: (table: string) => void;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const groups = ORDER.map((type) => ({
    type,
    items: gaps.items.filter((gap) => gap.gap_type === type),
    total: gaps.by_type[type] ?? 0,
  })).filter((group) => group.total > 0);

  return (
    <section aria-labelledby="gaps-heading" className="mt-14 border-t rule pt-8">
      <h2 id="gaps-heading" className="display text-[clamp(1.4rem,2.4vw,1.9rem)] leading-none">
        What may be missing
      </h2>
      <p className="mt-3 max-w-[40rem] text-[14px] leading-relaxed text-[var(--fg-muted)]">
        Stratum compares the database with what it expects a well-kept one to hold. These are
        suggestions, not findings: none of them is used to answer a question, and the SQL is
        written for you to review and run yourself. Stratum never runs it.
      </p>

      <div className="mt-6 space-y-8">
        {groups.map(({ type, items, total }) => (
          <div key={type}>
            <h3 className="text-[15px] font-medium">
              {GAP_LABEL[type].title}
              <span className="tabular ml-2 text-[13px] font-normal text-[var(--fg-muted)]">
                {total}
              </span>
            </h3>
            <p className="mt-1 max-w-[40rem] text-[13px] text-[var(--fg-muted)]">
              {GAP_LABEL[type].blurb}
            </p>
            <ul className="mt-3 divide-y rule border-y rule">
              {items.slice(0, open[type] ? items.length : PER_GROUP).map((gap) => (
                <li key={gap.id} className="py-2">
                  <details>
                    <summary className="flex cursor-pointer flex-wrap items-baseline gap-x-3 text-[13px]">
                      <span className="font-mono break-all">{where(gap)}</span>
                      <span className="tabular text-[var(--fg-muted)]">
                        {Math.round(gap.confidence * 100)}% sure
                      </span>
                    </summary>
                    <p className="mt-2 max-w-[44rem] text-[13px] leading-relaxed">{gap.reason}</p>
                    {gap.blockers.length > 0 && (
                      <ul className="mt-2 list-disc pl-5 text-[13px] text-[var(--fg-muted)]">
                        {gap.blockers.map((blocker) => (
                          <li key={blocker}>{blocker}</li>
                        ))}
                      </ul>
                    )}
                    {gap.fix && (
                      <div className="mt-3">
                        <pre className="overflow-x-auto border rule bg-[var(--surface)] p-3 text-[12px] leading-relaxed">
                          <code className="font-mono">{gap.fix}</code>
                        </pre>
                        <div className="mt-2 flex items-center gap-3">
                          <CopyButton text={gap.fix} />
                          <span className="text-[12px] text-[var(--fg-muted)]">
                            For you to review and run. Stratum does not execute it.
                          </span>
                        </div>
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => onSelect(gap.table)}
                      className="mt-2 text-[12px] underline underline-offset-2"
                    >
                      Show {gap.table} on the map
                    </button>
                  </details>
                </li>
              ))}
            </ul>
            {items.length > PER_GROUP && (
              <button
                type="button"
                onClick={() => setOpen((state) => ({ ...state, [type]: !state[type] }))}
                className="mt-2 text-[13px] underline underline-offset-2"
              >
                {open[type] ? "Show fewer" : `Show all ${items.length}`}
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
