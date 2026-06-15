import { useState, type FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";
import { api } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";
export default function WorkspaceHome() {
  const { session, logout } = useSession();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const workspaces = useQuery({
    queryKey: [session.user.id, "workspaces"],
    queryFn: ({ signal }) => api.workspaces(signal),
  });
  const create = useMutation({
    mutationFn: api.createWorkspace,
    onSuccess: (workspace) => navigate(`/workspaces/${workspace.id}`),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  }
  return (
    <main className="workspace-home">
      <header>
        <Link to="/" className="brand">
          ◈ EvidenceDesk
        </Link>
        <button onClick={() => void logout()}>Sign out</button>
      </header>
      <p className="eyebrow">YOUR WORKSPACES</p>
      <h1>Your organization’s knowledge.</h1>
      <p className="muted">
        Choose a workspace to ask questions across its knowledge bases. Sources
        are shared with workspace members; your conversations are private.
      </p>
      <ProblemBanner
        error={workspaces.error}
        retry={() => void workspaces.refetch()}
      />
      {workspaces.isPending && <p role="status">Loading workspaces…</p>}
      <div className="workspace-grid">
        {workspaces.data?.items.map((workspace) => (
          <Link
            className="workspace-card"
            to={`/workspaces/${workspace.id}`}
            key={workspace.id}
          >
            <span className="workspace-icon">
              {workspace.name.slice(0, 1).toUpperCase()}
            </span>
            <h2>{workspace.name}</h2>
            <span className="muted">{workspace.role} access ↗</span>
          </Link>
        ))}
      </div>
      {workspaces.data?.items.length === 0 && (
        <p>You haven’t joined a workspace yet. Create one to get started.</p>
      )}
      <form className="create-workspace" onSubmit={submit}>
        <h2>Create a workspace</h2>
        <label htmlFor="workspace-name">Workspace name</label>
        <div className="inline-form">
          <input
            id="workspace-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={120}
            placeholder="e.g. Product research"
          />
          <button
            className="primary"
            disabled={create.isPending || !name.trim()}
          >
            {create.isPending ? "Creating…" : "Create workspace"}
          </button>
        </div>
        <ProblemBanner error={create.error} />
      </form>
    </main>
  );
}
