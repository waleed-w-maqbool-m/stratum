import Link from "next/link";

import { Blindspots } from "@/components/landing/Blindspots";
import { RevenueCore } from "@/components/landing/RevenueCore";

/**
 * The landing page.
 *
 * It makes one claim and shows the evidence for it, in that order: a legacy database
 * hides rules a schema cannot show, so a model reading the schema gets plausible wrong
 * answers — and Stratum finds the rules first. Every number on this page was measured
 * on the benchmark databases; none is illustrative.
 */

const LOGICAL = `SELECT SUM(amount)
FROM payment`;

const PHYSICAL = `SELECT SUM(amount) FROM (
  SELECT CASE WHEN txn_ts >= '2022-04-01'
              THEN amt_c / 100.0
              ELSE amt END AS amount
  FROM fin_txn
  WHERE txn_typ = 'P'
) AS payment`;

function Section({
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
    <section aria-labelledby={id} className="mt-28 md:mt-36">
      <h2 id={id} className="display text-[clamp(1.75rem,3.2vw,2.4rem)] leading-none">
        {title}
      </h2>
      <p className="mt-4 max-w-[38rem] text-[16px] leading-relaxed text-[var(--fg-muted)]">
        {intro}
      </p>
      <div className="mt-10">{children}</div>
    </section>
  );
}

export default function Home() {
  return (
    <main className="mx-auto max-w-[76rem] px-5 pb-32 pt-14 sm:px-8 md:pt-20">
      <section className="grid items-start gap-x-16 gap-y-14 lg:grid-cols-[minmax(0,1fr)_minmax(0,31rem)]">
        <div className="lg:pt-6">
          <h1 className="display text-[clamp(2.9rem,7vw,5.6rem)] leading-[0.9]">
            Ask questions of databases nobody documented.
          </h1>
          <p className="mt-7 max-w-[34rem] text-[18px] leading-relaxed text-[var(--fg-muted)]">
            Thirty years of decisions live in the data, not the schema: a unit that
            changed, rows marked deleted instead of deleted, a table holding three
            things at once. Stratum digs those rules out first, then answers against
            what it found and shows you every assumption.
          </p>
          <div className="mt-10 flex flex-wrap gap-3">
            <Link
              href="/connect"
              className="px-5 py-3 text-[15px] font-medium"
              style={{ background: "var(--accent)", color: "var(--on-accent)" }}
            >
              Try a demo database
            </Link>
            <Link href="/lab" className="border rule px-5 py-3 text-[15px] font-medium">
              See the benchmark results
            </Link>
          </div>
        </div>
        <RevenueCore />
      </section>

      <Section
        id="blindspots"
        title="What a model can’t see in a schema"
        intro={
          <>
            Each row is a kind of legacy mess, ordered by how often a model reading the
            whole schema answered questions about it correctly. The top rows are the
            rules nobody wrote down, and the second bar is the same model after Stratum
            excavated them.
          </>
        }
      >
        <Blindspots />
      </Section>

      <Section
        id="measured"
        title="Measured on two databases"
        intro={
          <>
            Two public databases were deliberately made messy, with an answer key that
            records every change. The baseline gives a model the whole schema and one
            prompt; Stratum gets the same model and the same questions.
          </>
        }
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[34rem] text-left">
            <caption className="sr-only">
              Benchmark results for the baseline and Stratum on two databases
            </caption>
            <thead>
              <tr className="border-b rule text-[13px] text-[var(--fg-muted)]">
                <th scope="col" className="py-3 pr-6 font-normal">Database</th>
                <th scope="col" className="py-3 pr-6 text-right font-normal">Baseline</th>
                <th scope="col" className="py-3 pr-6 text-right font-normal">Stratum</th>
                <th scope="col" className="py-3 text-right font-normal">Tokens per question</th>
              </tr>
            </thead>
            <tbody className="text-[16px]">
              <tr className="border-b rule">
                <th scope="row" className="py-4 pr-6 font-normal">
                  Chinook
                  <span className="block text-[13px] text-[var(--fg-muted)]">
                    SQLite, 46 questions, gpt-oss-120b
                  </span>
                </th>
                <td className="tabular py-4 pr-6 text-right text-[var(--fg-muted)]">23.9%</td>
                <td className="tabular py-4 pr-6 text-right text-[22px] text-[var(--accent)]">60.9%</td>
                <td className="tabular py-4 text-right">5,817 to 3,095</td>
              </tr>
              <tr className="border-b rule">
                <th scope="row" className="py-4 pr-6 font-normal">
                  Pagila
                  <span className="block text-[13px] text-[var(--fg-muted)]">
                    Postgres 16, 26 paired questions, qwen3.8-27b
                  </span>
                </th>
                <td className="tabular py-4 pr-6 text-right text-[var(--fg-muted)]">30.8%</td>
                <td className="tabular py-4 pr-6 text-right text-[22px] text-[var(--accent)]">57.7%</td>
                <td className="tabular py-4 text-right">3,711 to 2,354</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="mt-5 max-w-[40rem] text-[14px] leading-relaxed text-[var(--fg-muted)]">
          Pagila’s baseline covers 26 of its 45 questions, because the free tier’s daily
          token budget ran out; on all 45, Stratum scored 57.8%.{" "}
          <Link href="/lab" className="underline decoration-[var(--rule)] underline-offset-4 hover:text-[var(--fg)]">
            Every question, gold answer and failure is in the benchmark view.
          </Link>
        </p>
      </Section>

      <Section
        id="how"
        title="The model writes clean SQL. A compiler writes the real query."
        intro={
          <>
            The language model only ever sees a tidy logical schema. The rules Stratum
            excavated are applied by a deterministic compiler, reviewed once, instead of
            being re-guessed by a model on every question.
          </>
        }
      >
        <div className="grid items-start gap-6 md:grid-cols-[minmax(0,0.7fr)_minmax(0,1fr)]">
          <figure>
            <figcaption className="mb-2 text-[13px] text-[var(--fg-muted)]">
              What the model writes
            </figcaption>
            <pre className="overflow-x-auto border rule bg-[var(--surface)] p-5 text-[14px] leading-relaxed">
              <code className="font-mono">{LOGICAL}</code>
            </pre>
          </figure>
          <figure>
            <figcaption className="mb-2 text-[13px] text-[var(--fg-muted)]">
              What runs on the legacy database
            </figcaption>
            <pre className="overflow-x-auto border rule bg-[var(--surface)] p-5 text-[14px] leading-relaxed">
              <code className="font-mono">{PHYSICAL}</code>
            </pre>
          </figure>
        </div>
      </Section>

      <Section
        id="guarantees"
        title="What it will never do"
        intro="Pointing a tool at a production database takes trust. These are enforced in code, not promised in a policy."
      >
        <dl className="grid gap-x-12 gap-y-8 md:grid-cols-3">
          {[
            [
              "Change your data",
              "Every statement is parsed first. Anything but a single SELECT is refused, and even that runs inside a transaction that is rolled back.",
            ],
            [
              "Send your data away",
              "Credentials never reach a language model, and neither do tables. It sees the schema, profiles and at most three sample values per column.",
            ],
            [
              "Hide how it got there",
              "Each answer comes with its SQL, the rules it applied, the joins it used and how confident it is.",
            ],
          ].map(([term, detail]) => (
            <div key={term} className="border-t rule pt-4">
              <dt className="text-[17px] font-medium">{term}</dt>
              <dd className="mt-2 text-[15px] leading-relaxed text-[var(--fg-muted)]">{detail}</dd>
            </div>
          ))}
        </dl>
      </Section>
    </main>
  );
}
