import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes, Link } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ViewBoundary } from "../components/ViewBoundary";
import { SessionGate } from "./session";
import WorkspaceLayout from "./WorkspaceLayout";
const Compare = lazy(() => import("../features/compare/ComparePage"));
const Briefs = lazy(() => import("../features/briefs/BriefsPage"));
const Evaluations = lazy(() => import("../features/reports/EvaluationsPage"));
const Usage = lazy(() => import("../features/reports/UsagePage"));
const Home = lazy(() => import("../features/workspaces/WorkspaceHome"));
const Chat = lazy(() => import("../features/chat/ChatWorkspace"));
const Knowledge = lazy(() => import("../features/knowledge/KnowledgePage"));
const Document = lazy(() => import("../features/knowledge/DocumentPage"));
const client = new QueryClient({
  defaultOptions: {
    queries: { retry: false, staleTime: 10_000, gcTime: 0 },
    mutations: { retry: false },
  },
});
export function App() {
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <ViewBoundary>
          <SessionGate>
            <Suspense
              fallback={
                <main className="center-screen" role="status">
                  Opening workspace…
                </main>
              }
            >
              <Routes>
                <Route path="/" element={<Home />} />
                <Route
                  path="/workspaces/:workspaceId"
                  element={<WorkspaceLayout />}
                >
                  <Route index element={<Chat />} />
                  <Route
                    path="conversations/:conversationId"
                    element={<Chat />}
                  />
                  <Route path="knowledge" element={<Knowledge />} />
                  <Route path="documents/:documentId" element={<Document />} />
                  <Route path="compare" element={<Compare />} />
                  <Route path="briefs" element={<Briefs />} />
                  <Route path="usage" element={<Usage />} />
                  <Route path="evaluations" element={<Evaluations />} />
                </Route>
                <Route
                  path="*"
                  element={
                    <main className="center-screen">
                      <h1>Page not found</h1>
                      <Link to="/">Return to workspaces</Link>
                    </main>
                  }
                />
              </Routes>
            </Suspense>
          </SessionGate>
        </ViewBoundary>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
