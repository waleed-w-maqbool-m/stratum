import type { Metadata } from "next";
import { Bricolage_Grotesque, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";

import { SiteHeader } from "@/components/SiteHeader";

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

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body className="min-h-screen antialiased">
        <a
          href="#content"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-3 focus:z-50 focus:bg-[var(--accent)] focus:px-3 focus:py-2 focus:text-[var(--on-accent)]"
        >
          Skip to content
        </a>
        <SiteHeader />
        <div id="content">{children}</div>
      </body>
    </html>
  );
}
