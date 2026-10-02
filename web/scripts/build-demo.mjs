// Build the public demo: a static export that reads public/demo instead of an engine.
// Plain Node rather than an env-var prefix, so the same command works on Windows and on
// Cloudflare's Linux builders.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";

if (!existsSync("public/demo/connections.json")) {
  console.error("public/demo is empty: record it first with scripts/record_demo.py");
  process.exit(1);
}

const result = spawnSync("npx", ["next", "build"], {
  stdio: "inherit",
  shell: process.platform === "win32",
  env: { ...process.env, NEXT_PUBLIC_DEMO: "1", STATIC_EXPORT: "1" },
});
process.exit(result.status ?? 1);
