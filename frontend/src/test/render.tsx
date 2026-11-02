import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, createQueryClient } from "../App";
import { mockSession } from "./server";

/** Renders the real app (router, auth, providers) at a path with an existing session. */
export function renderApp(path = "/", as: "owner" | "viewer" | null = "owner") {
  mockSession.role = as;
  const queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { ...queryClient.getDefaultOptions().queries, retry: false } });
  const user = userEvent.setup();
  const utils = render(<App queryClient={queryClient} initialPath={path} />);
  return { user, queryClient, ...utils };
}
