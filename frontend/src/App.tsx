import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";
import { ApiError } from "./api/client";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import { LoadingBlock } from "./components/ui";
import { LiveUpdatesProvider } from "./live/LiveUpdates";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";

// Pages load on demand so the charting library is fetched only where it is used.
const OverviewPage = lazy(() => import("./pages/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const ProvidersPage = lazy(() => import("./pages/ProvidersPage").then((m) => ({ default: m.ProvidersPage })));
const ProviderDetailPage = lazy(() => import("./pages/ProviderDetailPage").then((m) => ({ default: m.ProviderDetailPage })));
const PaymentsPage = lazy(() => import("./pages/PaymentsPage").then((m) => ({ default: m.PaymentsPage })));
const ThresholdsPage = lazy(() => import("./pages/ThresholdsPage").then((m) => ({ default: m.ThresholdsPage })));
const AlertsPage = lazy(() => import("./pages/AlertsPage").then((m) => ({ default: m.AlertsPage })));
const AwsPage = lazy(() => import("./pages/AwsPage").then((m) => ({ default: m.AwsPage })));
const VouchersPage = lazy(() => import("./pages/VouchersPage").then((m) => ({ default: m.VouchersPage })));

const page = (element: React.ReactNode) => (
  <Suspense fallback={<div className="card"><LoadingBlock rows={5} label="Loading page" /></div>}>{element}</Suspense>
);

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: true,
        // Retry transient failures only; never retry auth or validation errors.
        retry: (count, error) =>
          count < 2 && !(error instanceof ApiError && error.status > 0 && error.status < 500),
      },
    },
  });
}

/** Every route below is private: without a valid session only the sign-in page renders. */
function ProtectedApp() {
  const { status } = useAuth();
  if (status === "checking")
    return (
      <main className="login">
        <LoadingBlock label="Checking session" />
      </main>
    );
  if (status !== "signed_in") return <LoginPage />;
  return (
    <LiveUpdatesProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={page(<OverviewPage />)} />
          <Route path="providers" element={page(<ProvidersPage />)} />
          <Route path="providers/:id" element={page(<ProviderDetailPage />)} />
          <Route path="payments" element={page(<PaymentsPage />)} />
          <Route path="thresholds" element={page(<ThresholdsPage />)} />
          <Route path="alerts" element={page(<AlertsPage />)} />
          <Route path="aws" element={page(<AwsPage />)} />
          <Route path="vouchers" element={page(<VouchersPage />)} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </LiveUpdatesProvider>
  );
}

export function App({ queryClient, initialPath }: { queryClient?: QueryClient; initialPath?: string }) {
  const [client] = useState(() => queryClient ?? createQueryClient());
  const Router = initialPath ? MemoryRouter : BrowserRouter;
  const routerProps = initialPath ? { initialEntries: [initialPath] } : {};
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <AuthProvider>
          <Router {...routerProps}>
            <ProtectedApp />
          </Router>
        </AuthProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}
