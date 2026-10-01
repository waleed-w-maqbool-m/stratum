/**
 * A page's title and what it is for, in one sentence or two.
 *
 * No eyebrow label above it: the header already says which database this is, so the
 * space above a title is not spent repeating it.
 */
export function PageTitle({
  title,
  lede,
  aside,
}: {
  title: string;
  lede?: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-x-10 gap-y-4 border-b rule pb-6">
      <div className="max-w-[40rem]">
        <h1 className="display text-[clamp(2.1rem,4.4vw,3.2rem)] leading-[0.95]">
          {title}
        </h1>
        {lede && (
          <p className="mt-3 text-[15px] leading-relaxed text-[var(--fg-muted)]">
            {lede}
          </p>
        )}
      </div>
      {aside}
    </header>
  );
}
