// Minimal Server-Sent Events client over fetch().
//
// Streams with fetch (same-origin, session cookie) and parses the SSE format here, so the
// connection lifecycle (backoff, pause when hidden, 401 handling) is fully under our control.

export interface SseMessage {
  event: string;
  data: string;
}

/** Incremental parser: feed text chunks, get complete messages back. */
export class SseParser {
  private buffer = "";
  private event = "message";
  private data: string[] = [];

  feed(chunk: string): SseMessage[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");
    const out: SseMessage[] = [];
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line === "") {
        if (this.data.length) out.push({ event: this.event, data: this.data.join("\n") });
        this.event = "message";
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue; // comment / keep-alive
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") this.event = value;
      else if (field === "data") this.data.push(value);
    }
    return out;
  }
}

export type LiveState = "connecting" | "live" | "reconnecting" | "paused" | "offline";

export interface LiveOptions {
  url: string;
  onMessage: (message: SseMessage) => void;
  onState: (state: LiveState) => void;
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
  /** Injectable for tests. */
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  maxBackoffMs?: number;
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const id = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(id);
      resolve();
    });
  });
}

export function backoffDelay(attempt: number, maxMs: number): number {
  return Math.min(maxMs, 1000 * 2 ** Math.min(attempt, 10));
}

/** Connects and keeps reconnecting with exponential backoff until stopped. */
export function startLiveStream(options: LiveOptions): () => void {
  const controller = new AbortController();
  const doFetch = options.fetchImpl ?? fetch.bind(globalThis);
  const sleep = options.sleep ?? defaultSleep;
  const maxBackoff = options.maxBackoffMs ?? 30_000;

  void (async () => {
    let failures = 0;
    while (!controller.signal.aborted) {
      options.onState(failures === 0 ? "connecting" : "reconnecting");
      try {
        const response = await doFetch(options.url, {
          headers: { Accept: "text/event-stream" },
          signal: controller.signal,
          credentials: "same-origin",
          cache: "no-store",
        });
        if (response.status === 401) {
          options.onState("offline");
          options.onUnauthorized?.();
          return;
        }
        if (!response.ok || !response.body) throw new Error(`stream failed (${response.status})`);
        options.onState("live");
        failures = 0;
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        const parser = new SseParser();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const message of parser.feed(value)) options.onMessage(message);
        }
        // Server closed the stream (bounded lifetime): reconnect promptly.
      } catch {
        if (controller.signal.aborted) return;
        failures += 1;
      }
      if (controller.signal.aborted) return;
      options.onState("reconnecting");
      await sleep(failures === 0 ? 250 : backoffDelay(failures - 1, maxBackoff), controller.signal);
    }
  })();

  return () => controller.abort();
}
