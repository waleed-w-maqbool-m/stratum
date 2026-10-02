"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { PageTitle } from "@/components/PageTitle";
import { api, type Connection } from "@/lib/api";
import { CONNECT_LOCALLY, DEMO } from "@/lib/demo";

/**
 * Connect.
 *
 * A demo first, because that is what a first visit wants: a database that already has
 * the problems Stratum exists for, one click away. Your own database second, with the
 * URL echoed back as fields while you type, so a wrong port is visible before anything
 * is attempted rather than surfacing as a timeout.
 */

type Demo = Awaited<ReturnType<typeof api.demos>>[number];
type Parsed = Record<string, unknown>;

const FIELD_NAMES: Record<string, string> = {
  dialect: "Database",
  host: "Host",
  port: "Port",
  database: "Name",
  username: "User",
  path: "File",
};

export default function ConnectPage() {
  const router = useRouter();
  const [demos, setDemos] = useState<Demo[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [opening, setOpening] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [busy, setBusy] = useState<"test" | "connect" | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string; detail?: string } | null>(null);

  useEffect(() => {
    api.demos().then(setDemos).catch(() => setDemos([]));
    api.connections().then(setConnections).catch(() => setConnections([]));
  }, []);

  useEffect(() => {
    if (DEMO || !url.trim()) {
      setParsed(null);
      return;
    }
    const timer = setTimeout(() => {
      api.testConnection(url).then((r) => setParsed(r.parsed)).catch(() => setParsed(null));
    }, 400);
    return () => clearTimeout(timer);
  }, [url]);

  const open = useCallback(
    async (id: string, target: string) => {
      setOpening(id);
      try {
        const connection = await api.connect(target, id);
        router.push(`/db/${connection.id}/${connection.excavated ? "ask" : "excavate"}`);
      } catch (exception) {
        setResult({ ok: false, text: `Could not open ${id}.`, detail: String(exception) });
        setOpening(null);
      }
    },
    [router],
  );

  const test = useCallback(async () => {
    setBusy("test");
    setResult(null);
    try {
      const response = await api.testConnection(url);
      setResult(
        response.ok
          ? { ok: true, text: "It connects. Stratum can read this database." }
          : { ok: false, text: "It does not connect.", detail: response.error_hint },
      );
    } catch (exception) {
      setResult({ ok: false, text: "It does not connect.", detail: String(exception) });
    } finally {
      setBusy(null);
    }
  }, [url]);

  return (
    <main className="mx-auto max-w-[60rem] px-5 pb-28 pt-10 sm:px-8">
      <PageTitle
        title="Connect a database"
        lede="Stratum only ever reads. Every statement is checked before it reaches the database, and it runs inside a transaction that is rolled back."
      />

      <section aria-labelledby="demos">
        <h2 id="demos" className="text-[19px] font-medium">Try a benchmark database</h2>
        <p className="mt-1 text-[15px] text-[var(--fg-muted)]">
          Both are public sample databases, deliberately made messy. Already excavated, so
          you can start asking straight away.
        </p>
        <ul className="mt-5 grid gap-4 md:grid-cols-2">
          {demos.map((demo) => (
            <li key={demo.id} className="flex flex-col border rule p-5">
              <p className="flex items-baseline justify-between gap-3">
                <span className="display text-[24px] leading-none">{demo.name}</span>
                <span className="text-[13px] text-[var(--fg-muted)]">{demo.engine}</span>
              </p>
              <p className="mt-3 flex-1 text-[14px] leading-relaxed text-[var(--fg-muted)]">
                {demo.description}
              </p>
              {demo.url ? (
                <button
                  type="button"
                  onClick={() => open(demo.id, demo.url!)}
                  disabled={opening !== null}
                  className="mt-5 self-start px-4 py-2 text-[14px] font-medium disabled:opacity-50"
                  style={{ background: "var(--accent)", color: "var(--on-accent)" }}
                >
                  {opening === demo.id ? "Opening…" : `Open ${demo.name}`}
                </button>
              ) : (
                <p className="mt-5 text-[13px] text-[var(--fg-muted)]">
                  Not built on this machine yet. Run{" "}
                  <code className="font-mono text-[var(--fg)]">{demo.build}</code> first.
                </p>
              )}
            </li>
          ))}
        </ul>
      </section>

      {connections.length > 0 && (
        <section aria-labelledby="connected" className="mt-14">
          <h2 id="connected" className="text-[19px] font-medium">Already connected</h2>
          <ul className="mt-4 border-t rule">
            {connections.map((connection) => (
              <li key={connection.id} className="flex flex-wrap items-center justify-between gap-3 border-b rule py-3">
                <span>
                  <span className="font-mono text-[14px]">{connection.id}</span>
                  <span className="ml-3 text-[13px] text-[var(--fg-muted)]">{connection.dialect}</span>
                </span>
                <Link
                  href={`/db/${connection.id}/${connection.excavated ? "ask" : "excavate"}`}
                  className="text-[14px] underline decoration-[var(--rule)] underline-offset-4 hover:text-[var(--accent)]"
                >
                  {connection.excavated ? "Ask a question" : "Excavate it first"}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {DEMO ? (
        <section aria-labelledby="own" className="mt-14">
          <h2 id="own" className="text-[19px] font-medium">Your own database</h2>
          <p className="mt-2 max-w-[40rem] text-[15px] leading-relaxed text-[var(--fg-muted)]">
            {CONNECT_LOCALLY} Run it locally with <code className="font-mono text-[var(--fg)]">make engine</code>{" "}
            and <code className="font-mono text-[var(--fg)]">make web</code>, then paste a connection URL here.
            It works with SQLite and Postgres, and it only ever reads.
          </p>
        </section>
      ) : (
      <section aria-labelledby="own" className="mt-14">
        <h2 id="own" className="text-[19px] font-medium">Your own database</h2>
        <p className="mt-1 text-[15px] text-[var(--fg-muted)]">
          Paste a connection URL. For a SQLite file use{" "}
          <code className="font-mono text-[var(--fg)]">sqlite:///C:/path/to/file.db</code>; it is opened read-only.
        </p>
        <label htmlFor="url" className="sr-only">Connection URL</label>
        <input
          id="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="postgresql://user:password@host:5432/sales"
          spellCheck={false}
          autoComplete="off"
          className="mt-4 w-full border rule bg-[var(--surface)] px-4 py-3 font-mono text-[14px] outline-none focus:border-[var(--accent)]"
        />

        {parsed && (
          <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-[14px] sm:grid-cols-4">
            {Object.entries(parsed)
              .filter(([key]) => key in FIELD_NAMES)
              .map(([key, value]) => (
                <div key={key}>
                  <dt className="text-[13px] text-[var(--fg-muted)]">{FIELD_NAMES[key]}</dt>
                  <dd className="mt-0.5 truncate font-mono">
                    {value === "" || value === null || value === undefined ? (
                      <span className="text-[var(--fg-muted)]">not set</span>
                    ) : (
                      String(value)
                    )}
                  </dd>
                </div>
              ))}
          </dl>
        )}

        <div className="mt-5 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={test}
            disabled={!url.trim() || busy !== null}
            className="border rule px-4 py-2 text-[14px] disabled:opacity-40"
          >
            {busy === "test" ? "Testing…" : "Test connection"}
          </button>
          <button
            type="button"
            onClick={() => {
              setBusy("connect");
              void open(parsed && typeof parsed.database === "string" && parsed.database
                ? String(parsed.database).replace(/\W+/g, "_")
                : "my_database", url).finally(() => setBusy(null));
            }}
            disabled={!url.trim() || busy !== null}
            className="px-4 py-2 text-[14px] font-medium disabled:opacity-40"
            style={{ background: "var(--accent)", color: "var(--on-accent)" }}
          >
            {busy === "connect" ? "Connecting…" : "Connect and excavate"}
          </button>
        </div>

        {result && (
          <div
            role="status"
            className="mt-5 border-l-2 py-1 pl-4 text-[14px]"
            style={{ borderColor: result.ok ? "var(--good)" : "var(--danger)" }}
          >
            <p>{result.text}</p>
            {result.detail && <p className="mt-1 text-[var(--fg-muted)]">{result.detail}</p>}
          </div>
        )}
      </section>
      )}
    </main>
  );
}
