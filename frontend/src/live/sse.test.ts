import { describe, expect, it, vi } from "vitest";
import { backoffDelay, SseParser, startLiveStream, type LiveState } from "./sse";

function streamResponse(chunks: string[], status = 200): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

describe("SseParser", () => {
  it("handles split chunks, comments and multiple events", () => {
    const p = new SseParser();
    expect(p.feed("retry: 5000\nevent: ready\nda")).toEqual([]);
    expect(p.feed('ta: {"topics":[]}\n\n: keep-alive\n\nevent: changed\r\ndata: {"topics":["payments"]}\r\n\r\n')).toEqual([
      { event: "ready", data: '{"topics":[]}' },
      { event: "changed", data: '{"topics":["payments"]}' },
    ]);
  });
});

describe("startLiveStream", () => {
  it("uses the same-origin session cookie (no token anywhere) and delivers messages", async () => {
    const fetchImpl = vi.fn(async () => streamResponse(['event: changed\ndata: {"topics":["alerts"]}\n\n']));
    const messages: string[] = [];
    const states: LiveState[] = [];
    const stop = startLiveStream({
      url: "/api/v1/events",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: (m) => messages.push(m.data),
      onState: (s) => states.push(s),
      sleep: () => new Promise(() => {}), // stop after the first stream ends
    });
    await vi.waitFor(() => expect(messages).toHaveLength(1));
    stop();
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/events");
    expect(init.credentials).toBe("same-origin");
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
    expect(states).toContain("live");
  });

  it("reconnects with growing backoff after failures", async () => {
    let calls = 0;
    const fetchImpl = vi.fn(async () => {
      calls += 1;
      if (calls < 3) throw new TypeError("network down");
      return streamResponse([]);
    });
    const delays: number[] = [];
    const stop = startLiveStream({
      url: "/e",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: () => {},
      onState: () => {},
      sleep: async (ms) => {
        delays.push(ms);
        if (delays.length > 3) await new Promise(() => {});
      },
    });
    await vi.waitFor(() => expect(calls).toBeGreaterThanOrEqual(3));
    stop();
    expect(delays.slice(0, 2)).toEqual([1000, 2000]);
    expect(backoffDelay(20, 30_000)).toBe(30_000);
  });

  it("stops and signs out on 401", async () => {
    const onUnauthorized = vi.fn();
    const states: LiveState[] = [];
    startLiveStream({
      url: "/e",
      fetchImpl: (async () => new Response(null, { status: 401 })) as unknown as typeof fetch,
      onMessage: () => {},
      onState: (s) => states.push(s),
      onUnauthorized,
    });
    await vi.waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
    expect(states.at(-1)).toBe("offline");
  });
});
