"use client";

import gsap from "gsap";
import { useEffect, useRef, useState } from "react";

import { Brand } from "@/components/Brand";

/**
 * The two-second intro: the logo builds in the middle of the screen, layer by layer
 * like a core sample, then travels to the header while the page fades in around it.
 *
 * It exists to answer "what is this?" with the mark before the page does, and to hand
 * the eye to the top-left, where navigation lives. So it plays once per visit, on the
 * landing page only, and any click or key skips it. A small script in the document head
 * (see layout.tsx) marks the page `data-intro="pending"` before first paint, which is
 * what keeps the page from flashing before the overlay appears; it is never set for
 * visitors who ask for reduced motion, so for them this component does nothing.
 *
 * The logo here is the header's own `Brand`, placed over the header's logo and scaled
 * up, so the dock is a transform back to identity and ends on exactly the same pixels.
 */

const SEEN = "stratum-intro";

export function Intro() {
  const [active, setActive] = useState(false);
  const overlay = useRef<HTMLDivElement>(null);
  const logo = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (document.documentElement.dataset.intro === "pending") setActive(true);
  }, []);

  useEffect(() => {
    if (!active) return;
    const root = document.documentElement;
    const target = document.querySelector<HTMLElement>("[data-brand]");
    const node = logo.current;
    const cover = overlay.current;
    if (!target || !node || !cover) {
      delete root.dataset.intro;
      setActive(false);
      return;
    }
    try {
      sessionStorage.setItem(SEEN, "1");
    } catch {
      /* private mode: the intro may play again, which is harmless */
    }

    const finish = () => {
      delete root.dataset.intro;
      setActive(false);
    };

    let timeline: gsap.core.Timeline | undefined;
    const skip = () => timeline?.timeScale(10);
    window.addEventListener("pointerdown", skip, { once: true });
    window.addEventListener("keydown", skip, { once: true });

    const ctx = gsap.context(() => {
      // Put the copy exactly over the header's logo, then lift it to the middle.
      const box = target.getBoundingClientRect();
      node.style.left = `${box.left}px`;
      node.style.top = `${box.top}px`;
      const fit = Math.min(4.6, (window.innerWidth * 0.7) / box.width);
      const dx = window.innerWidth / 2 - (box.left + box.width / 2);
      const dy = window.innerHeight / 2 - (box.top + box.height / 2);

      timeline = gsap.timeline({ onComplete: finish });
      timeline
        .set(node, { x: dx, y: dy, scale: fit, opacity: 1 })
        .from(node.querySelectorAll("[data-layer]"), {
          scaleY: 0,
          transformOrigin: "50% 100%",
          duration: 0.5,
          stagger: 0.12,
          ease: "power2.out",
        })
        .from(
          node.querySelector("[data-wordmark]"),
          { opacity: 0, y: 12, duration: 0.45, ease: "power3.out" },
          "-=0.3",
        )
        .to({}, { duration: 0.3 })
        // The page fades in while the logo travels; the header's own logo stays hidden
        // until this one has landed on it.
        .add(() => {
          root.dataset.intro = "docking";
          window.dispatchEvent(new Event("stratum:intro-docking"));
        })
        .to(node, { x: 0, y: 0, scale: 1, duration: 0.85, ease: "power3.inOut" }, "<")
        .to(cover, { opacity: 0, duration: 0.5, ease: "power1.out" }, "<0.2");
    });

    return () => {
      window.removeEventListener("pointerdown", skip);
      window.removeEventListener("keydown", skip);
      ctx.revert();
    };
  }, [active]);

  if (!active) return null;
  return (
    <div
      ref={overlay}
      aria-hidden
      className="fixed inset-0 z-[100]"
      style={{ background: "var(--bg)" }}
    >
      <div
        ref={logo}
        className="fixed"
        style={{ opacity: 0, transformOrigin: "50% 50%", willChange: "transform" }}
      >
        <Brand />
      </div>
    </div>
  );
}
