import { readFileSync } from "node:fs";
import path from "node:path";

/**
 * The databases a static build pre-renders pages for: the ones recorded for the public
 * demo. A live build renders any database id on request, so this only matters there.
 */
export function generateStaticParams(): { id: string }[] {
  try {
    const file = path.join(process.cwd(), "public", "demo", "connections.json");
    const connections = JSON.parse(readFileSync(file, "utf8")) as { id: string }[];
    return connections.map(({ id }) => ({ id }));
  } catch {
    return [{ id: "pagila_legacy" }, { id: "chinook_legacy" }];
  }
}

export default function DatabaseLayout({ children }: { children: React.ReactNode }) {
  return children;
}
