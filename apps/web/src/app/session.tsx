import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, setCsrf, type Schema } from "../lib/api";
import { ProblemBanner } from "../components/ProblemBanner";
const Session = createContext<{
  session: Schema["Session"];
  logout: () => Promise<void>;
} | null>(null);
export function useSession() {
  const value = useContext(Session);
  if (!value) throw new Error("Session is required.");
  return value;
}

export function SessionGate({ children }: { children: ReactNode }) {
  const cache = useQueryClient();
  const [expired, setExpired] = useState(false);
  const [logoutError, setLogoutError] = useState<unknown>();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => api.me(signal),
    retry: false,
    staleTime: 60_000,
    enabled: !expired,
  });
  useEffect(() => {
    const channel = new BroadcastChannel("evidencedesk-session");
    function clear() {
      setCsrf("");
      setExpired(true);
      void cache.cancelQueries();
      cache.clear();
    }
    channel.onmessage = (event) => {
      if (event.data === "logout") clear();
    };
    window.addEventListener("ed:session-expired", clear);
    return () => {
      channel.close();
      window.removeEventListener("ed:session-expired", clear);
    };
  }, [cache]);
  useEffect(() => {
    if (session.data && !expired) setCsrf(session.data.csrf_token);
  }, [session.data, expired]);
  async function logout() {
    try {
      await api.logout();
      const channel = new BroadcastChannel("evidencedesk-session");
      channel.postMessage("logout");
      channel.close();
      window.dispatchEvent(new Event("ed:session-expired"));
    } catch (error) {
      setLogoutError(error);
    }
  }
  if (session.isPending && !expired)
    return (
      <main className="center-screen">
        <p role="status">Opening your workspace…</p>
      </main>
    );
  if (
    expired ||
    (session.error instanceof ApiError && session.error.status === 401)
  )
    return (
      <main className="login-page">
        <a className="brand" href="/">
          ◈ EvidenceDesk
        </a>
        <div className="login-card">
          <p className="eyebrow">YOUR SOURCES. CLEAR ANSWERS.</p>
          <h1>
            Knowledge you can
            <br />
            trace back.
          </h1>
          <p>
            Ask questions across your organization’s knowledge bases. Sources
            are shared with workspace members; your conversations are private.
          </p>
          <a className="button primary" href="/auth/login?return_to=%2F">
            Sign in to your workspace <span aria-hidden="true">↗</span>
          </a>
          <small>
            Sign in securely through your organization’s identity provider.
          </small>
        </div>
        <footer>
          Grounded answers · Verifiable sources · Private conversations
        </footer>
      </main>
    );
  if (session.error || !session.data)
    return (
      <main className="center-screen">
        <ProblemBanner
          error={session.error ?? new Error("Session unavailable.")}
          retry={() => void session.refetch()}
        />
      </main>
    );
  return (
    <Session.Provider value={{ session: session.data, logout }}>
      <ProblemBanner error={logoutError} />
      {children}
    </Session.Provider>
  );
}
