import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Your database",
  description: "Paste a schema and see your database as a map, in your browser.",
};

export default function SchemaLayout({ children }: { children: React.ReactNode }) {
  return children;
}
