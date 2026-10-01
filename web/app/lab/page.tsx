"use client";

import { useEffect, useMemo, useState } from "react";

import { PageTitle } from "@/components/PageTitle";
import { PairedBar } from "@/components/PairedBar";
import { QuestionList } from "@/components/lab/QuestionList";
import { ReliabilityChart } from "@/components/lab/ReliabilityChart";
import {
  evalApi,
  modelOf,
  pair,
  pct,
  SUITE_NAMES,
  TAG_NAMES,
  type QuestionRow,
  type Recall,
  type Report,
} from "@/lib/eval";

/**
 * The benchmark.
 *
 * Baseline and Stratum are compared on exactly the questions both answered, per
 * database, so a baseline stopped by a daily token limit is never scored against a
 * different exam. Every question opens into the gold answer next to Stratum's.
 *
 * All of it is read from files the gauntlet publishes after each run; nothing on this
 * page is computed by the engine being graded.
 */

const RECALL_ROWS: { key: string; label: string; format: (v: Recall[string]) => string }[] = [
  { key: "joins", label: "Joins nobody declared", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "keys", label: "Keys", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "soft_deletes", label: "Rows marked deleted", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "god_tables", label: "Tables holding several things", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "drift", label: "Columns that changed meaning", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "embedded_formats", label: "Formats inside columns", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "dead_tables", label: "Tables to ignore", format: (v) => `F1 ${v.f1?.toFixed(2)}` },
  { key: "table_class_exact", label: "Tables classified", format: (v) => `${v.agree} of ${v.total}` },
  { key: "composite_keys", label: "Keys spread over columns", format: (v) => `${v.found} of ${v.total}` },
  { key: "enums", label: "Coded values decoded", format: (v) => `${v.decoded} of ${v.total}` },
];

function Section({ title, intro, children }: {
  title: string; intro?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="mt-16">
      <h2 className="display text-[clamp(1.5rem,2.6vw,1.9rem)] leading-none">{title}</h2>
      {intro && (
        <p className="mt-3 max-w-[40rem] text-[15px] leading-relaxed text-[var(--fg-muted)]">{intro}</p>
      )}
      <div className="mt-7">{children}</div>
    </section>
  );
}

export default function BenchmarkPage() {
  const [reports, setReports] = useState<Report[]>([]);
  const [suite, setSuite] = useState<string>("");
  const [questions, setQuestions] = useState<{ full: QuestionRow[]; naive: QuestionRow[] }>({
    full: [], naive: [],
  });
  const [recall, setRecall] = useState<Recall | null>(null);
  const [loadingQuestions, setLoadingQuestions] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    evalApi
      .reports()
      .then((data) => {
        setReports(data);
        setSuite((current) => current || data[0]?.suite || "");
      })
      .catch((exception) => setError(String(exception)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!suite) return;
    const load = (tag: string) =>
      reports.some((r) => r.suite === suite && r.tag === tag && r.has_questions)
        ? evalApi.questions(suite, tag).catch(() => [] as QuestionRow[])
        : Promise.resolve([] as QuestionRow[]);
    setLoadingQuestions(true);
    Promise.all([load("full"), load("naive")])
      .then(([full, naive]) => setQuestions({ full, naive }))
      .finally(() => setLoadingQuestions(false));
    evalApi.recall(suite).then(setRecall).catch(() => setRecall(null));
  }, [suite, reports]);

  const suites = useMemo(() => Array.from(new Set(reports.map((r) => r.suite))), [reports]);
  const full = reports.find((r) => r.suite === suite && r.tag === "full");
  const naive = reports.find((r) => r.suite === suite && r.tag === "naive");
  const paired = useMemo(
    () => (questions.full.length && questions.naive.length ? pair(questions.naive, questions.full) : null),
    [questions],
  );
  const meta = SUITE_NAMES[suite] ?? { name: suite, engine: "" };

  return (
    <main className="mx-auto max-w-[76rem] px-5 pb-28 pt-10 sm:px-8">
      <PageTitle
        title="Benchmark"
        lede="Two public databases, made messy on purpose, with an answer key the engine never sees. Correct answers come from the clean copy; Stratum only ever queries the messy one."
      />

      {loading && <p className="text-[15px] text-[var(--fg-muted)]">Loading results…</p>}
      {!loading && !reports.length && (
        <div className="max-w-[38rem]">
          <p className="text-[17px]">No benchmark has been run yet.</p>
          <p className="mt-2 text-[15px] text-[var(--fg-muted)]">
            Run the suite from the repository and its results appear here:
          </p>
          <pre className="mt-4 border rule bg-[var(--surface)] p-4 text-[13px]">
            <code className="font-mono">{`make eval-naive SUITE=chinook_v1 PROVIDERS=groq\nmake eval       SUITE=chinook_v1 PROVIDERS=groq`}</code>
          </pre>
          {error && <p className="mt-3 text-[13px] text-[var(--danger)]">The engine said: {error}</p>}
        </div>
      )}

      {suites.length > 0 && (
        <>
          <div role="tablist" aria-label="Benchmark database" className="flex flex-wrap gap-2">
            {suites.map((name) => {
              const selected = name === suite;
              return (
                <button
                  key={name}
                  role="tab"
                  aria-selected={selected}
                  onClick={() => setSuite(name)}
                  className="border rule px-4 py-2 text-left"
                  style={selected ? { background: "var(--fg)", color: "var(--bg)" } : undefined}
                >
                  <span className="block text-[15px] font-medium">{SUITE_NAMES[name]?.name ?? name}</span>
                  <span className="block text-[12px]" style={{ opacity: 0.75 }}>
                    {SUITE_NAMES[name]?.engine}
                  </span>
                </button>
              );
            })}
          </div>

          <Section
            title={`${meta.name}: baseline against Stratum`}
            intro={
              paired ? (
                <>
                  On the {paired.n} questions both answered, with {modelOf(full) || "the same model"} for
                  both. The baseline is given the whole schema in one prompt.
                  {naive?.partial && (
                    <> Its run stopped after {naive.questions_answered} questions when the free tier’s
                    daily budget ran out, so only those are compared.</>
                  )}
                </>
              ) : loadingQuestions ? (
                "Loading the questions…"
              ) : (
                "The baseline has not been run for this database yet."
              )
            }
          >
            {paired && (
              <div className="grid gap-10 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
                <div>
                  <PairedBar
                    baseline={{ value: paired.baseline, correct: Math.round(paired.baseline * paired.n), total: paired.n }}
                    stratum={{ value: paired.stratum, correct: Math.round(paired.stratum * paired.n), total: paired.n }}
                  />
                  <dl className="mt-6 grid grid-cols-2 gap-4 border-t rule pt-4 text-[14px]">
                    <div>
                      <dt className="text-[var(--fg-muted)]">Tokens per question</dt>
                      <dd className="tabular mt-1">
                        {Math.round(paired.baselineTokens).toLocaleString()} to{" "}
                        {Math.round(paired.stratumTokens).toLocaleString()}
                      </dd>
                    </div>
                    {full && (
                      <div>
                        <dt className="text-[var(--fg-muted)]">Stratum on all {full.questions_scored}</dt>
                        <dd className="tabular mt-1">{pct(full.execution_accuracy)}</dd>
                      </div>
                    )}
                  </dl>
                </div>

                <div>
                  <p className="mb-4 text-[14px] text-[var(--fg-muted)]">
                    By kind of mess, hardest for the baseline first
                  </p>
                  <ol className="space-y-5">
                    {paired.byTag.map((row) => (
                      <li key={row.tag} className="grid gap-x-6 gap-y-1.5 sm:grid-cols-[11rem_1fr]">
                        <span className="text-[14px]">
                          {TAG_NAMES[row.tag] ?? row.tag}
                          <span className="tabular block text-[12px] text-[var(--fg-muted)]">
                            {row.n} questions
                          </span>
                        </span>
                        <PairedBar
                          baseline={{ value: row.baseline, correct: Math.round(row.baseline * row.n), total: row.n }}
                          stratum={{ value: row.stratum, correct: Math.round(row.stratum * row.n), total: row.n }}
                        />
                      </li>
                    ))}
                  </ol>
                </div>
              </div>
            )}
          </Section>

          {questions.full.length > 0 && (
            <Section
              title="Every question"
              intro="Open a question to see the correct answer next to Stratum’s, the SQL it wrote and ran, and the rules it applied on the way."
            >
              <div className="mb-2 hidden grid-cols-[3.5rem_1fr_5rem_5rem] gap-x-6 text-[12px] text-[var(--fg-muted)] sm:grid">
                <span />
                <span />
                <span className="text-right">Baseline</span>
                <span className="text-right">Stratum</span>
              </div>
              <QuestionList stratum={questions.full} baseline={questions.naive} />
            </Section>
          )}

          <div className="mt-4 grid gap-x-16 lg:grid-cols-2">
            {recall && (
              <Section
                title="What excavation recovered"
                intro="Scored against the answer key by the benchmark, not by the engine. This is before any question is asked: how much of the hidden structure Stratum found on its own."
              >
                <table className="w-full text-[14px]">
                  <caption className="sr-only">Knowledge recovered by excavation</caption>
                  <tbody>
                    {RECALL_ROWS.filter(({ key }) => recall[key]).map(({ key, label, format }) => (
                      <tr key={key} className="border-b rule">
                        <th scope="row" className="py-2 pr-4 text-left font-normal text-[var(--fg-muted)]">{label}</th>
                        <td className="tabular py-2 text-right">{format(recall[key])}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}

            {full?.calibration && (
              <Section
                title="Does its confidence mean anything?"
                intro={
                  <>
                    Calibration error fell from {full.calibration.ece_before.toFixed(3)} to{" "}
                    {full.calibration.ece_after.toFixed(3)} after fitting on {full.calibration.observations} answers
                    {full.calibration.monotonic ? ", and accuracy rises with confidence" : ""}. The fit is on the same
                    answers it is measured on, so the second figure is optimistic.
                  </>
                }
              >
                <ReliabilityChart
                  before={full.calibration.reliability_before}
                  after={full.calibration.reliability_after}
                />
              </Section>
            )}
          </div>
        </>
      )}
    </main>
  );
}
