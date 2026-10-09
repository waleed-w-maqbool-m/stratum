/** A titled section of a long page. Takes part in RevealGroup's scroll reveal. */
export function Section({
  id,
  title,
  intro,
  children,
}: {
  id: string;
  title: string;
  intro: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section data-reveal aria-labelledby={id} className="mt-24 md:mt-32">
      <h2 id={id} className="display text-[clamp(1.9rem,3.6vw,2.8rem)] leading-none">
        {title}
      </h2>
      <p className="mt-4 max-w-[38rem] text-[16px] leading-relaxed text-[var(--fg-muted)]">
        {intro}
      </p>
      <div className="mt-10">{children}</div>
    </section>
  );
}
