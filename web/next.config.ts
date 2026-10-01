import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The engine's event streams must reach the browser event by event. Compression
  // buffers them until the response ends, which turned the live pipeline trace into
  // one burst at the finish; the engine marks them `no-transform` too.
  compress: false,
  // The engine runs separately on :8000. Proxying keeps the browser same-origin, so
  // EventSource works without CORS preflight on the SSE endpoints.
  async rewrites() {
    return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/:path*" }];
  },
};

export default config;
