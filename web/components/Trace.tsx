"use client";

import type { TraceEvent } from "@/lib/api";

/**
 * The pipeline trace as a core sample (PLAN.md 5.2).
 *
 * Each stage is a stratum that fills as its SSE event arrives, with milliseconds and
 * tokens printed in mono. The animation is not decoration: the height of a band is its
 * share of the elapsed time, so where a question spent its budget is visible at a
 * glance, and a repair shows as a visible re-drill.
 */

const STAGE_LABELS: Record<string, string> = {
  route: "route",
  link: "link",
  joinplan: "plan joins",
  generate: "generate",
  compile: "compile",
  validate: "validate",
  execute: "execute",
  select: "select",
  confidence: "confidence",
  explain: "explain",
};

function label(stage: string): string {
  if (stage.startsWith("repair:")) return `repair ${stage.split(":")[1]}`;
  if (stage.startsWith("generate:")) return `generate (${stage.split(":")[1]})`;
  return STAGE_LABELS[stage] ?? stage;
}

export function Trace({ events }: { events: TraceEvent[] }) {
  if (!events.length) return null;
  const total = Math.max(1, events.reduce((sum, e) => sum + e.ms, 0));
  // A band's height is its share of the time only when there are enough bands for a
  // share to mean something; a cached answer is one stage, not a 140px slab.
  const proportional = events.length > 3;

  return (
    <ol className="space-y-px" aria-label="pipeline trace">
      {events.map((event, index) => {
        const share = event.ms / total;
        const isRepair = event.stage.startsWith("repair");
        return (
          <li
            key={`${event.stage}-${index}`}
            className="stratum grid grid-cols-[1fr_auto] items-center gap-3 border-l-2 pl-3"
            style={{
              animationDelay: `${index * 40}ms`,
              minHeight: `${proportional ? Math.max(22, share * 140) : 28}px`,
              borderColor: isRepair ? "var(--danger)" : "var(--accent-quiet)",
              background: `color-mix(in oklab, var(--color-slate) ${Math.round(
                12 + share * 55,
              )}%, transparent)`,
            }}
          >
            <span className="text-[13px] tracking-tight">
              {label(event.stage)}
              {isRepair && (
                <span className="ml-2 text-[12px] text-[var(--danger)]">drilled again</span>
              )}
            </span>
            <span className="tabular text-[11px] text-[var(--fg-muted)]">
              {event.ms}ms
              {event.tokens > 0 && (
                <span className="ml-2 text-[var(--accent)]">
                  {event.tokens}t
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
