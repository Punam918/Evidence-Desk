import { useEffect, useRef, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
  useParams,
} from "react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import {
  BookOpen,
  ChevronDown,
  Files,
  GitCompareArrows,
  Menu,
  MessageSquare,
  Plus,
  ReceiptText,
  ShieldCheck,
  ChartNoAxesCombined,
} from "lucide-react";
import { api } from "../lib/api";
import { ProblemBanner } from "../components/ProblemBanner";
import { useSession } from "./session";

export default function WorkspaceLayout() {
  const { workspaceId = "" } = useParams();
  return <WorkspaceShell key={workspaceId} id={workspaceId} />;
}

function WorkspaceShell({ id }: { id: string }) {
  const { session, logout } = useSession();
  const { conversationId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [menu, setMenu] = useState(false);
  const [compact, setCompact] = useState(
    () => window.matchMedia("(max-width:1023px)").matches,
  );
  const navPanel = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const base = `/workspaces/${id}`;
  const workspace = useQuery({
    queryKey: [session.user.id, id, "workspace"],
    queryFn: ({ signal }) => api.workspace(id, signal),
  });
  const history = useInfiniteQuery({
    queryKey: [session.user.id, id, "conversations"],
    queryFn: ({ signal, pageParam }) =>
      api.conversations(id, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const isChat =
    location.pathname === base ||
    location.pathname.startsWith(`${base}/conversations/`);
  const isKnowledge =
    location.pathname.startsWith(`${base}/knowledge`) ||
    location.pathname.startsWith(`${base}/documents/`);
  const section = isChat
    ? "Conversations"
    : isKnowledge
      ? "Workspace sources"
      : location.pathname.endsWith("/compare")
        ? "Compare revisions"
        : location.pathname.endsWith("/briefs")
          ? "Your briefs"
          : location.pathname.endsWith("/usage")
            ? "Usage and costs"
            : "Evaluation reports";
  const privateSection = isChat || location.pathname.endsWith("/briefs");

  useEffect(() => {
    const media = window.matchMedia("(max-width:1023px)");
    const update = () => {
      setCompact(media.matches);
      if (!media.matches) setMenu(false);
    };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => setMenu(false), [location.key]);
  function closeMenu() {
    setMenu(false);
    requestAnimationFrame(() => menuButton.current?.focus());
  }
  useEffect(() => {
    if (!menu || !compact) return;
    navPanel.current
      ?.querySelector<HTMLElement>("a[href],button:not(:disabled)")
      ?.focus({ preventScroll: true });
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu();
      }
      if (event.key === "Tab") {
        const items = navPanel.current?.querySelectorAll<HTMLElement>(
          "a[href],button:not(:disabled)",
        );
        if (!items?.length) return;
        const first = items[0],
          last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [menu, compact]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace-content">
        Skip to workspace content
      </a>
      <aside
        id="workspace-navigation"
        ref={navPanel}
        inert={compact && !menu}
        className={`sidebar ${menu ? "is-open" : ""}`}
        aria-label="Workspace navigation"
      >
        <Link className="brand" to="/">
          ◈ EvidenceDesk
        </Link>
        <Link className="workspace-selector" to="/">
          <span className="workspace-icon">
            {workspace.data?.name.slice(0, 1).toUpperCase() ?? "·"}
          </span>
          <span>
            {workspace.data?.name ?? "Workspace"}
            <small>Switch workspace</small>
          </span>
          <ChevronDown size={16} aria-hidden="true" />
        </Link>
        <button
          className="new-chat"
          onClick={() =>
            navigate(base, { state: { newChatKey: crypto.randomUUID() } })
          }
        >
          <Plus size={18} aria-hidden="true" /> New conversation
        </button>
        <nav className="workspace-links" aria-label="Workspace tools">
          <NavLink
            to={base}
            end
            className={() =>
              isChat ? "workspace-link selected" : "workspace-link"
            }
            aria-current={isChat ? "page" : undefined}
          >
            <MessageSquare size={18} aria-hidden="true" /> Ask questions
          </NavLink>
          <Link
            to={`${base}/knowledge`}
            className={`workspace-link ${isKnowledge ? "selected" : ""}`}
            aria-current={isKnowledge ? "page" : undefined}
          >
            <BookOpen size={18} aria-hidden="true" /> Workspace sources
          </Link>
          <NavLink
            to={`${base}/compare`}
            className={({ isActive }) =>
              `workspace-link ${isActive ? "selected" : ""}`
            }
          >
            <GitCompareArrows size={18} aria-hidden="true" /> Compare revisions
          </NavLink>
          <NavLink
            to={`${base}/briefs`}
            className={({ isActive }) =>
              `workspace-link ${isActive ? "selected" : ""}`
            }
          >
            <Files size={18} aria-hidden="true" /> Your briefs
          </NavLink>
        </nav>
        <div className="rail-section">
          <span className="eyebrow">YOUR CONVERSATIONS</span>
          <span className="private-label">
            <ShieldCheck size={13} aria-hidden="true" /> Private
          </span>
        </div>
        <nav className="history-list" aria-label="Conversation history">
          {history.data?.pages
            .flatMap((page) => page.items)
            .map((item) => (
              <Link
                key={item.id}
                className={item.id === conversationId ? "selected" : ""}
                aria-current={item.id === conversationId ? "page" : undefined}
                to={`${base}/conversations/${item.id}`}
              >
                <MessageSquare size={15} aria-hidden="true" />
                <span>{item.title}</span>
              </Link>
            ))}
          {history.isPending && (
            <p role="status" className="muted small">
              Loading history…
            </p>
          )}
          {history.data?.pages[0].items.length === 0 && (
            <p className="muted small">
              Your private conversations appear here.
            </p>
          )}
          {history.hasNextPage && (
            <button
              onClick={() => void history.fetchNextPage()}
              disabled={history.isFetchingNextPage}
            >
              Load more
            </button>
          )}
        </nav>
        <ProblemBanner
          error={history.error}
          retry={() => void history.refetch()}
        />
        <nav
          className="workspace-links workspace-report-links"
          aria-label="Workspace reports"
        >
          <NavLink
            to={`${base}/usage`}
            className={({ isActive }) =>
              `workspace-link ${isActive ? "selected" : ""}`
            }
          >
            <ReceiptText size={18} aria-hidden="true" /> Usage and costs
          </NavLink>
          {workspace.data &&
            ["admin", "owner"].includes(workspace.data.role) && (
              <NavLink
                to={`${base}/evaluations`}
                className={({ isActive }) =>
                  `workspace-link ${isActive ? "selected" : ""}`
                }
              >
                <ChartNoAxesCombined size={18} aria-hidden="true" /> Evaluation
                reports
              </NavLink>
            )}
        </nav>
        <div className="sidebar-bottom">
          <span className="avatar">
            {session.user.display_name.slice(0, 1).toUpperCase()}
          </span>
          <span className="user-name">
            {session.user.display_name}
            <small>{workspace.data?.role ?? "Member"} access</small>
          </span>
          <button className="quiet" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </aside>
      {menu && compact && (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={closeMenu}
        />
      )}
      <div className="workspace-content" inert={compact && menu}>
        <header className="workspace-topbar">
          <button
            ref={menuButton}
            className="mobile-menu"
            aria-label="Open navigation"
            aria-controls="workspace-navigation"
            aria-expanded={menu}
            onClick={() => setMenu(true)}
          >
            <Menu size={22} aria-hidden="true" />
          </button>
          <div className="workspace-location">
            <span>{workspace.data?.name ?? "Workspace"}</span>
            <span aria-hidden="true">/</span>
            <strong>{section}</strong>
          </div>
          <span className="workspace-privacy">
            <ShieldCheck size={15} aria-hidden="true" />
            {privateSection
              ? "Private to you"
              : isKnowledge
                ? "Shared workspace knowledge"
                : "Workspace access"}
          </span>
        </header>
        <div id="workspace-content" className="workspace-route" tabIndex={-1}>
          <ProblemBanner
            error={workspace.error}
            retry={() => void workspace.refetch()}
          />
          <Outlet />
        </div>
      </div>
    </div>
  );
}
