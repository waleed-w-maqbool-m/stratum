"use client";

import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { useRef } from "react";

gsap.registerPlugin(useGSAP);

/**
 * One question, drilled through the layers of a legacy database.
 *
 * Every figure here was measured on the Pagila benchmark: each is the total a
 * reasonable-looking query returns when it misses one of the database's unwritten
 * rules. They are in the order a person discovers the rules — and the last layer is
 * what the excavated model returns, which matches the clean database to the cent.
 *
 * The animation is the argument: the drill passes through each wrong answer, the
 * answer is struck out, and the next layer down is closer. Under reduced motion the
 * whole core is simply present.
 */

type Layer = {
  value: string;
  what: string;
  why: string;
  verdict: "wrong" | "close" | "right";
  band: string;
};

const LAYERS: Layer[] = [
  {
    value: "$24,430.27",
    what: "Sum the amount column",
    why: "Money moved to a cents column in April 2022. Everything after is missing.",
    verdict: "wrong",
    band: "#5c3c14",
  },
  {
    value: "$42,989.64",
    what: "Believe the column comment",
    why: "It says the amount is in cents. It is in dollars.",
    verdict: "wrong",
    band: "#8a6021",
  },
  {
    value: "$67,828.52",
    what: "Add both columns up",
    why: "The table also holds refunds and late fees. 0.6% off, and nobody would notice.",
    verdict: "close",
    band: "#d9573b",
  },
  {
    value: "$67,416.51",
    what: "Ask Stratum",
    why: "The two rules were found in the data, so the total matches the clean database to the cent.",
    verdict: "right",
    band: "#ffb23f",
  },
];

export function RevenueCore() {
  const root = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduce) return;

      const rows = gsap.utils.toArray<HTMLElement>("[data-layer]");
      const timeline = gsap.timeline({ defaults: { ease: "expo.out" }, delay: 0.25 });

      timeline.from("[data-drill]", { scaleY: 0, duration: 1.6, ease: "power2.inOut" }, 0);
      rows.forEach((row, index) => {
        const at = 0.2 + index * 0.42;
        timeline
          .from(row.querySelector("[data-band]"), { scaleX: 0, duration: 0.6 }, at)
          .from(
            row.querySelectorAll("[data-text]"),
            { opacity: 0, y: 6, duration: 0.6, stagger: 0.06 },
            at + 0.05,
          );
        const strike = row.querySelector("[data-strike]");
        if (strike) {
          timeline.from(strike, { scaleX: 0, duration: 0.32, ease: "power2.in" }, at + 0.5);
        }
      });
    },
    { scope: root },
  );

  return (
    <figure ref={root} aria-labelledby="core-question" className="relative">
      <figcaption id="core-question" className="mb-5">
        <span className="block text-[13px] text-[var(--fg-muted)]">
          One question, four answers, one database
        </span>
        <span className="mt-1 block text-[19px]">“What was our total revenue?”</span>
      </figcaption>

      <div className="relative grid grid-cols-[14px_1fr] gap-x-5">
        {/* The core itself: a column of sediment the answers are read out of. */}
        <div
          aria-hidden
          data-drill
          className="origin-top rounded-[3px]"
          style={{
            background: `linear-gradient(to bottom, ${LAYERS.map(
              (layer, index) =>
                `${layer.band} ${(index / LAYERS.length) * 100}% ${((index + 1) / LAYERS.length) * 100}%`,
            ).join(", ")})`,
          }}
        />

        <ol className="divide-y divide-[var(--rule)]">
          {LAYERS.map((layer) => (
            <li key={layer.value} data-layer className="relative py-4 first:pt-0 last:pb-0">
              <span
                aria-hidden
                data-band
                className="absolute -left-5 top-1/2 h-px w-3 origin-left"
                style={{ background: layer.band }}
              />
              <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <span
                  data-text
                  className="tabular relative text-[clamp(1.5rem,2.6vw,2rem)] leading-none"
                  style={{
                    color:
                      layer.verdict === "right"
                        ? "var(--accent)"
                        : "var(--fg-muted)",
                    fontWeight: layer.verdict === "right" ? 600 : 400,
                  }}
                >
                  {layer.value}
                  {layer.verdict !== "right" && (
                    <span
                      aria-hidden
                      data-strike
                      className="absolute left-0 top-1/2 h-[2px] w-full origin-left"
                      style={{ background: "var(--danger)" }}
                    />
                  )}
                </span>
                <span data-text className="text-[15px]">
                  {layer.what}
                  <span className="sr-only">
                    {layer.verdict === "right"
                      ? " — the correct total"
                      : layer.verdict === "close"
                        ? " — close, but wrong"
                        : " — wrong"}
                  </span>
                </span>
              </div>
              <p
                data-text
                className="mt-1.5 max-w-[34rem] text-[14px] leading-relaxed text-[var(--fg-muted)]"
              >
                {layer.why}
              </p>
            </li>
          ))}
        </ol>
      </div>
    </figure>
  );
}
