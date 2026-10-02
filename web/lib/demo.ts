/**
 * The public demo: the same app, with recorded engine output in place of an engine.
 *
 * `npm run build:demo` builds a static site with `NEXT_PUBLIC_DEMO=1`. Every read the
 * pages make is served from `public/demo/`, recorded from a real engine by
 * `scripts/record_demo.py`. Excavations and answers were recorded event by event with
 * the time each arrived, and are replayed at that pace, so the trace fills, the
 * layers settle and the needle lands exactly as they do against a live engine.
 *
 * What cannot be recorded is what needs an engine on the spot: a question nobody
 * asked yet, or a database nobody has connected. Those say so plainly.
 */

export const DEMO = process.env.NEXT_PUBLIC_DEMO === "1";

export const ASK_LOCALLY =
  "This public demo replays answers Stratum gave to the questions listed on this page. " +
  "Asking something new needs the engine and a language model, so run Stratum locally " +
  "to ask anything you like.";

export const CONNECT_LOCALLY =
  "Connecting your own database needs the Stratum engine, which this public demo does not run.";

type Recorded = { event: string; data: unknown; at: number };

type Handlers = {
  onEvent?: (event: string, data: unknown) => void;
  onDone?: (data: unknown) => void;
  onError?: (message: string) => void;
};

// A gap longer than this was the recording machine waiting on a rate limit, not the
// pipeline working; replaying it would only look broken.
const LONGEST_PAUSE_MS = 4000;

function file(path: string): string {
  return `/demo${path.split("?")[0].replace(/\/+$/, "")}.json`;
}

export async function demoJson<T>(path: string): Promise<T> {
  const response = await fetch(file(path));
  if (!response.ok) throw new Error(`${path} is not part of the public demo.`);
  return response.json() as Promise<T>;
}

/** Writes the demo can honour without an engine. Nothing is persisted. */
export async function demoWrite<T>(path: string, body: unknown): Promise<T> {
  if (/\/model\/items\//.test(path)) {
    // A review decision: accepted on screen, kept for this visit only.
    return { ok: true, version: 0 } as T;
  }
  if (path === "/connections") {
    const { url = "", id = "" } = (body ?? {}) as { url?: string; id?: string };
    const wanted = url.startsWith("demo:") ? url.slice(5) : id;
    const connections = await demoJson<{ id: string }[]>("/connections");
    const found = connections.find((c) => c.id === wanted);
    if (found) return found as T;
  }
  throw new Error(CONNECT_LOCALLY);
}

function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

async function replay(events: Recorded[], handlers: Handlers, signal?: AbortSignal) {
  let clock = 0;
  for (const recorded of events) {
    await pause(Math.min(Math.max(0, recorded.at - clock), LONGEST_PAUSE_MS), signal);
    clock = recorded.at;
    if (recorded.event === "done") handlers.onDone?.(recorded.data);
    else if (recorded.event === "error")
      handlers.onError?.((recorded.data as { message?: string }).message ?? "unknown error");
    else handlers.onEvent?.(recorded.event, recorded.data);
  }
}

/** How a question is matched to a recording: case, spacing and the final mark ignored. */
function key(text: string): string {
  return text.toLowerCase().replace(/\s+/g, " ").replace(/[?.!\s]+$/, "").trim();
}

type AskRecording = {
  question: string;
  clarification?: string;
  allow_expensive?: boolean;
  events: Recorded[];
};

export async function demoStream(
  path: string,
  body: unknown,
  handlers: Handlers,
  signal?: AbortSignal,
): Promise<void> {
  const excavation = path.match(/^\/databases\/([^/]+)\/excavate$/);
  if (excavation) {
    const events = await demoJson<Recorded[]>(`/databases/${excavation[1]}/excavate`);
    return replay(events, handlers, signal);
  }

  if (path === "/ask") {
    const request = body as {
      database_id: string;
      question: string;
      clarification?: string;
      allow_expensive?: boolean;
    };
    const recordings = await demoJson<AskRecording[]>(`/databases/${request.database_id}/ask`);
    const match = recordings.find(
      (r) =>
        key(r.question) === key(request.question) &&
        (r.clarification ?? "") === (request.clarification ?? "") &&
        Boolean(r.allow_expensive) === Boolean(request.allow_expensive),
    );
    if (!match) {
      handlers.onError?.(ASK_LOCALLY);
      return;
    }
    return replay(match.events, handlers, signal);
  }

  handlers.onError?.(`${path} is not part of the public demo.`);
}
