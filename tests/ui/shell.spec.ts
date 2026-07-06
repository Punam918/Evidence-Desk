import AxeBuilder from "../../apps/web/node_modules/@axe-core/playwright";
import {
  test,
  expect,
  type Page,
} from "../../apps/web/node_modules/@playwright/test";
const user = "00000000-0000-4000-8000-000000000001";
const w = "00000000-0000-4000-8000-000000000002";
const c = "00000000-0000-4000-8000-000000000003";
const run = "00000000-0000-4000-8000-000000000004";
const doc = "00000000-0000-4000-8000-000000000005";
const stamp = "2026-10-11T00:00:00Z";
const answer = {
  status: "answered",
  sentences: [
    {
      text: "The retention period is 30 days. <script>window.pwned=true</script>",
      citation_ids: ["E1"],
    },
  ],
  limitations: [],
};
const citations = [
  {
    evidence_id: "E1",
    chunk_id: doc,
    document_id: doc,
    revision_id: doc,
    title: "Retention policy",
    page_start: 2,
    heading_path: ["Data retention"],
    available: true,
  },
];
const conversation = {
  id: c,
  title: "Retention questions",
  version: 1,
  created_at: stamp,
  updated_at: stamp,
};
function frame(seq: number, type: string, data: unknown) {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ spec_version: "1", run_id: run, seq, type, occurred_at: stamp, data })}\n\n`;
}
async function fixtures(
  page: Page,
  mode: "empty" | "reconnect" | "unavailable" = "empty",
  options: { role?: "owner" | "viewer"; noSources?: boolean } = {},
) {
  await page.route("**/auth/logout", (route) => route.fulfill({ status: 204 }));
  let posts = 0,
    streams = 0,
    complete = false;
  let revoked = false,
    missingOriginal = false;
  let feedbackFails = false;
  let usageState: "data" | "empty" | "error" = "data";
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = {};
    if (path === "/v1/me")
      data = {
        user: { id: user, email: "demo@example.test", display_name: "Aayush" },
        csrf_token: "fixture-csrf",
        expires_at: "2099-01-01T00:00:00Z",
      };
    else if (path === "/v1/workspaces")
      data = {
        items: [
          {
            id: w,
            name: "Research workspace",
            role: options.role ?? "owner",
            version: 1,
            created_at: stamp,
          },
        ],
        next_cursor: null,
      };
    else if (path === `/v1/workspaces/${w}`)
      data = {
        id: w,
        name: "Research workspace",
        role: options.role ?? "owner",
        version: 1,
        created_at: stamp,
      };
    else if (path.endsWith("/tool-executions"))
      data = { items: [], next_cursor: null };
    else if (path.endsWith("/briefs")) data = { items: [], next_cursor: null };
    else if (path.endsWith("/models"))
      data = {
        items: [
          {
            key: "paid_standard",
            display_name: "Standard",
            provider: "openai",
            privacy_class: "hosted",
            available: mode !== "unavailable",
            supports_schema: true,
            context_cap: 16384,
            unavailable_reason:
              mode === "unavailable" ? "Awaiting readiness probe" : null,
          },
        ],
        next_cursor: null,
      };
    else if (path.endsWith("/knowledge-bases"))
      data = {
        items: options.noSources
          ? []
          : [
              {
                id: doc,
                workspace_id: w,
                name: "Company handbook",
                document_count: 1,
                created_at: stamp,
              },
            ],
        next_cursor: null,
      };
    else if (path.endsWith("/feedback")) {
      expect(request.method()).toBe("PUT");
      expect(request.headers()["x-csrf-token"]).toBe("fixture-csrf");
      if (feedbackFails)
        return route.fulfill({
          status: 503,
          json: { code: "unavailable", detail: "Feedback could not be saved." },
        });
      data = { message_id: doc, ...request.postDataJSON(), updated_at: stamp };
    } else if (path.endsWith("/usage")) {
      if (usageState === "error")
        return route.fulfill({
          status: 503,
          json: { code: "unavailable", detail: "Usage is unavailable." },
        });
      data = {
        buckets:
          usageState === "empty"
            ? []
            : [
                {
                  day: "2026-10-11",
                  model_key: "paid_standard",
                  requests: 3,
                  input_tokens: 1200,
                  output_tokens: 100,
                  cost_micro_usd: 4500,
                  estimated_attempts: 1,
                },
              ],
        reserved_micro_usd: usageState === "empty" ? 0 : 8000,
        currency: "USD",
      };
    } else if (path.endsWith(`/chunks/${doc}`)) {
      if (revoked)
        return route.fulfill({
          status: 404,
          json: { code: "not_found", detail: "Source unavailable." },
        });
      data = {
        id: doc,
        document_id: doc,
        revision_id: doc,
        text: "Exact retained source. <script>window.pwned=true</script>",
        page_start: 2,
        page_end: 3,
        heading_path: ["Data retention"],
        content_sha256: "a".repeat(64),
      };
    } else if (path.endsWith("/download")) {
      expect(new URL(request.url()).searchParams.get("revision_id")).toBe(doc);
      if (missingOriginal)
        return route.fulfill({
          status: 503,
          json: {
            code: "storage_unavailable",
            detail: "Original document could not be read.",
          },
        });
      return route.fulfill({
        contentType: "application/octet-stream",
        body: "Original retained bytes",
      });
    } else if (path.endsWith("/conversations") && request.method() === "POST")
      data = conversation;
    else if (path.endsWith("/conversations"))
      data = { items: [conversation], next_cursor: null };
    else if (path.endsWith(`/conversations/${c}`))
      data = { ...conversation, version: complete ? 2 : 1 };
    else if (path.endsWith("/messages"))
      data = {
        items: complete
          ? [
              {
                id: doc,
                conversation_id: c,
                sequence: 2,
                role: "assistant",
                state: "final",
                text: "",
                answer,
                citations,
                created_at: stamp,
                source_status: "available",
              },
            ]
          : [],
        next_cursor: null,
      };
    else if (path.endsWith("/events")) {
      streams++;
      if (streams === 1 && mode === "reconnect")
        return route.fulfill({
          contentType: "text/event-stream",
          body:
            frame(1, "run.accepted", { status: "queued" }) +
            frame(2, "answer.delta", {
              text: "A draft answer",
              draft: true,
              sentence_index: 0,
            }),
        });
      complete = true;
      return route.fulfill({
        contentType: "text/event-stream",
        body:
          (streams === 1
            ? frame(1, "run.accepted", { status: "queued" }) +
              frame(2, "answer.delta", {
                text: "Draft",
                draft: true,
                sentence_index: 0,
              })
            : "") +
          frame(3, "answer.completed", { message_id: doc, answer, citations }),
      });
    } else if (path.endsWith("/runs") || path.endsWith(`/runs/${run}`)) {
      if (request.method() === "POST") {
        posts++;
        expect(request.headers()["x-csrf-token"]).toBe("fixture-csrf");
        expect(request.headers()["idempotency-key"].length).toBeGreaterThan(15);
      }
      data = {
        id: run,
        conversation_id: c,
        status: complete ? "completed" : "queued",
        phase: "queued",
        model_key: "paid_standard",
        degraded: [],
        events_url: `/v1/workspaces/${w}/runs/${run}/events`,
        message_id: doc,
        error_code: null,
        created_at: stamp,
        completed_at: complete ? stamp : null,
      };
    } else
      return route.fulfill({
        status: 404,
        json: { code: "not_found", detail: "Not found." },
      });
    await route.fulfill({
      status: request.method() === "POST" ? 202 : 200,
      json: data,
    });
  });
  return {
    feedbackFail: () => {
      feedbackFails = true;
    },
    usageState: (state: "data" | "empty" | "error") => {
      usageState = state;
    },
    posts: () => posts,
    streams: () => streams,
    revoke: () => {
      revoked = true;
    },
    missing: () => {
      missingOriginal = true;
    },
  };
}

test("keyboard question, reconnect without duplicate generation, safe answer and source dialog", async ({
  page,
}) => {
  const calls = await fixtures(page, "reconnect");
  await page.goto(`/workspaces/${w}`);
  const composer = page.getByRole("textbox", {
    name: "Ask a question about your documents",
  });
  await composer.fill("What is the retention period?");
  await expect(
    page.getByRole("button", { name: "Send question" }),
  ).toBeEnabled();
  await composer.press("Enter");
  await expect(
    page.getByText("The retention period is 30 days.", { exact: false }),
  ).toBeVisible();
  expect(calls.posts()).toBe(1);
  expect(calls.streams()).toBe(2);
  expect(
    await page.evaluate(() => (window as unknown as { pwned?: boolean }).pwned),
  ).toBeUndefined();
  await page.getByRole("button", { name: "Open source E1" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Retention policy" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open source E1" }),
  ).toBeFocused();
});

for (const width of [360, 768, 1280])
  test(`responsive empty workspace ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await fixtures(page);
    await page.goto(`/workspaces/${w}`);
    await expect(
      page.getByRole("heading", { name: "Find the answer. See the evidence." }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    if (width < 1024) {
      await page.getByRole("button", { name: "Open navigation" }).click();
      await expect(
        page.getByRole("link", { name: "Retention questions" }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Close navigation" }).click();
    }
    const accessibility = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(accessibility.violations).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`workspace-${width}.png`),
      fullPage: true,
    });
  });

test("unavailable model cannot be submitted", async ({ page }) => {
  await fixtures(page, "unavailable");
  await page.goto(`/workspaces/${w}`);
  await page
    .getByRole("textbox", { name: "Ask a question about your documents" })
    .fill("Question");
  await expect(
    page.getByRole("button", { name: "Send question" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("option", { name: "Standard · Awaiting readiness probe" }),
  ).toHaveJSProperty("disabled", true);
  await expect(
    page.getByText("No model is ready to answer.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Check model availability" }),
  ).toBeVisible();
});

for (const role of ["owner", "viewer"] as const)
  test(`empty organization sources explain the ${role} next step`, async ({
    page,
  }) => {
    await fixtures(page, "empty", { role, noSources: true });
    await page.goto(`/workspaces/${w}`);
    await page
      .getByRole("textbox", { name: "Ask a question about your documents" })
      .fill("Question");
    await expect(
      page.getByRole("button", { name: "Send question" }),
    ).toBeDisabled();
    await expect(
      page.getByText(
        role === "owner"
          ? "Add a knowledge base and publish a document before asking a question."
          : "This workspace has no knowledge bases yet. An editor or administrator can add shared sources.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Evaluation reports", exact: true }),
    ).toHaveCount(role === "owner" ? 1 : 0);
    if (role === "owner")
      await expect(
        page.getByRole("link", { name: "Create a knowledge base →" }),
      ).toBeVisible();
  });

for (const width of [360, 1440])
  test(`shared workspace navigation preserves context and mobile focus at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixtures(page);
    await page.goto(`/workspaces/${w}`);
    if (width < 1024) {
      const menu = page.getByRole("button", { name: "Open navigation" });
      await menu.click();
      await expect(
        page.getByRole("link", { name: "◈ EvidenceDesk" }),
      ).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      await expect(
        page.getByRole("button", { name: "Sign out" }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        page.getByRole("link", { name: "◈ EvidenceDesk" }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toBeFocused();
      await expect(menu).toHaveAttribute("aria-expanded", "false");
    }
    for (const [destination, heading] of [
      ["Workspace sources", "Your knowledge bases"],
      ["Compare revisions", "Compare two revisions"],
      ["Your briefs", "Your briefs"],
      ["Usage and costs", "Model usage"],
    ]) {
      if (width < 1024)
        await page.getByRole("button", { name: "Open navigation" }).click();
      await page
        .getByRole("navigation", {
          name:
            destination === "Usage and costs"
              ? "Workspace reports"
              : "Workspace tools",
        })
        .getByRole("link", { name: destination, exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: heading, exact: true }),
      ).toBeVisible();
      await expect(page.locator(".workspace-location")).toContainText(
        "Research workspace",
      );
      if (width < 1024)
        await expect(
          page.getByRole("button", { name: "Open navigation" }),
        ).toHaveAttribute("aria-expanded", "false");
      else
        await expect(
          page
            .getByRole("navigation", { name: "Conversation history" })
            .getByRole("link", { name: "Retention questions" }),
        ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
    }
  });

test("expired session displays login and clears tenant interface", async ({
  page,
}) => {
  await fixtures(page);
  await page.goto(`/workspaces/${w}`);
  await expect(
    page.getByRole("heading", { name: "New conversation" }),
  ).toBeVisible();
  await page.evaluate(() =>
    window.dispatchEvent(new Event("ed:session-expired")),
  );
  await expect(
    page.getByRole("link", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  await expect(page.getByText("Research workspace")).toHaveCount(0);
});

test("logout propagates to another tab and removes private workspace content", async ({
  page,
  context,
}) => {
  await fixtures(page);
  const other = await context.newPage();
  await fixtures(other);
  await page.goto(`/workspaces/${w}`);
  await other.goto(`/workspaces/${w}`);
  await expect(
    other.getByRole("heading", { name: "New conversation" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("link", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  await expect(
    other.getByRole("link", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  await expect(other.getByText("Research workspace")).toHaveCount(0);
});

for (const width of [360, 768, 1280])
  test(`exact evidence inspector ${width}px`, async ({ page }) => {
    const calls = await fixtures(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/workspaces/${w}`);
    await page
      .getByRole("textbox", { name: "Ask a question about your documents" })
      .fill("What is the retention period?");
    await page.getByRole("button", { name: "Send question" }).click();
    await page.getByRole("button", { name: "Open source E1" }).click();
    const dialog = page.getByRole("dialog");
    await expect(
      dialog.getByText("Exact retained source.", { exact: false }),
    ).toBeVisible();
    await expect(dialog.getByText("2–3", { exact: true })).toBeVisible();
    expect(
      await page.evaluate(
        () => (window as unknown as { pwned?: boolean }).pwned,
      ),
    ).toBeUndefined();
    expect(
      (
        await new AxeBuilder({ page })
          .include("dialog")
          .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`evidence-${width}.png`),
      fullPage: true,
    });
    const download = page.waitForEvent("download");
    await dialog
      .getByRole("button", { name: "Download original revision" })
      .click();
    expect((await download).suggestedFilename()).toBe("Retention policy");
    calls.missing();
    await dialog
      .getByRole("button", { name: "Download original revision" })
      .click();
    await expect(dialog.getByRole("alert")).toContainText(
      "Original document could not be read.",
    );
    calls.revoke();
    await expect(
      dialog.getByText("Source unavailable.", { exact: false }),
    ).toBeVisible({ timeout: 10000 });
    await expect(dialog.locator(".source-passage")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("button", { name: "Open source E1" }),
    ).toBeFocused();
  });

test("private feedback preserves saved vote on a failed edit", async ({
  page,
}) => {
  const calls = await fixtures(page);
  await page.goto(`/workspaces/${w}`);
  await page
    .getByRole("textbox", { name: "Ask a question about your documents" })
    .fill("Question");
  await page.getByRole("button", { name: "Send question" }).click();
  const yes = page.getByRole("button", { name: "Helpful", exact: true });
  await yes.click();
  await expect(
    page.getByText("Feedback saved. You can change it."),
  ).toBeVisible();
  await expect(yes).toHaveAttribute("aria-pressed", "true");
  await page.getByText("Add a reason", { exact: true }).click();
  await page
    .getByLabel("Feedback reason (optional)")
    .fill("The citation is helpful.");
  await page.getByRole("button", { name: "Save feedback" }).click();
  await expect(
    page.getByRole("button", { name: "Save feedback" }),
  ).toBeEnabled();
  calls.feedbackFail();
  await page.getByRole("button", { name: "Not helpful", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Feedback could not be saved.",
  );
  await expect(yes).toHaveAttribute("aria-pressed", "true");
});
for (const width of [360, 1280])
  test(`usage ledger, estimates and empty/error states ${width}px`, async ({
    page,
  }) => {
    const calls = await fixtures(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/workspaces/${w}/usage`);
    await expect(
      page.getByRole("heading", { name: "Model usage", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Includes 1 estimated attempt", { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("table")).toContainText("$0.0045");
    await expect(
      page.getByText("Outstanding reservations", { exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect(
      (
        await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`usage-${width}.png`),
      fullPage: true,
    });
    await page.getByLabel("Start date (UTC)").fill("2026-01-01");
    await page.getByLabel("End date (UTC)").fill("2026-04-01");
    await page.getByRole("button", { name: "Apply dates" }).click();
    await expect(page.getByRole("alert")).toContainText("1–90 days");
    calls.usageState("empty");
    await page.getByLabel("End date (UTC)").fill("2026-01-01");
    await page.getByRole("button", { name: "Apply dates" }).click();
    await expect(
      page.getByText("No model attempts recorded in this date range."),
    ).toBeVisible();
    calls.usageState("error");
    await page.getByLabel("End date (UTC)").fill("2026-01-02");
    await page.getByRole("button", { name: "Apply dates" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "Usage is unavailable.",
    );
    await expect(
      page.getByText("No model attempts recorded in this date range."),
    ).not.toBeVisible();
    await expect(page.getByRole("table")).toHaveCount(0);
  });

for (const width of [360, 768, 1280])
  test(`comparison selects two revisions and keeps scope ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixtures(page);
    const right = "00000000-0000-4000-8000-000000000006";
    let submitted: Record<string, unknown> | null = null;
    await page.route("**/v1/**/documents?*", (route) =>
      route.fulfill({
        json: {
          items: [{ id: doc, title: "Retention policy", kb_id: doc }],
          next_cursor: null,
        },
      }),
    );
    await page.route("**/v1/**/revisions?*", (route) =>
      route.fulfill({
        json: {
          items: [
            { id: doc, document_id: doc, revision_no: 1, status: "superseded" },
            {
              id: right,
              document_id: doc,
              revision_no: 2,
              status: "published",
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route("**/v1/**/runs", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      submitted = route.request().postDataJSON();
      return route.fallback();
    });
    await page.goto(`/workspaces/${w}/compare`);
    for (const side of ["Left", "Right"]) {
      await page.getByLabel(`${side} knowledge base`).selectOption(doc);
      await page.getByLabel(`${side} document`).selectOption(doc);
      await page.getByLabel(`${side} version`).selectOption(doc);
    }
    await page
      .getByLabel("Comparison focus")
      .fill("Compare retention policies.");
    await expect(
      page.getByText("Choose two different revisions."),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Compare revisions" }),
    ).toBeDisabled();
    await page.getByLabel("Right version").selectOption(right);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`compare-${width}.png`),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Compare revisions" }).click();
    await expect(page).toHaveURL(new RegExp(`conversations/${c}\\?run=${run}`));
    expect(submitted).toMatchObject({
      intent: "compare",
      revision_ids: [doc, right],
      kb_ids: [doc],
    });
  });

test("brief approval shows pending until artifact exists and revocation hides content", async ({
  page,
}) => {
  await fixtures(page);
  const execution = {
    id: run,
    run_id: run,
    tool_name: "save_brief",
    status: "proposed",
    payload_sha256: "a".repeat(64),
    proposal: { title: "Evidence brief", answer_message_id: doc },
    expires_at: "2099-10-12T00:00:00Z",
    brief_id: null as string | null,
    error_code: null,
  };
  let proposed = false;
  let revoked = false;
  await page.route("**/v1/**/tool-executions?*", (route) =>
    route.fulfill({
      json: { items: proposed ? [execution] : [], next_cursor: null },
    }),
  );
  await page.route("**/v1/**/brief-proposals", (route) => {
    proposed = true;
    expect(route.request().postDataJSON()).toEqual(execution.proposal);
    return route.fulfill({ status: 201, json: execution });
  });
  await page.route("**/v1/**/approval", (route) => {
    expect(route.request().postDataJSON()).toEqual({
      decision: "approve",
      expected_payload_sha256: execution.payload_sha256,
    });
    execution.status = "approved";
    return route.fulfill({ json: execution });
  });
  await page.route("**/v1/**/briefs?*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: doc,
            title: revoked ? "Unavailable brief" : "Evidence brief",
            answer: revoked ? null : answer,
            citations: revoked ? [] : citations,
            created_at: stamp,
            source_status: revoked ? "unavailable" : "available",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.goto(`/workspaces/${w}`);
  await page
    .getByRole("textbox", { name: "Ask a question about your documents" })
    .fill("Retention?");
  await page.getByRole("button", { name: "Send question" }).click();
  await page.getByRole("button", { name: "Preview save proposal" }).click();
  await expect(
    page.getByText("Research workspace / Your briefs"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Approve save" }).click();
  await expect(
    page.getByText("Approval recorded. Waiting for the save to finish."),
  ).toBeVisible();
  await expect(
    page.getByText("Brief saved.", { exact: true }),
  ).not.toBeVisible();
  execution.status = "succeeded";
  execution.brief_id = doc;
  await expect(page.getByText("Brief saved.", { exact: true })).toBeVisible({
    timeout: 10000,
  });
  await page.getByRole("link", { name: "Open your briefs" }).click();
  await expect(page.getByText(answer.sentences[0].text)).toBeVisible();
  revoked = true;
  await expect(
    page.getByText(
      "This brief is unavailable because its sources were removed.",
    ),
  ).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(answer.sentences[0].text)).not.toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});
