"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api, type Connection } from "@/lib/api";

/**
 * The one piece of chrome every page shares.
 *
 * It carries the context each page used to repeat in its own heading — which database
 * you are looking at, and where else you can go with it — so page titles can say what
 * the page is for instead. Inside a database the sections read left to right in the
 * order you would use them: ask, see what was excavated, map it, review it.
 */

const SECTIONS = [
  { slug: "ask", label: "Ask" },
  { slug: "excavate", label: "Excavate" },
  { slug: "atlas", label: "Atlas" },
  { slug: "workbench", label: "Review" },
] as const;

export function SiteHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const [connections, setConnections] = useState<Connection[]>([]);

  const match = pathname.match(/^\/db\/([^/]+)\/([^/]+)/);
  const database = match ? decodeURIComponent(match[1]) : null;
  const section = match ? match[2] : null;

  useEffect(() => {
    api.connections().then(setConnections).catch(() => setConnections([]));
  }, [pathname]);

  const link = (href: string, label: string, current: boolean) => (
    <Link
      key={href}
      href={href}
      aria-current={current ? "page" : undefined}
      className="relative py-1 text-[14px] transition-colors"
      style={{ color: current ? "var(--fg)" : "var(--fg-muted)" }}
    >
      {label}
      {current && (
        <span
          aria-hidden
          className="absolute inset-x-0 -bottom-[13px] hidden h-[2px] sm:block"
          style={{ background: "var(--accent)" }}
        />
      )}
    </Link>
  );

  return (
    <header className="border-b rule">
      <nav
        aria-label="primary"
        className="mx-auto flex max-w-[76rem] flex-wrap items-center gap-x-7 gap-y-2 px-5 py-3 sm:px-8"
      >
        <Link
          href="/"
          className="flex items-center gap-2 text-[17px]"
          style={{
            fontFamily: "var(--font-display)",
            fontWeight: 700,
            fontVariationSettings: '"wdth" 85',
          }}
        >
          <CoreGlyph />
          Stratum
        </Link>

        {database && (
          <div className="flex items-center gap-2">
            <label htmlFor="database" className="sr-only">
              Database
            </label>
            <select
              id="database"
              value={database}
              onChange={(event) =>
                router.push(`/db/${event.target.value}/${section ?? "ask"}`)
              }
              className="border rule bg-[var(--surface)] px-2 py-1 font-mono text-[13px]"
            >
              {!connections.some((c) => c.id === database) && (
                <option value={database}>{database}</option>
              )}
              {connections.map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.id}
                </option>
              ))}
            </select>
          </div>
        )}

        {database && (
          <div className="flex items-center gap-5">
            {SECTIONS.map(({ slug, label }) =>
              link(`/db/${database}/${slug}`, label, section === slug),
            )}
          </div>
        )}

        <div className="ml-auto flex items-center gap-5">
          {link("/lab", "Benchmark", pathname.startsWith("/lab"))}
          {link("/connect", "Connect", pathname.startsWith("/connect"))}
        </div>
      </nav>
    </header>
  );
}

/** A core sample in miniature: the mark is the thing the product does. */
export function CoreGlyph({ size = 18 }: { size?: number }) {
  return (
    <svg width={size * 0.55} height={size} viewBox="0 0 11 20" aria-hidden>
      <rect x="0" y="0" width="11" height="20" rx="1.5" fill="var(--color-slate)" />
      <rect x="0" y="3" width="11" height="3" fill="#8a6021" />
      <rect x="0" y="8" width="11" height="2" fill="#d9573b" />
      <rect x="0" y="12" width="11" height="5" fill="#ffb23f" />
    </svg>
  );
}
