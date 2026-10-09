/**
 * The Stratum mark: a core sample in miniature, with the wordmark beside it.
 *
 * One component, used by the header and by the intro, so the logo that builds in the
 * middle of the screen and the one it docks into are the same pixels. The glyph's
 * layers carry `data-layer` so the intro can build them one at a time.
 */

export function CoreGlyph({ size = 18 }: { size?: number }) {
  const layer = { transformBox: "fill-box" as const };
  return (
    <svg width={size * 0.55} height={size} viewBox="0 0 11 20" aria-hidden>
      <rect data-layer style={layer} x="0" y="0" width="11" height="20" rx="1.5" fill="var(--color-slate)" />
      <rect data-layer style={layer} x="0" y="3" width="11" height="3" fill="#8a6021" />
      <rect data-layer style={layer} x="0" y="8" width="11" height="2" fill="#d9573b" />
      <rect data-layer style={layer} x="0" y="12" width="11" height="5" fill="#ffb23f" />
    </svg>
  );
}

export function Brand({ size = 18 }: { size?: number }) {
  return (
    <span
      className="flex items-center gap-2 text-[17px]"
      style={{
        fontFamily: "var(--font-display)",
        fontWeight: 700,
        fontVariationSettings: '"wdth" 85',
      }}
    >
      <CoreGlyph size={size} />
      <span data-wordmark>Stratum</span>
    </span>
  );
}
