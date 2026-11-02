import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";
import { csrfStore } from "../api/client";
import { mockSession, requests, server } from "./server";

// Recharts measures its container; jsdom has no layout engine.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  requests.length = 0;
  mockSession.role = null;
  mockSession.expired = false;
  csrfStore.set(null);
  sessionStorage.clear();
  localStorage.clear();
});
afterAll(() => server.close());
