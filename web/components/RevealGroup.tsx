"use client";

import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useRef } from "react";

gsap.registerPlugin(ScrollTrigger, useGSAP);

/**
 * Sections rise into place as they scroll into view, once each.
 *
 * It is how a long page earns its length: each section arrives when you reach it
 * instead of the whole page being there at once. Anything inside with `data-reveal`
 * takes part. Nothing moves for visitors who ask for reduced motion, and the content is
 * in the page either way.
 */
export function RevealGroup({ children }: { children: React.ReactNode }) {
  const root = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      gsap.utils.toArray<HTMLElement>("[data-reveal]").forEach((element) => {
        gsap.from(element, {
          // Opacity, not visibility: a section that has not been scrolled to yet is
          // still in the page for screen readers, find-in-page and keyboard focus.
          opacity: 0,
          y: 36,
          duration: 0.9,
          ease: "expo.out",
          scrollTrigger: { trigger: element, start: "top 88%", once: true },
        });
      });
    },
    { scope: root },
  );

  return <div ref={root}>{children}</div>;
}
