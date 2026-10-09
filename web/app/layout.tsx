import type { Metadata } from "next";
import { Bricolage_Grotesque, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";

import { Intro } from "@/components/Intro";
import { SiteHeader } from "@/components/SiteHeader";
import { SmoothScroll } from "@/components/SmoothScroll";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Stratum", template: "%s — Stratum" },
  description:
    "Ask questions of databases nobody documented. Stratum excavates the schema, " +
    "compiles a reviewable semantic model, and answers in SQL you can check.",
};

/**
 * Plex is a quiet nod to the mainframe era these databases come from; Bricolage
 * Grotesque carries the display type, with its width and optical-size axes kept so a
 * headline can be set narrow and tall like a stencilled core-sample label. Self-hosted
 * through next/font, so there is no layout shift while they load.
 */
const display = Bricolage_Grotesque({
  subsets: ["latin"],
  axes: ["wdth", "opsz"],
  variable: "--font-bricolage",
  display: "swap",
});
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

/**
 * Runs before first paint. On the landing page, on a first visit, for anyone who has not
 * asked for reduced motion, it marks the page `data-intro="pending"`, which the CSS uses
 * to keep the page hidden until the intro (components/Intro.tsx) takes over. The timer
 * is a safety net: if the script that plays the intro never runs, the page still shows.
 */
const INTRO_GATE = `(function(){try{var d=document.documentElement;if(location.pathname==='/'&&!sessionStorage.getItem('stratum-intro')&&!matchMedia('(prefers-reduced-motion: reduce)').matches){d.setAttribute('data-intro','pending');setTimeout(function(){d.removeAttribute('data-intro')},7000)}}catch(e){}})()`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${display.variable} ${sans.variable} ${mono.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: INTRO_GATE }} />
      </head>
      <body className="min-h-screen antialiased">
        <a
          href="#content"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-3 focus:z-50 focus:bg-[var(--accent)] focus:px-3 focus:py-2 focus:text-[var(--on-accent)]"
        >
          Skip to content
        </a>
        <SiteHeader />
        <div id="content">{children}</div>
        <SmoothScroll />
        <Intro />
      </body>
    </html>
  );
}
