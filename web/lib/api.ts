/**
 * The engine's API, typed.
 *
 * Requests go through Next's rewrite to :8000, which keeps the browser same-origin so
 * EventSource works on the two streaming endpoints without a CORS preflight.
 */

export type TraceEvent = {
  stage: string;
  ms: number;
  tokens: number;
  detail: Record<string, unknown>;
};

export type Assumption = {
  kind: "soft_delete" | "discriminator" | "normalisation" | "drift" | "unit" | "other";
  entity: string;
  text: string;
  expr: string;
  rows_excluded: number;
};

export type ConfidenceSignal = {
  name: string;
  value: number;
  weight: number;
  contribution: number;
  reason: string;
};

export type Confidence = {
  score: number;
  band: "show" | "assumptions" | "abstain";
  verified: boolean;
  reasons: string[];
  signals: ConfidenceSignal[];
};

export type JoinStep = {
  from: string;
  to: string;
  on: string;
  condition: string;
  confidence: number;
};

export type Answer = {
  question: string;
  status: "ok" | "clarify" | "abstain" | "cost_guard" | "error";
  logical_sql: string;
  physical_sql: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  entities: string[];
  assumptions: Assumption[];
  confidence: Confidence | null;
  gate: { action: string; message: string };
  join_plan: { root: string; steps: JoinStep[]; min_confidence: number } | null;
  cost_warning: {
    reason?: string;
    est_rows?: number;
    cheaper_variant?: string;
    cheaper_explanation?: string;
  } | null;
  explanation: string;
  message: string;
  verified_hit: boolean;
  repairs: number;
  tokens: number;
  elapsed_ms: number;
  trace: TraceEvent[];
  run_id?: string;
};

export type Connection = {
  id: string;
  dialect: string;
  profile: string;
  database?: string;
  version?: string;
  excavated: boolean;
  privileges?: { can_write: boolean; username: string; notes: string[] };
};

export type ReviewItem = {
  id: string;
  kind: string;
  target: string;
  value: unknown;
  confidence: number;
  evidence: Record<string, unknown>;
  detector: string;
  impact: number;
  note: string;
};

const BASE = "/api";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => json<{ ok: boolean; servers: Record<string, string> }>("/health"),

  testConnection: (url: string) =>
    json<{ ok: boolean; error_hint?: string; parsed: Record<string, unknown> }>(
      "/connections/test",
      { method: "POST", body: JSON.stringify({ url }) },
    ),

  connect: (url: string, id?: string) =>
    json<Connection>("/connections", {
      method: "POST",
      body: JSON.stringify({ url, id }),
    }),

  connections: () => json<Connection[]>("/connections"),

  demos: () =>
    json<
      {
        id: string;
        name: string;
        engine: string;
        description: string;
        url: string | null;
        build: string;
      }[]
    >("/demos"),

  model: (databaseId: string) =>
    json<Record<string, unknown>>(`/databases/${databaseId}/model`),

  review: (databaseId: string, limit = 100) =>
    json<ReviewItem[]>(`/databases/${databaseId}/review?limit=${limit}`),

  patchItem: (databaseId: string, itemId: string, action: string, value?: unknown) =>
    json<{ ok: boolean; version: number }>(
      `/databases/${databaseId}/model/items/${encodeURIComponent(itemId)}`,
      { method: "PATCH", body: JSON.stringify({ action, value }) },
    ),

  verified: (databaseId: string) =>
    json<{ question: string; logical_sql: string }[]>(
      `/databases/${databaseId}/verified`,
    ),
};

/**
 * Drive an SSE endpoint.
 *
 * `EventSource` cannot POST, so the streaming endpoints are read with `fetch` and the
 * body is parsed as an event stream by hand. That is a little more code than
 * EventSource, and it is what lets the request carry a JSON body.
 */
export async function stream(
  path: string,
  body: unknown,
  handlers: {
    onEvent?: (event: string, data: unknown) => void;
    onDone?: (data: unknown) => void;
    onError?: (message: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    handlers.onError?.(await response.text());
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // The SSE spec allows \r\n, \n or \r as line endings, and sse-starlette sends
    // \r\n. Splitting on "\n\n" alone never matched "\r\n\r\n", so no event was ever
    // parsed and the page waited for an answer that had already arrived.
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");

    // Events are separated by a blank line; a partial event stays in the buffer.
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      let event = "message";
      const dataLines: string[] = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let parsed: unknown;
      try {
        parsed = JSON.parse(dataLines.join("\n"));
      } catch {
        continue;
      }
      if (event === "done") handlers.onDone?.(parsed);
      else if (event === "error")
        handlers.onError?.((parsed as { message?: string }).message ?? "unknown error");
      else handlers.onEvent?.(event, parsed);
    }
  }
}
