"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Brand } from "@/components/Brand";
import { api, type Connection } from "@/lib/api";
import { ASK, MAP, OWN } from "@/lib/routes";

/**
 * The one piece of chrome every page shares.
 *
 * Outside a database it is a short list of ways in: the map, a question, how it works,
 * the benchmark, and your own database. Inside a database the sections read left to
 * right in the order you would use them: ask, see what was excavated, map it, review it.
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
      key={label}
      href={href}
      aria-current={current ? "page" : undefined}
      className="relative py-1 text-[14px] transition-colors hover:text-[var(--fg)]"
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
    <header data-site-header className="relative z-20 border-b rule">
      <nav
        aria-label="primary"
        className="mx-auto flex max-w-[76rem] flex-wrap items-center gap-x-7 gap-y-2 px-5 py-3 sm:px-8"
      >
        <Link href="/" data-brand aria-label="Stratum, home" className="block">
          <Brand />
        </Link>

        {database ? (
          <>
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
            <div className="flex items-center gap-5">
              {SECTIONS.map(({ slug, label }) =>
                link(`/db/${database}/${slug}`, label, section === slug),
              )}
            </div>
          </>
        ) : (
          <div className="flex items-center gap-5">
            {link(MAP, "Atlas", false)}
            {link(ASK, "Ask", false)}
          </div>
        )}

        <div className="ml-auto flex items-center gap-5">
          {link("/how-it-works", "How it works", pathname.startsWith("/how-it-works"))}
          {link("/lab", "Benchmark", pathname.startsWith("/lab"))}
          {link(OWN, "Your database", pathname.startsWith("/schema") || pathname.startsWith("/connect"))}
        </div>
      </nav>
    </header>
  );
}
