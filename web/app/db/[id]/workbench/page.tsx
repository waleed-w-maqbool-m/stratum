"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PageTitle } from "@/components/PageTitle";
import { editable, headline, KINDS, kindLabel, short } from "@/components/workbench/describe";
import { Finding } from "@/components/workbench/Finding";
import { api, type ReviewItem } from "@/lib/api";

/**
 * The Workbench (PLAN.md 5.3).
 *
 * The review queue, ordered by impact (uncertainty times how much depends on it), so
 * the first thing asked about is the soft-delete rule that changes every revenue
 * answer, not a description on a lookup table.
 *
 * Keyboard-first, because this is a queue somebody works through: J/K to move, A to
 * accept, R to reject, E to edit. Each item leads with its claim in a sentence and the
 * evidence behind it, since "accept" is only a meaningful act if the reason is visible.
 */

type Load = { state: "loading" } | { state: "ready" } | { state: "missing" } | { state: "failed"; message: string };

export default function WorkbenchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [kind, setKind] = useState<string>("all");
  const [cursor, setCursor] = useState(0);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [decided, setDecided] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState("");
  const listRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    setLoad({ state: "loading" });
    api
      .review(id, 500)
      .then((queue) => {
        setItems(queue);
        setLoad({ state: "ready" });
      })
      .catch((exception: Error) =>
        setLoad(
          /run excavation first|404/i.test(exception.message)
            ? { state: "missing" }
            : { state: "failed", message: exception.message },
        ),
      );
  }, [id]);

  const shown = useMemo(
    () => (kind === "all" ? items : items.filter((item) => item.kind === kind)),
    [items, kind],
  );
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const item of items) out[item.kind] = (out[item.kind] ?? 0) + 1;
    return out;
  }, [items]);
  const current = shown[Math.min(cursor, shown.length - 1)];
  const decidedCount = Object.keys(decided).length;

  const move = useCallback(
    (step: number) => {
      setEditing(false);
      setCursor((c) => Math.max(0, Math.min(shown.length - 1, c + step)));
    },
    [shown.length],
  );

  // The next item still waiting, so working the queue never lands on a decided one.
  const advance = useCallback(
    (from: number, justDecided: string) => {
      const waiting = (index: number) =>
        !decided[shown[index].id] && shown[index].id !== justDecided;
      for (let index = from + 1; index < shown.length; index++) if (waiting(index)) return index;
      for (let index = 0; index < from; index++) if (waiting(index)) return index;
      return from;
    },
    [decided, shown],
  );

  const decide = useCallback(
    async (action: "accept" | "reject" | "edit", value?: unknown) => {
      if (!current || busy) return;
      setBusy(true);
      setFailure("");
      try {
        await api.patchItem(id, current.id, action, value);
        if (action === "edit" && value !== undefined) {
          setItems((prior) => prior.map((item) => (item.id === current.id ? { ...item, value } : item)));
        }
        setDecided((prior) => ({ ...prior, [current.id]: action }));
        setEditing(false);
        setCursor(advance(cursor, current.id));
      } catch (exception) {
        setFailure(`That decision was not saved: ${(exception as Error).message}`);
      } finally {
        setBusy(false);
      }
    },
    [advance, busy, current, cursor, id],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (editing || event.metaKey || event.ctrlKey || event.altKey) return;
      if (target.closest("input, textarea, select, [contenteditable]")) return;
      const key = event.key.toLowerCase();
      if (key === "j" || event.key === "ArrowDown") move(1);
      else if (key === "k" || event.key === "ArrowUp") move(-1);
      else if (key === "a") void decide("accept");
      else if (key === "r") void decide("reject");
      else if (key === "e" && current && editable(current)) setEditing(true);
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, decide, editing, move]);

  // Keep the selected row in view as J/K walk past the edge of the list.
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>('[aria-current="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor, kind]);

  return (
    <main className="mx-auto max-w-[76rem] px-5 py-10 sm:px-8">
      <PageTitle
        title="Review what Stratum inferred"
        lede={
          <>
            Everything below was worked out from the data, not declared in the schema. Accepting
            an inference makes it a rule every answer follows; rejecting it takes it out. The
            ones that change the most answers come first.
          </>
        }
        aside={
          load.state === "ready" && items.length > 0 ? (
            <Progress decided={decidedCount} total={items.length} />
          ) : undefined
        }
      />

      {load.state === "loading" && (
        <p role="status" className="text-[15px] text-[var(--fg-muted)]">Loading the review queue…</p>
      )}

      {load.state === "missing" && (
        <p className="text-[15px]">
          {id} has not been excavated yet, so there is nothing to review.{" "}
          <Link href={`/db/${id}/excavate`} className="text-[var(--accent)] underline underline-offset-4">
            Excavate it
          </Link>{" "}
          first.
        </p>
      )}

      {load.state === "failed" && (
        <p className="border-l-2 pl-3 text-[15px]" style={{ borderColor: "var(--danger)" }}>
          The review queue could not be loaded: {load.message}
        </p>
      )}

      {load.state === "ready" && !items.length && (
        <p className="text-[15px]">
          Every inference for {id} has been decided.{" "}
          <Link href={`/db/${id}/ask`} className="text-[var(--accent)] underline underline-offset-4">
            Ask it a question
          </Link>
          .
        </p>
      )}

      {load.state === "ready" && items.length > 0 && (
        <>
          <div role="group" aria-label="Show only" className="mb-6 flex flex-wrap gap-2">
            {[{ kind: "all", label: "All" }, ...KINDS.filter((k) => counts[k.kind])].map((option) => (
              <button
                key={option.kind}
                type="button"
                aria-pressed={kind === option.kind}
                onClick={() => {
                  setKind(option.kind);
                  setCursor(0);
                  setEditing(false);
                }}
                className="border px-3 py-1 text-[13px]"
                style={
                  kind === option.kind
                    ? { background: "var(--fg)", color: "var(--bg)", borderColor: "var(--fg)" }
                    : { borderColor: "var(--rule)", color: "var(--fg-muted)" }
                }
              >
                {option.label}{" "}
                <span className="tabular">{option.kind === "all" ? items.length : counts[option.kind]}</span>
              </button>
            ))}
          </div>

          <div className="grid gap-8 lg:grid-cols-[minmax(0,23rem)_minmax(0,1fr)]">
            <ol
              ref={listRef}
              aria-label="Inferences, most consequential first"
              className="max-h-[34vh] overflow-y-auto border-y rule lg:max-h-[calc(100vh-15rem)]"
            >
              {shown.map((item, index) => {
                const selected = item === current;
                const decision = decided[item.id];
                return (
                  <li key={item.id} className="border-b rule last:border-0">
                    <button
                      type="button"
                      onClick={() => {
                        setCursor(index);
                        setEditing(false);
                      }}
                      aria-current={selected}
                      className="block w-full border-l-2 px-3 py-2.5 text-left"
                      style={{
                        borderColor: selected ? "var(--accent)" : "transparent",
                        background: selected ? "var(--surface)" : undefined,
                      }}
                    >
                      <span className="flex items-baseline justify-between gap-3 text-[12px] text-[var(--fg-muted)]">
                        <span>{kindLabel(item.kind)}</span>
                        {decision && (
                          <span style={{ color: decision === "reject" ? "var(--danger)" : "var(--good)" }}>
                            {decision === "reject" ? "Rejected" : "Accepted"}
                          </span>
                        )}
                      </span>
                      <span
                        className="mt-0.5 line-clamp-2 block text-[14px] leading-snug"
                        style={{ color: decision ? "var(--fg-muted)" : "var(--fg)" }}
                      >
                        {headline(item)}
                      </span>
                      <span className="sr-only">{short(item.target)}</span>
                    </button>
                  </li>
                );
              })}
            </ol>

            <div className="min-w-0 lg:sticky lg:top-6 lg:self-start">
              {failure && (
                <p role="alert" className="mb-4 border-l-2 pl-3 text-[14px]" style={{ borderColor: "var(--danger)" }}>
                  {failure}
                </p>
              )}
              {current && (
                <Finding
                  key={current.id}
                  item={current}
                  decision={decided[current.id]}
                  editing={editing}
                  busy={busy}
                  onDecide={decide}
                  onEdit={() => setEditing(true)}
                  onCancelEdit={() => setEditing(false)}
                />
              )}
              <p className="mt-8 border-t rule pt-3 text-[13px] text-[var(--fg-muted)]">
                Keys: <kbd className="font-mono">J</kbd> and <kbd className="font-mono">K</kbd> move,{" "}
                <kbd className="font-mono">A</kbd> accepts, <kbd className="font-mono">R</kbd> rejects,{" "}
                <kbd className="font-mono">E</kbd> edits names and codes. Every decision saves a new
                version of the model, so a change in the benchmark can be traced back to it.
              </p>
            </div>
          </div>
        </>
      )}
    </main>
  );
}

/** How far through the queue this session is. The bar moves when a decision lands. */
function Progress({ decided, total }: { decided: number; total: number }) {
  return (
    <div className="w-full max-w-[16rem]">
      <p className="text-[14px]">
        <span className="tabular">{decided}</span> of <span className="tabular">{total}</span> decided
      </p>
      <div
        role="progressbar"
        aria-label="Inferences decided"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={decided}
        className="mt-2 h-[4px]"
        style={{ background: "var(--rule)" }}
      >
        <div
          className="h-full transition-[width] duration-[320ms]"
          style={{ width: `${(decided / total) * 100}%`, background: "var(--accent)", transitionTimingFunction: "var(--ease-expo)" }}
        />
      </div>
    </div>
  );
}
