"use client";

import { use, useCallback, useRef, useState } from "react";

import { AnswerView } from "@/components/ask/Answer";
import { Trace } from "@/components/Trace";
import { stream, type Answer, type TraceEvent } from "@/lib/api";
import { DEMO } from "@/lib/demo";

/**
 * Ask.
 *
 * The question, the pipeline filling in as a core sample while it works, then the
 * answer with everything needed to check it. When Stratum cannot answer outright — the
 * question is ambiguous, or the query would scan a table it should not — the page asks
 * for a decision instead of failing, and the decision re-asks with it.
 */

// Kept identical to QUESTIONS in scripts/record_demo.py: the public demo can only
// replay what was recorded, so it never suggests anything else.
const SUGGESTIONS: Record<string, string[]> = {
  chinook_legacy: [
    "What is our total revenue across all invoices?",
    "How many customers do we have?",
    "Which billing country generated the most revenue?",
    "Show total revenue per year.",
    "Who are our best customers?",
    "Which genre sells the most tracks?",
  ],
  pagila_legacy: [
    "What is the total revenue from all payments?",
    "How many rentals have been made in total?",
    "How many films are rated PG-13?",
    "Who are our best customers?",
    "How many customers do we have?",
    "How many events were logged in March 2022?",
  ],
};

type Option = {
  id: string;
  label: string;
  interpretation: string;
  row_count?: number | null;
  is_default?: boolean;
};

type Clarification = { question: string; term: string; options: Option[] };

export default function AskPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  // The reading chosen for the question on screen, so "run it anyway" after a cost
  // warning keeps it instead of being asked to clarify all over again.
  const [chosen, setChosen] = useState("");
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);

  const run = useCallback(
    async (text: string, extra: { clarification?: string; allow_expensive?: boolean } = {}) => {
      if (!text.trim()) return;
      abort.current?.abort();
      abort.current = new AbortController();
      setAsked(text);
      setChosen(extra.clarification ?? "");
      setRunning(true);
      setEvents([]);
      setAnswer(null);
      setError("");

      await stream(
        "/ask",
        { database_id: id, question: text, ...extra },
        {
          onEvent: (name, data) => {
            if (name === "stage.end") setEvents((prior) => [...prior, data as TraceEvent]);
          },
          onDone: (data) => {
            setAnswer(data as Answer);
            setRunning(false);
          },
          onError: (message) => {
            setError(message);
            setRunning(false);
          },
        },
        abort.current.signal,
      ).catch((exception) => {
        if ((exception as Error).name !== "AbortError") setError(String(exception));
        setRunning(false);
      });
    },
    [id],
  );

  const clarification = (answer as (Answer & { clarification?: Clarification }) | null)?.clarification;

  return (
    <main className="mx-auto max-w-[76rem] px-5 pb-28 pt-10 sm:px-8">
      <h1 className="sr-only">Ask {id} a question</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void run(question);
        }}
        className="flex flex-col gap-3 sm:flex-row"
      >
        <label htmlFor="question" className="sr-only">Your question</label>
        <input
          id="question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask about this database in plain words"
          autoComplete="off"
          className="min-w-0 flex-1 border rule bg-[var(--surface)] px-5 py-4 text-[18px] outline-none focus:border-[var(--accent)]"
        />
        <button
          type="submit"
          disabled={running || !question.trim()}
          className="px-7 py-4 text-[16px] font-medium disabled:opacity-40"
          style={{ background: "var(--accent)", color: "var(--on-accent)" }}
        >
          {running ? "Digging…" : "Ask"}
        </button>
      </form>

      {!answer && !running && !error && (
        <section aria-labelledby="try" className="mt-10 max-w-[44rem]">
          <h2 id="try" className="text-[15px] text-[var(--fg-muted)]">
            Some questions this database can answer
          </h2>
          <ul className="mt-3 border-t rule">
            {(SUGGESTIONS[id] ?? []).map((suggestion) => (
              <li key={suggestion} className="border-b rule">
                <button
                  type="button"
                  onClick={() => {
                    setQuestion(suggestion);
                    void run(suggestion);
                  }}
                  className="w-full py-3 text-left text-[16px] transition-colors hover:text-[var(--accent)]"
                >
                  {suggestion}
                </button>
              </li>
            ))}
          </ul>
          <p className="mt-4 text-[14px] leading-relaxed text-[var(--fg-muted)]">
            Each answer shows the SQL it ran and the rules it applied, so you can check it
            rather than take it on trust.{" "}
            {DEMO
              ? "This public demo replays the answers Stratum gave to these questions, at the speed it gave them; run it locally to ask your own."
              : "Answering uses a language model and takes a few seconds."}
          </p>
        </section>
      )}

      {(running || answer || error) && (
        <div className="mt-10 grid gap-10 lg:grid-cols-[15rem_minmax(0,1fr)]">
          <aside aria-label="How the answer was worked out">
            <p className="text-[14px] text-[var(--fg-muted)]">
              {running ? "Working through the pipeline" : "How it was worked out"}
            </p>
            <div className="mt-3">
              <Trace events={events} />
            </div>
          </aside>

          <section aria-live="polite" aria-busy={running} className="min-w-0">
            {asked && <p className="mb-6 text-[20px] leading-snug">{asked}</p>}
            {running && !events.length && (
              <p className="text-[15px] text-[var(--fg-muted)]">Reading the question…</p>
            )}

            {error && (
              <div className="border-l-2 py-1 pl-4" style={{ borderColor: "var(--danger)" }}>
                <p className="text-[16px]">Stratum could not answer that.</p>
                <p className="mt-1 text-[14px] text-[var(--fg-muted)]">{error}</p>
              </div>
            )}

            {answer?.status === "clarify" && clarification && (
              <div>
                <p className="text-[18px]">{answer.message || clarification.question}</p>
                <ul className="mt-4 grid gap-2">
                  {clarification.options.map((option) => (
                    <li key={option.id}>
                      <button
                        type="button"
                        onClick={() => run(asked, { clarification: option.interpretation })}
                        className="w-full border rule px-4 py-3 text-left transition-colors hover:border-[var(--accent)]"
                      >
                        <span className="block text-[16px]">
                          {option.label}
                          {option.is_default && (
                            <span className="ml-2 text-[13px] text-[var(--fg-muted)]">most likely</span>
                          )}
                        </span>
                        <span className="mt-0.5 block text-[14px] text-[var(--fg-muted)]">
                          {option.interpretation}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {answer?.status === "cost_guard" && (
              <div>
                <p className="text-[18px]">This question would scan more of the database than it should.</p>
                <p className="mt-2 text-[15px] text-[var(--fg-muted)]">{answer.cost_warning?.reason ?? answer.message}</p>
                {answer.cost_warning?.cheaper_variant && (
                  <>
                    <p className="mt-5 text-[15px]">{answer.cost_warning.cheaper_explanation}</p>
                    <pre className="mt-2 overflow-auto whitespace-pre-wrap break-words border rule bg-[var(--surface)] p-4 text-[13px]">
                      <code className="font-mono">{answer.cost_warning.cheaper_variant}</code>
                    </pre>
                  </>
                )}
                <button
                  type="button"
                  onClick={() =>
                    run(asked, { allow_expensive: true, ...(chosen ? { clarification: chosen } : {}) })
                  }
                  className="mt-5 border rule px-4 py-2 text-[14px]"
                >
                  Run the full query anyway
                </button>
              </div>
            )}

            {(answer?.status === "abstain" || answer?.status === "error") && (
              <div className="border-l-2 py-1 pl-4" style={{ borderColor: "var(--danger)" }}>
                <p className="text-[18px]">
                  {answer.status === "abstain" ? "Stratum decided not to answer." : "Something went wrong."}
                </p>
                <p className="mt-2 text-[15px] text-[var(--fg-muted)]">{answer.message}</p>
              </div>
            )}

            {answer?.status === "ok" && <AnswerView answer={answer} />}
          </section>
        </div>
      )}
    </main>
  );
}
