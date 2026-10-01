/**
 * Baseline against Stratum for one group of questions: two thin bars, the focus series
 * in amber and the baseline as recessive context.
 *
 * Identity is never colour alone — each bar carries its name and value as text — so the
 * pair reads the same in greyscale, for a colour-blind reader, and in forced colours.
 * Hovering or focusing a bar gives the raw counts behind the percentage.
 */

type Series = { value: number; correct?: number; total?: number };

function Bar({
  name,
  series,
  tone,
  labelWidth,
}: {
  name: string;
  series: Series;
  tone: string;
  labelWidth: string;
}) {
  const percent = Math.round(series.value * 100);
  const detail =
    series.total !== undefined
      ? `${name}: ${series.correct} of ${series.total} correct (${percent}%)`
      : `${name}: ${percent}%`;

  return (
    <div
      className="group relative grid items-center gap-3"
      style={{ gridTemplateColumns: `${labelWidth} 1fr 2.6rem` }}
    >
      <span className="text-[13px] text-[var(--fg-muted)]">{name}</span>
      <span
        tabIndex={0}
        aria-label={detail}
        className="relative block h-[10px] outline-offset-4"
      >
        <span
          data-bar
          className="absolute inset-y-0 left-0 origin-left rounded-r-[4px]"
          style={{ width: `${Math.max(percent, 0.8)}%`, background: tone }}
        />
        <span
          aria-hidden
          className="pointer-events-none absolute -top-9 left-0 z-10 whitespace-nowrap border rule bg-[var(--surface)] px-2 py-1 text-[12px] opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
        >
          {detail}
        </span>
      </span>
      <span className="tabular text-right text-[13px]">{percent}%</span>
    </div>
  );
}

export function PairedBar({
  baseline,
  stratum,
  labelWidth = "4.6rem",
}: {
  baseline: Series;
  stratum: Series;
  labelWidth?: string;
}) {
  return (
    <div className="space-y-[2px]">
      <Bar name="Baseline" series={baseline} tone="var(--series-context)" labelWidth={labelWidth} />
      <Bar name="Stratum" series={stratum} tone="var(--series-focus)" labelWidth={labelWidth} />
    </div>
  );
}
