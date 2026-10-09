"use client";

import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Lenis from "lenis";
import { usePathname } from "next/navigation";
import { useEffect } from "react";

gsap.registerPlugin(ScrollTrigger);

/**
 * Smooth scrolling on the two long, read-through pages: the landing page and How it
 * works. The app pages have panels that scroll on their own (the review queue, the
 * schema table), and a page-wide smoother would fight them, so they keep the browser's
 * own scrolling. Off entirely for anyone who asks for reduced motion.
 */
const LONG_PAGES = /^\/(how-it-works\/?)?$/;

export function SmoothScroll() {
  const pathname = usePathname();

  useEffect(() => {
    if (!LONG_PAGES.test(pathname)) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const lenis = new Lenis({ duration: 1.1, smoothWheel: true });
    lenis.on("scroll", ScrollTrigger.update);
    const tick = (time: number) => lenis.raf(time * 1000);
    gsap.ticker.add(tick);
    gsap.ticker.lagSmoothing(0);

    return () => {
      gsap.ticker.remove(tick);
      lenis.destroy();
    };
  }, [pathname]);

  return null;
}
