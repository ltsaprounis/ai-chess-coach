// Shared fetch-based SSE consumption, extracted from useExplain.ts so
// it isn't duplicated by useChat.ts (docs/08-frontend.md, "Stack and
// structure"). Both explain and chat stream via `fetch` rather than
// `EventSource`: a pre-stream failure (404/409/400/503) arrives as an
// ordinary JSON error body, which `EventSource` has no way to surface
// — it only ever fires a bare, bodyless `error` event, and it cannot
// POST a body at all (chat's message send needs both).

/** One `event:`/`data:` block off the wire, before its data is parsed. */
export type RawSseEvent = { event: string; data: string };

// sse-starlette's default separator is "\r\n", with a blank line (so
// "\r\n\r\n") between events; accept plain "\n\n" too since intermediate
// proxies/dev servers may normalize line endings.
const BLOCK_SEP = /\r\n\r\n|\n\n/;
const LINE_SEP = /\r\n|\n/;

/** Splits whatever has arrived so far into complete blocks plus a remainder. */
export function splitBlocks(buffer: string): {
  blocks: string[];
  rest: string;
} {
  const parts = buffer.split(BLOCK_SEP);
  const rest = parts.pop() ?? "";
  return { blocks: parts, rest };
}

/** Parses one block's `event:`/`data:` lines; comment/ping lines are ignored. */
export function parseBlock(block: string): RawSseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split(LINE_SEP)) {
    if (line === "" || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice("data:".length).trim());
    }
  }
  return data.length === 0 ? null : { event, data: data.join("\n") };
}

/** The same `{error:{message}}` convention `api.ts`'s `json()` unwraps
 *  — for a pre-stream failure response (4xx/5xx) that never became SSE. */
export async function errorMessage(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as {
    error?: { message?: string };
  } | null;
  return body?.error?.message ?? `HTTP ${response.status}`;
}

/**
 * Reads `body`, invoking `onEvent` for each complete `event:`/`data:`
 * block as it arrives. `onEvent` returns `false` to stop reading early
 * — the caller has seen a terminal event (`done`/`error`) and doesn't
 * need the rest of the body — which resolves `"stopped"`; returning
 * anything else keeps the loop going. A body that closes on its own
 * without `onEvent` ever returning `false` resolves `"ended"`, which
 * callers treat as an unexpected mid-stream disconnect unless the read
 * was deliberately aborted (a fresh request superseding this one, or
 * unmount).
 */
export async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: RawSseEvent) => boolean | undefined,
): Promise<"ended" | "stopped"> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      return "ended";
    }
    buffer += decoder.decode(value, { stream: true });
    const { blocks, rest } = splitBlocks(buffer);
    buffer = rest;
    for (const block of blocks) {
      const raw = parseBlock(block);
      if (raw === null) {
        continue;
      }
      if (onEvent(raw) === false) {
        return "stopped";
      }
    }
  }
}
