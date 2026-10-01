"use client";

import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Lenis from "lenis";
import { useEffect, useRef } from "react";

import { PairedBar } from "@/components/PairedBar";

gsap.registerPlugin(ScrollTrigger, useGSAP);

/**
 * The anti-patterns, deepest first.
 *
 * "Deepest" is measured, not asserted: rows are ordered by how often a model given the
 * whole schema got questions that depend on them right. At the top are the rules a
 * schema simply does not show — a unit that changed in 2023, a country spelled four
 * ways — and next to each, what the same model managed once Stratum had excavated them.
 *
 * Chinook benchmark, gpt-oss-120b for both columns, the same questions each time.
 */

type Blindspot = {
  name: string;
  before: string;
  after: string;
  baseline: number;
  stratum: number;
  n: number;
};

export const BLINDSPOTS: Blindspot[] = [
  {
    name: "Mixed types",
    before: "CUST_NO '0000000042' joined to CUST_ID 42",
    after: "the cast is compiled into the join",
    baseline: 0,
    stratum: 80,
    n: 10,
  },
  {
    name: "Messy strings",
    before: "'US' 'USA' 'U.S.A.' 'usa'",
    after: "one country",
    baseline: 0,
    stratum: 37.5,
    n: 8,
  },
  {
    name: "Schema drift",
    before: "TOT_AMT in dollars, TOT_AMT_C in cents after 2023",
    after: "one total_amount, right on both sides of the cutover",
    baseline: 7.1,
    stratum: 64.3,
    n: 14,
  },
  {
    name: "A god table",
    before: "TRANS_MSTR: 53 columns, mostly empty",
    after: "orders, payments and refunds as separate things",
    baseline: 15.8,
    stratum: 63.2,
    n: 19,
  },
  {
    name: "Vague questions",
    before: "“Who are our top customers?”",
    after: "asks which ranking you mean",
    baseline: 20,
    stratum: 60,
    n: 5,
  },
  {
    name: "Formats inside columns",
    before: "<a><c>Berlin</c><cy>DE</cy></a>",
    after: "city and country as columns",
    baseline: 20,
    stratum: 20,
    n: 5,
  },
  {
    name: "Rows marked deleted",
    before: "DEL_FLG='Y'  STS=9  DLT_DT set  ZZ_ prefix",
    after: "left out by default, and the answer says so",
    baseline: 23.3,
    stratum: 63.3,
    n: 30,
  },
  {
    name: "Missing foreign keys",
    before: "0 declared constraints",
    after: "16 joins recovered from the data",
    baseline: 31.6,
    stratum: 57.9,
    n: 19,
  },
  {
    name: "Cryptic names",
    before: "CFNM1  tblPrd  intMs",
    after: "first_name  track  milliseconds",
    baseline: 33.3,
    stratum: 52.4,
    n: 21,
  },
  {
    name: "Keys spread over columns",
    before: "order numbers restart in every store",
    after: "joined on (store, order, line)",
    baseline: 57.1,
    stratum: 71.4,
    n: 7,
  },
];

export function Blindspots() {
  const root = useRef<HTMLDivElement>(null);

  // Smooth scroll on the landing page only, and only when motion is welcome.
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const lenis = new Lenis({ duration: 1.05, smoothWheel: true });
    let frame = 0;
    const raf = (time: number) => {
      lenis.raf(time);
      frame = requestAnimationFrame(raf);
    };
    frame = requestAnimationFrame(raf);
    lenis.on("scroll", ScrollTrigger.update);
    return () => {
      cancelAnimationFrame(frame);
      lenis.destroy();
    };
  }, []);

  useGSAP(
    () => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      gsap.utils.toArray<HTMLElement>("[data-row]").forEach((row) => {
        gsap.from(row.querySelectorAll("[data-bar]"), {
          scaleX: 0,
          duration: 0.9,
          ease: "expo.out",
          stagger: 0.12,
          scrollTrigger: { trigger: row, start: "top 85%", once: true },
        });
      });
    },
    { scope: root },
  );

  return (
    <div ref={root}>
      <ol>
        {BLINDSPOTS.map((spot) => (
          <li
            key={spot.name}
            data-row
            className="grid gap-x-10 gap-y-3 border-t rule py-6 md:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]"
          >
            <div>
              <h3 className="text-[17px] font-medium">{spot.name}</h3>
              <p className="mt-2 font-mono text-[13px] text-[var(--danger)]">{spot.before}</p>
              <p className="mt-0.5 font-mono text-[13px] text-[var(--good)]">{spot.after}</p>
            </div>
            <div className="self-center">
              <PairedBar
                baseline={{ value: spot.baseline / 100 }}
                stratum={{ value: spot.stratum / 100 }}
              />
              <p className="mt-1.5 text-right text-[12px] text-[var(--fg-muted)]">
                {spot.n} questions
              </p>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
