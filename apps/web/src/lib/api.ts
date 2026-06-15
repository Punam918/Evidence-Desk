import type { components } from "@evidencedesk/api-client";
export type Schema = components["schemas"];
export type Page<T> = { items: T[]; next_cursor: string | null };

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId = "",
  ) {
    super(message);
  }
}
let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}
export function workspacePath(id: string) {
  return `/v1/workspaces/${encodeURIComponent(id)}`;
}

async function response(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  if (!path.startsWith("/v1/") && path !== "/auth/logout")
    throw new Error("Unsupported API path.");
  const method = options.method ?? "GET";
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD"].includes(method)) headers.set("X-CSRF-Token", csrf);
  if (method === "POST" && !headers.has("Idempotency-Key"))
    headers.set("Idempotency-Key", crypto.randomUUID());
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
    redirect: "error",
    cache: "no-store",
  });
  if (!response.ok) {
    let body: Partial<Schema["Problem"]> = {};
    try {
      body = await response.json();
    } catch {
      /* A gateway can fail without a Problem body. */
    }
    if (response.status === 401)
      window.dispatchEvent(new Event("ed:session-expired"));
    throw new ApiError(
      response.status,
      body.code ?? "request_failed",
      body.detail ?? "The request could not be completed.",
      body.request_id ?? response.headers.get("x-request-id") ?? "",
    );
  }
  return response;
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const result = await response(path, options);
  return result.status === 204
    ? (undefined as T)
    : (result.json() as Promise<T>);
}

export const api = {
  evaluations: (id: string, signal?: AbortSignal, cursor?: string) =>
    request<Page<Schema["Evaluation"]>>(
      `${workspacePath(id)}/evaluations?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  importEvaluation: (id: string, body: unknown, key: string) =>
    request<Schema["Evaluation"]>(`${workspacePath(id)}/evaluations`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": key },
    }),
  proposeBrief: (
    id: string,
    run: string,
    body: Schema["BriefProposalInput"],
    key: string,
  ) =>
    request<Schema["ToolExecution"]>(
      `${workspacePath(id)}/runs/${encodeURIComponent(run)}/brief-proposals`,
      {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "Idempotency-Key": key },
      },
    ),
  approveBrief: (
    id: string,
    execution: string,
    body: Schema["ApprovalInput"],
    key: string,
  ) =>
    request<Schema["ToolExecution"]>(
      `${workspacePath(id)}/tool-executions/${encodeURIComponent(execution)}/approval`,
      {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "Idempotency-Key": key },
      },
    ),
  executions: (id: string, run: string, signal?: AbortSignal) =>
    request<Page<Schema["ToolExecution"]>>(
      `${workspacePath(id)}/runs/${encodeURIComponent(run)}/tool-executions?limit=100`,
      { signal },
    ),
  briefs: (id: string, signal?: AbortSignal, cursor?: string) =>
    request<Page<Schema["Brief"]>>(
      `${workspacePath(id)}/briefs?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  deleteBrief: (id: string, brief: string) =>
    request<void>(`${workspacePath(id)}/briefs/${encodeURIComponent(brief)}`, {
      method: "DELETE",
    }),

  feedback: (id: string, message: string, body: Schema["FeedbackInput"]) =>
    request<Schema["Feedback"]>(
      `${workspacePath(id)}/messages/${encodeURIComponent(message)}/feedback`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  usage: (id: string, start: string, end: string, signal?: AbortSignal) =>
    request<Schema["Usage"]>(
      `${workspacePath(id)}/usage?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}`,
      { signal },
    ),
  chunk: (id: string, chunk: string, signal?: AbortSignal) =>
    request<Schema["Chunk"]>(
      `${workspacePath(id)}/chunks/${encodeURIComponent(chunk)}`,
      { signal },
    ),
  original: async (
    id: string,
    document: string,
    revision: string,
    signal?: AbortSignal,
  ) => {
    const result = await response(
      `${workspacePath(id)}/documents/${encodeURIComponent(document)}/download?revision_id=${encodeURIComponent(revision)}`,
      { signal },
    );
    return result.blob();
  },
  me: (signal?: AbortSignal) =>
    request<Schema["Session"]>("/v1/me", { signal }),
  workspaces: (signal?: AbortSignal) =>
    request<Page<Schema["Workspace"]>>("/v1/workspaces?limit=100", { signal }),
  workspace: (id: string, signal?: AbortSignal) =>
    request<Schema["Workspace"]>(workspacePath(id), { signal }),
  createWorkspace: (name: string) =>
    request<Schema["Workspace"]>("/v1/workspaces", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  conversations: (id: string, signal?: AbortSignal, cursor?: string) =>
    request<Page<Schema["Conversation"]>>(
      `${workspacePath(id)}/conversations?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  conversation: (id: string, conversation: string, signal?: AbortSignal) =>
    request<Schema["Conversation"]>(
      `${workspacePath(id)}/conversations/${encodeURIComponent(conversation)}`,
      { signal },
    ),
  createConversation: (id: string, title: string) =>
    request<Schema["Conversation"]>(`${workspacePath(id)}/conversations`, {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  messages: (
    id: string,
    conversation: string,
    signal?: AbortSignal,
    cursor?: string,
  ) =>
    request<Page<Schema["Message"]>>(
      `${workspacePath(id)}/conversations/${encodeURIComponent(conversation)}/messages?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  models: (id: string, signal?: AbortSignal) =>
    request<Page<Schema["Model"]>>(`${workspacePath(id)}/models?limit=100`, {
      signal,
    }),
  knowledge: (id: string, signal?: AbortSignal, cursor?: string) =>
    request<Page<Schema["KnowledgeBase"]>>(
      `${workspacePath(id)}/knowledge-bases?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  createKnowledge: (id: string, name: string) =>
    request<Schema["KnowledgeBase"]>(`${workspacePath(id)}/knowledge-bases`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  deleteKnowledge: (id: string, kb: string) =>
    request<Schema["JobRef"]>(
      `${workspacePath(id)}/knowledge-bases/${encodeURIComponent(kb)}`,
      { method: "DELETE" },
    ),
  documents: (id: string, kb: string, signal?: AbortSignal, cursor?: string) =>
    request<Page<Schema["Document"]>>(
      `${workspacePath(id)}/knowledge-bases/${encodeURIComponent(kb)}/documents?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  upload: (
    id: string,
    target: { kb: string } | { document: string },
    file: File,
    key: string,
    signal: AbortSignal,
  ) => {
    const body = new FormData();
    body.append("file", file);
    const suffix =
      "kb" in target
        ? `/knowledge-bases/${encodeURIComponent(target.kb)}/documents`
        : `/documents/${encodeURIComponent(target.document)}/revisions`;
    return request<Schema["UploadAccepted"]>(`${workspacePath(id)}${suffix}`, {
      method: "POST",
      body,
      headers: { "Idempotency-Key": key },
      signal,
    });
  },
  job: (id: string, job: string, signal?: AbortSignal) =>
    request<Schema["Job"]>(
      `${workspacePath(id)}/jobs/${encodeURIComponent(job)}`,
      { signal },
    ),
  retryJob: (id: string, job: string, key: string) =>
    request<Schema["JobRef"]>(
      `${workspacePath(id)}/jobs/${encodeURIComponent(job)}/retry`,
      { method: "POST", headers: { "Idempotency-Key": key } },
    ),
  reprocess: (id: string, document: string, version: number, key: string) =>
    request<Schema["UploadAccepted"]>(
      `${workspacePath(id)}/documents/${encodeURIComponent(document)}/reprocess`,
      {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify({
          profile_id: "structure_v1",
          expected_version: version,
        }),
      },
    ),
  document: (id: string, document: string, signal?: AbortSignal) =>
    request<Schema["Document"]>(
      `${workspacePath(id)}/documents/${encodeURIComponent(document)}`,
      { signal },
    ),
  revisions: (
    id: string,
    document: string,
    signal?: AbortSignal,
    cursor?: string,
  ) =>
    request<Page<Schema["Revision"]>>(
      `${workspacePath(id)}/documents/${encodeURIComponent(document)}/revisions?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      { signal },
    ),
  start: (
    id: string,
    conversation: string,
    body: Schema["RunInput"],
    key: string,
  ) =>
    request<Schema["Run"]>(
      `${workspacePath(id)}/conversations/${encodeURIComponent(conversation)}/runs`,
      {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify(body),
      },
    ),
  run: (id: string, run: string, signal?: AbortSignal) =>
    request<Schema["Run"]>(
      `${workspacePath(id)}/runs/${encodeURIComponent(run)}`,
      { signal },
    ),
  cancel: (id: string, run: string) =>
    request<Schema["Run"]>(
      `${workspacePath(id)}/runs/${encodeURIComponent(run)}/cancel`,
      { method: "POST" },
    ),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
};
