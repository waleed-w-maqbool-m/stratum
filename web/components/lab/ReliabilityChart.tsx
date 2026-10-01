"use client";

import { useState } from "react";

import type { Reliability } from "@/lib/eval";

/**
 * Does a confidence of 0.8 mean right 80% of the time?
 *
 * Each point is a band of answers: across, the confidence they were given; up, how
 * often they were actually right. On the dashed line the score means exactly what it
 * says. The fitted points should sit closer to it than the unfitted ones — that is
 * the whole claim calibration makes, drawn rather than summarised.
 */

const SIZE = 320;
const PAD = { left: 62, right: 16, top: 14, bottom: 40 };
const W = SIZE + PAD.left + PAD.right;
const H = SIZE + PAD.top + PAD.bottom;

const x = (v: number) => PAD.left + v * SIZE;
const y = (v: number) => PAD.top + (1 - v) * SIZE;

type Hover = { series: string; bin: Reliability } | null;

export function ReliabilityChart({
  before,
  after,
}: {
  before: Reliability[];
  after: Reliability[];
}) {
  const [hover, setHover] = useState<Hover>(null);
  const series = [
    { name: "Before fitting", bins: before.filter((b) => b.count), tone: "var(--series-context)" },
    { name: "After fitting", bins: after.filter((b) => b.count), tone: "var(--series-focus)" },
  ];
  const biggest = Math.max(1, ...series.flatMap((s) => s.bins.map((b) => b.count)));
  const radius = (count: number) => 4 + 9 * Math.sqrt(count / biggest);

  return (
    <figure>
      <div className="relative max-w-[26rem]">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
             aria-label="Reliability diagram: claimed confidence against actual accuracy, before and after fitting">
          {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
            <g key={tick}>
              <line x1={x(0)} x2={x(1)} y1={y(tick)} y2={y(tick)} stroke="var(--rule)" strokeWidth={1} />
              <text x={x(0) - 8} y={y(tick) + 4} textAnchor="end" fontSize="11" fill="var(--fg-muted)"
                    className="tabular">{tick.toFixed(2)}</text>
              <text x={x(tick)} y={y(0) + 18} textAnchor="middle" fontSize="11" fill="var(--fg-muted)"
                    className="tabular">{tick.toFixed(2)}</text>
            </g>
          ))}
          <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="var(--fg-muted)"
                strokeDasharray="4 5" strokeWidth={1} />
          <text x={x(0.5)} y={H - 4} textAnchor="middle" fontSize="12" fill="var(--fg-muted)">
            Confidence it gave
          </text>
          <text x={12} y={y(0.5)} textAnchor="middle" fontSize="12" fill="var(--fg-muted)"
                transform={`rotate(-90 12 ${y(0.5)})`}>
            How often it was right
          </text>

          {series.map((s) =>
            s.bins.map((bin) => (
              <circle
                key={`${s.name}-${bin.range[0]}`}
                cx={x(bin.predicted)}
                cy={y(bin.observed)}
                r={radius(bin.count)}
                fill={s.name === "After fitting" ? s.tone : "none"}
                stroke={s.tone}
                strokeWidth={2}
                tabIndex={0}
                onMouseEnter={() => setHover({ series: s.name, bin })}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover({ series: s.name, bin })}
                onBlur={() => setHover(null)}
                style={{ outline: "none", cursor: "default" }}
              >
                <title>{`${s.name}: ${bin.count} answers given ${bin.predicted.toFixed(2)}, right ${Math.round(bin.observed * 100)}% of the time`}</title>
              </circle>
            )),
          )}
        </svg>
        {hover && (
          <div
            className="pointer-events-none absolute border rule bg-[var(--surface)] px-2.5 py-1.5 text-[12px] leading-snug"
            style={{
              left: `${(x(hover.bin.predicted) / W) * 100}%`,
              top: `${(y(hover.bin.observed) / H) * 100}%`,
              transform: "translate(12px, -110%)",
            }}
          >
            <p className="font-medium">{hover.series}</p>
            <p className="tabular text-[var(--fg-muted)]">
              {hover.bin.count} answers, confidence {hover.bin.predicted.toFixed(2)}
            </p>
            <p className="tabular">right {Math.round(hover.bin.observed * 100)}% of the time</p>
          </div>
        )}
      </div>
      <figcaption className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[13px] text-[var(--fg-muted)]">
        {series.map((s) => (
          <span key={s.name} className="flex items-center gap-2">
            <svg width="12" height="12" aria-hidden>
              <circle cx="6" cy="6" r="5" stroke={s.tone} strokeWidth="2"
                      fill={s.name === "After fitting" ? s.tone : "none"} />
            </svg>
            {s.name}
          </span>
        ))}
        <span className="flex items-center gap-2">
          <svg width="18" height="12" aria-hidden>
            <line x1="0" y1="6" x2="18" y2="6" stroke="var(--fg-muted)" strokeDasharray="3 3" />
          </svg>
          Perfectly calibrated
        </span>
      </figcaption>

      <table className="sr-only">
        <caption>Reliability bins before and after fitting</caption>
        <thead>
          <tr><th>Series</th><th>Answers</th><th>Confidence</th><th>Accuracy</th></tr>
        </thead>
        <tbody>
          {series.flatMap((s) => s.bins.map((bin) => (
            <tr key={`${s.name}-${bin.range[0]}`}>
              <td>{s.name}</td><td>{bin.count}</td>
              <td>{bin.predicted.toFixed(2)}</td><td>{Math.round(bin.observed * 100)}%</td>
            </tr>
          )))}
        </tbody>
      </table>
    </figure>
  );
}
