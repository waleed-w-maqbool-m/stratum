"use client";

import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import dynamic from "next/dynamic";
import Link from "next/link";
import { Component, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { prefersReducedMotion, type AtlasData } from "@/lib/atlas";
import { DEMO } from "@/lib/demo";
import { ASK, MAP, OWN } from "@/lib/routes";

import { SCENE_LABELS } from "./labels";

gsap.registerPlugin(ScrollTrigger, useGSAP);

/**
 * The landing page: the Atlas first, then three short beats, then three doors.
 *
 * Everything a visitor needs in the first ten seconds is on the first screen (what it is,
 * and a way to open the map or ask a question). Scrolling is the explanation: the camera
 * moves through Pagila's schema while three one-sentence beats say what is being shown.
 * The detail (results, SQL, guarantees) lives on its own page, one click away.
 *
 * Under reduced motion there is no pinned stage and nothing moves: the same map is drawn
 * once, and the beats are ordinary stacked sections.
 */

const LandingScene = dynamic(() => import("./LandingScene").then((m) => m.LandingScene), {
  ssr: false,
});

/** If the browser cannot make a WebGL context, the page still reads: the scene is
 * decoration, the beats are the content. */
class SceneBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}

const BG = "#0b0c0e";

// ----------------------------------------------------------------------------- beats

function Hero() {
  return (
    <div data-hero>
      <h1 className="sr-only">Ask questions of databases nobody documented.</h1>
      <p
        aria-hidden
        data-hero-part
        data-hero-title
        className="display-hero text-[clamp(2.4rem,7.4vw,6.4rem)] leading-[0.9]"
      >
        {/* Fixed line breaks: the headline widens as the page scrolls, and letting it
            re-wrap while it does would make the lines jump. */}
        <span className="block whitespace-nowrap">Ask questions</span>{" "}
        <span className="block whitespace-nowrap">of databases</span>{" "}
        <span className="block whitespace-nowrap">nobody</span>{" "}
        <span className="block whitespace-nowrap">documented.</span>
      </p>
      <p
        data-hero-part
        className="mt-6 max-w-[27rem] text-[16px] leading-relaxed text-[var(--fg-muted)]"
      >
        Stratum reads a messy database, works out what it means, and answers in SQL you
        can check.
      </p>
      <div data-hero-part className="mt-8 flex flex-wrap gap-3">
        <Link
          href={MAP}
          className="px-5 py-3 text-[15px] font-medium"
          style={{ background: "var(--accent)", color: "var(--on-accent)" }}
        >
          Explore the map
        </Link>
        <Link href={ASK} className="border rule px-5 py-3 text-[15px] font-medium">
          Try a question
        </Link>
      </div>
    </div>
  );
}

function Stat({ children }: { children: ReactNode }) {
  return (
    <p className="display-hero text-[clamp(4.2rem,12vw,9.5rem)] leading-[0.85] text-[var(--accent)]">
      {children}
    </p>
  );
}

function BeatTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="display mt-3 text-[clamp(1.8rem,3.8vw,3.1rem)] leading-[0.98]">{children}</h2>
  );
}

function BeatText({ children }: { children: ReactNode }) {
  return (
    <p className="mt-4 max-w-[26rem] text-[16px] leading-relaxed text-[var(--fg-muted)]">
      {children}
    </p>
  );
}

function LeftoversBeat({ data }: { data: AtlasData | null }) {
  const leftover = data?.summary.excluded ?? 72;
  const total = data?.nodes.length ?? 90;
  return (
    <>
      <Stat>
        {leftover}
        <span className="text-[var(--fg-muted)]"> of {total}</span>
      </Stat>
      <BeatTitle>tables are leftovers.</BeatTitle>
      <BeatText>
        Backups, archives, staging copies and scratch tables. Stratum finds them and sinks
        them, so no answer draws from them.
      </BeatText>
    </>
  );
}

function JoinsBeat({ data }: { data: AtlasData | null }) {
  const joins = data?.edges.length ?? 19;
  return (
    <>
      <Stat>{joins}</Stat>
      <BeatTitle>joins nobody declared.</BeatTitle>
      <BeatText>
        This database has no foreign keys. Stratum tests which columns hold each
        other&rsquo;s values, and draws the joins back in.
      </BeatText>
    </>
  );
}

function AnswerBeat({ withLink = true }: { withLink?: boolean }) {
  const figure = { fontVariantNumeric: "tabular-nums" as const };
  return (
    <>
      <p className="text-[15px] text-[var(--fg-muted)]">&ldquo;What was our total revenue?&rdquo;</p>
      <div className="mt-5">
        <p className="text-[14px] text-[var(--fg-muted)]">
          A plain query adds up the amount columns
        </p>
        <p className="mt-1 flex flex-wrap items-baseline gap-x-3">
          <span
            className="display text-[clamp(1.7rem,3.2vw,2.5rem)] leading-none text-[var(--fg-muted)] line-through decoration-[var(--danger)] decoration-2"
            style={figure}
          >
            $67,828.52
          </span>
          <span className="text-[13px] text-[var(--danger)]">0.6% too high</span>
        </p>
        <p className="mt-1 text-[13px] text-[var(--fg-muted)]">It also counts refunds and late fees.</p>
      </div>
      <div className="mt-6">
        <p className="text-[14px] text-[var(--fg-muted)]">Stratum</p>
        <p
          className="display-hero text-[clamp(2.8rem,6.6vw,5.4rem)] leading-[0.9] text-[var(--accent)]"
          style={figure}
        >
          $67,416.51
        </p>
        <p className="mt-1 text-[13px] text-[var(--fg-muted)]">
          Matches the clean database to the cent.
        </p>
      </div>
      {withLink && (
        <Link
          href="/how-it-works"
          className="mt-6 inline-block text-[14px] underline decoration-[var(--rule)] underline-offset-4 hover:text-[var(--accent)]"
        >
          See how it gets there
        </Link>
      )}
    </>
  );
}

// ------------------------------------------------------------------------------ doors

function Doors() {
  const doors = [
    {
      href: MAP,
      title: "Explore the map",
      text: "Fly around Pagila’s 90 tables and see which ones count.",
    },
    {
      href: ASK,
      title: "Ask a question",
      text: DEMO
        ? "Replay recorded questions on Pagila and Chinook, with every rule shown."
        : "Ask a benchmark database anything, with every rule shown.",
    },
    {
      href: OWN,
      title: "Your database",
      text: DEMO
        ? "Paste a schema and see its map, right in your browser."
        : "Connect a database and watch Stratum excavate it.",
    },
  ];
  return (
    <section aria-labelledby="doors" className="mx-auto max-w-[76rem] px-5 pb-28 pt-24 sm:px-8">
      <h2 id="doors" className="display text-[clamp(2rem,4.4vw,3.4rem)] leading-none">
        Pick a way in.
      </h2>
      <ul className="mt-10 grid grid-cols-1 gap-4 md:grid-cols-3">
        {doors.map((door) => (
          <li key={door.title} data-door>
            <Link
              href={door.href}
              className="group flex h-full flex-col border rule p-6 transition-colors hover:border-[var(--accent)]"
            >
              <span className="display text-[26px] leading-none group-hover:text-[var(--accent)]">
                {door.title}
              </span>
              <span className="mt-3 text-[15px] leading-relaxed text-[var(--fg-muted)]">
                {door.text}
              </span>
            </Link>
          </li>
        ))}
      </ul>
      <p className="mt-8 text-[14px] text-[var(--fg-muted)]">
        Or read{" "}
        <Link href="/how-it-works" className="underline decoration-[var(--rule)] underline-offset-4 hover:text-[var(--fg)]">
          how it works
        </Link>{" "}
        and{" "}
        <Link href="/lab" className="underline decoration-[var(--rule)] underline-offset-4 hover:text-[var(--fg)]">
          the benchmark results
        </Link>
        .
      </p>
    </section>
  );
}

// ------------------------------------------------------------------------------ page

export function Landing() {
  const wrapper = useRef<HTMLElement>(null);
  // The tall block the story scrolls through. The trigger and the on-screen check use
  // this, not the whole page: the doors below it must not stretch the scroll range.
  const story = useRef<HTMLDivElement>(null);
  const progress = useRef(0);
  const labels = useRef<Record<string, HTMLElement | null>>({});
  const [data, setData] = useState<AtlasData | null>(null);
  const [reduced, setReduced] = useState(false);
  const [onScreen, setOnScreen] = useState(true);

  useEffect(() => {
    setReduced(prefersReducedMotion());
    // Recorded atlas data ships with the site, so the landing page never needs an engine.
    fetch("/demo/databases/pagila_legacy/atlas.json")
      .then((response) => (response.ok ? (response.json() as Promise<AtlasData>) : null))
      .then(setData)
      .catch(() => setData(null));
  }, []);

  // Stop drawing frames once the story has scrolled away.
  useEffect(() => {
    const node = story.current;
    if (!node) return;
    const observer = new IntersectionObserver(([entry]) => setOnScreen(entry.isIntersecting), {
      rootMargin: "100px",
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [reduced]);

  useGSAP(
    () => {
      const root = wrapper.current;
      const block = story.current;
      if (!root || !block || reduced) return;

      // The hero's parts rise in once the page is visible: right away, or when the intro
      // starts handing over to it.
      const entrance = gsap.from("[data-hero-part]", {
        autoAlpha: 0,
        y: 36,
        duration: 1,
        stagger: 0.12,
        ease: "expo.out",
        paused: true,
      });
      const play = () => entrance.play();
      if (document.documentElement.dataset.intro) {
        window.addEventListener("stratum:intro-docking", play, { once: true });
      } else {
        play();
      }

      // The story: one timeline, one unit long, scrubbed by scroll. Its progress is also
      // what steers the camera in the scene.
      const beats = gsap.utils.toArray<HTMLElement>("[data-beat]");
      const hero = root.querySelector<HTMLElement>("[data-hero]");
      const title = root.querySelector<HTMLElement>("[data-hero-title]");
      const cue = root.querySelector<HTMLElement>("[data-cue]");
      gsap.set(beats, { autoAlpha: 0, y: 44 });

      const timeline = gsap.timeline({
        defaults: { ease: "none" },
        scrollTrigger: {
          trigger: block,
          start: "top top",
          end: "bottom bottom",
          scrub: 0.4,
          onUpdate: (self) => {
            progress.current = self.progress;
          },
        },
      });
      const show = (index: number, inAt: number, inTo: number, outAt?: number, outTo?: number) => {
        timeline.to(beats[index], { autoAlpha: 1, y: 0, duration: inTo - inAt }, inAt);
        if (outAt !== undefined && outTo !== undefined) {
          timeline.to(beats[index], { autoAlpha: 0, y: -44, duration: outTo - outAt }, outAt);
        }
      };
      if (hero) timeline.to(hero, { autoAlpha: 0, y: -60, duration: 0.06 }, 0.12);
      // The headline stretches wider as the page scrolls away from it.
      if (title) {
        timeline.to(title, { fontVariationSettings: '"wdth" 92, "opsz" 96', duration: 0.18 }, 0);
      }
      if (cue) timeline.to(cue, { autoAlpha: 0, duration: 0.04 }, 0.02);
      show(0, 0.2, 0.26, 0.34, 0.4);
      show(1, 0.42, 0.48, 0.6, 0.66);
      show(2, 0.68, 0.74);
      timeline.set({}, {}, 1);

      gsap.from("[data-door]", {
        opacity: 0,
        y: 30,
        duration: 0.8,
        stagger: 0.12,
        ease: "expo.out",
        scrollTrigger: { trigger: "[data-door]", start: "top 88%", once: true },
      });

      return () => window.removeEventListener("stratum:intro-docking", play);
    },
    { scope: wrapper, dependencies: [reduced] },
  );

  const scene = data && (
    <SceneBoundary>
      <LandingScene
        data={data}
        progress={progress}
        labels={labels}
        animated={!reduced}
        running={onScreen}
      />
    </SceneBoundary>
  );

  const labelLayer = (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
      {Object.entries(SCENE_LABELS).map(([id, text]) => (
        <span
          key={id}
          ref={(node) => {
            labels.current[id] = node;
          }}
          className="absolute left-0 top-0 whitespace-nowrap font-mono text-[12px] opacity-0"
          style={{ color: "#e9e4d8", textShadow: "0 0 8px #0b0c0e, 0 0 3px #0b0c0e" }}
        >
          {text}
        </span>
      ))}
    </div>
  );

  // The scrim keeps text readable over the field: along the left on a wide screen,
  // along the bottom on a narrow one.
  const scrim = (
    <>
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 hidden lg:block"
        style={{ background: `linear-gradient(to right, ${BG}e6 0%, ${BG}8c 38%, transparent 66%)` }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 lg:hidden"
        style={{ background: `linear-gradient(to top, ${BG} 8%, ${BG}cc 38%, transparent 72%)` }}
      />
    </>
  );

  if (reduced) {
    return (
      <main>
        <section className="relative h-svh min-h-[34rem] overflow-hidden" style={{ background: BG }}>
          <div className="absolute inset-0">{scene}</div>
          {labelLayer}
          {scrim}
          <div className="relative mx-auto flex h-full max-w-[76rem] items-end px-5 pb-14 sm:px-8 lg:items-center lg:pb-0">
            <div className="w-full lg:w-[min(46rem,54%)]">
              <Hero />
            </div>
          </div>
        </section>
        <section className="mx-auto max-w-[76rem] space-y-24 px-5 py-24 sm:px-8">
          <div><LeftoversBeat data={data} /></div>
          <div><JoinsBeat data={data} /></div>
          <div><AnswerBeat /></div>
        </section>
        <Doors />
      </main>
    );
  }

  return (
    <main ref={wrapper}>
      <div ref={story} className="relative h-[460svh]">
        <div className="sticky top-0 h-svh w-full overflow-hidden" style={{ background: BG }}>
          <div
            aria-hidden
            className="absolute inset-0 transition-opacity duration-[1600ms]"
            style={{ opacity: data ? 1 : 0 }}
          >
            {scene}
          </div>
          {labelLayer}
          {scrim}

          <div className="relative mx-auto h-full max-w-[76rem] px-5 sm:px-8">
            <div className="relative h-full w-full lg:w-[min(46rem,54%)]">
              <div
                className="absolute inset-x-0 bottom-12 lg:bottom-auto lg:top-1/2 lg:-translate-y-1/2"
              >
                <Hero />
              </div>
              {[
                <LeftoversBeat key="a" data={data} />,
                <JoinsBeat key="b" data={data} />,
                <AnswerBeat key="c" />,
              ].map((content, index) => (
                <div
                  key={index}
                  aria-hidden
                  className="absolute inset-x-0 bottom-12 lg:bottom-auto lg:top-1/2 lg:-translate-y-1/2"
                >
                  <div data-beat className="invisible">
                    {content}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* The panels above are hidden from assistive technology while they take turns on
              screen, so the same three beats are here, in order, for a screen reader. */}
          <div className="sr-only">
            <LeftoversBeat data={data} />
            <JoinsBeat data={data} />
            <AnswerBeat withLink={false} />
          </div>

          <div
            data-cue
            aria-hidden
            className="pointer-events-none absolute bottom-5 left-1/2 hidden -translate-x-1/2 flex-col items-center gap-2 text-[12px] text-[var(--fg-muted)] sm:flex"
          >
            Scroll
            <span className="cue block h-8 w-px" style={{ background: "var(--accent)" }} />
          </div>
        </div>
      </div>
      <Doors />
    </main>
  );
}
