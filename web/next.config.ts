import type { NextConfig } from "next";

// `npm run build:demo` sets this: a fully static site for Cloudflare Pages, reading the
// recorded engine output in public/demo instead of a live engine (lib/demo.ts).
const staticExport = process.env.STATIC_EXPORT === "1";

const config: NextConfig = {
  reactStrictMode: true,
  // The engine's event streams must reach the browser event by event. Compression
  // buffers them until the response ends, which turned the live pipeline trace into
  // one burst at the finish; the engine marks them `no-transform` too.
  compress: false,
  ...(staticExport
    ? {
        output: "export",
        // `/lab/index.html` rather than `/lab.html`: what static hosts serve for `/lab`.
        trailingSlash: true,
        images: { unoptimized: true },
      }
    : {
        // The engine runs separately on :8000. Proxying keeps the browser same-origin,
        // so EventSource works without CORS preflight on the SSE endpoints.
        async rewrites() {
          return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/:path*" }];
        },
      }),
};

export default config;
