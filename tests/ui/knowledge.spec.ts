import { test, expect } from "../../apps/web/node_modules/@playwright/test";
import AxeBuilder from "../../apps/web/node_modules/@axe-core/playwright";
const w = "00000000-0000-4000-8000-000000000011";
const kb = "00000000-0000-4000-8000-000000000012";
const doc = "00000000-0000-4000-8000-000000000013";
const stamp = "2026-10-11T00:00:00Z";
for (const role of ["owner", "viewer"])
  for (const width of [360, 1440])
    test(`knowledge management ${role} affordances and revision metadata at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      let exists = role === "viewer";
      let deletes = 0;
      await page.route("**/v1/**", async (route) => {
        const request = route.request(),
          path = new URL(request.url()).pathname;
        let data: unknown;
        if (path === "/v1/me")
          data = {
            user: {
              id: w,
              email: "fixture@example.test",
              display_name: "Reader",
            },
            csrf_token: "fixture",
            expires_at: "2099-01-01T00:00:00Z",
          };
        else if (path === `/v1/workspaces/${w}`)
          data = {
            id: w,
            name: "Research",
            role,
            version: 1,
            created_at: stamp,
          };
        else if (path.endsWith("/conversations"))
          data = { items: [], next_cursor: null };
        else if (
          path.endsWith("/knowledge-bases") &&
          request.method() === "POST"
        ) {
          exists = true;
          data = {
            id: kb,
            workspace_id: w,
            name: "Handbook",
            document_count: 1,
            created_at: stamp,
          };
        } else if (path.endsWith("/knowledge-bases"))
          data = {
            items: exists
              ? [
                  {
                    id: kb,
                    workspace_id: w,
                    name: "Handbook",
                    document_count: 1,
                    created_at: stamp,
                  },
                ]
              : [],
            next_cursor: null,
          };
        else if (
          path.endsWith(`/knowledge-bases/${kb}`) &&
          request.method() === "DELETE"
        ) {
          deletes++;
          exists = false;
          data = { job_id: doc, status: "queued" };
        } else if (path.endsWith("/documents"))
          data = {
            items: [
              {
                id: doc,
                workspace_id: w,
                kb_id: kb,
                title:
                  "Retention policy — organization-wide records and account closure requirements.txt",
                active_revision_id: doc,
                latest_revision_id: kb,
                status: "failed",
                created_at: stamp,
                version: 2,
              },
            ],
            next_cursor: null,
          };
        else if (path.endsWith(`/documents/${doc}`))
          data = {
            id: doc,
            workspace_id: w,
            kb_id: kb,
            title:
              "Retention policy — organization-wide records and account closure requirements.txt",
            active_revision_id: doc,
            latest_revision_id: kb,
            status: "failed",
            created_at: stamp,
            version: 2,
          };
        else if (path.endsWith("/revisions"))
          data = {
            items: [
              {
                id: kb,
                document_id: doc,
                revision_no: 2,
                status: "failed",
                content_sha256: "a".repeat(64),
                profile_id: "bge_small_en_v15",
                chunk_count: 0,
                warnings: ["No extractable text."],
                created_at: stamp,
              },
              {
                id: doc,
                document_id: doc,
                revision_no: 1,
                status: "published",
                content_sha256: "b".repeat(64),
                profile_id: "bge_small_en_v15",
                chunk_count: 2,
                warnings: [],
                created_at: stamp,
              },
            ],
            next_cursor: null,
          };
        else
          return route.fulfill({ status: 404, json: { detail: "Not found" } });
        return route.fulfill({ status: 200, json: data });
      });
      await page.goto(`/workspaces/${w}/knowledge`);
      if (role === "owner") {
        await page
          .getByRole("textbox", { name: "New knowledge base" })
          .fill("Handbook");
        await page
          .getByRole("button", { name: "Create knowledge base" })
          .click();
      } else {
        await expect(
          page.getByRole("button", { name: "Create knowledge base" }),
        ).toHaveCount(0);
        if (width <= 768)
          await page
            .getByRole("combobox", { name: "Knowledge base", exact: true })
            .selectOption(kb);
        else
          await page
            .getByRole("button", { name: "Handbook 1 document" })
            .click();
        await expect(
          page.getByRole("button", { name: "Delete Handbook" }),
        ).toHaveCount(0);
      }
      const documentLink = page.getByRole("link", {
        name: "Retention policy",
        exact: false,
      });
      await expect(documentLink).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      if (role === "owner") {
        const row = await documentLink.boundingBox();
        const upload = await page
          .getByRole("region", { name: "Add a source document" })
          .boundingBox();
        expect(row!.y + row!.height).toBeLessThan(upload!.y);
        await expect(
          page.getByRole("textbox", { name: "New knowledge base" }),
        ).toBeHidden();
      }
      expect(
        (
          await new AxeBuilder({ page })
            .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
            .analyze()
        ).violations,
      ).toEqual([]);
      await page.screenshot({
        path: test.info().outputPath(`knowledge-${role}-${width}.png`),
        fullPage: true,
      });
      await page
        .getByRole("link", { name: "Retention policy", exact: false })
        .click();
      await expect(page.getByText("Used for answers")).toBeVisible();
      await expect(page.getByText("No extractable text.")).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Revision 2" }),
      ).toBeVisible();
      await page.getByRole("link", { name: "Back to sources" }).click();
      if (role === "owner") {
        await page.getByRole("button", { name: "Delete Handbook" }).click();
        expect(deletes).toBe(0);
        await page.getByRole("button", { name: "Keep knowledge base" }).click();
        expect(deletes).toBe(0);
        await page.getByRole("button", { name: "Delete Handbook" }).click();
        await page
          .getByRole("button", { name: "Delete knowledge base", exact: true })
          .click();
        await expect(
          page.getByText(
            "Knowledge base removed. Its documents are no longer accessible.",
          ),
        ).toBeVisible();
        expect(deletes).toBe(1);
      }
    });

test("file upload retries preserve identity and send browser multipart", async ({
  page,
}) => {
  let attempts = 0;
  let jobReads = 0;
  let jobFails = false;
  let retries = 0;
  const keys: string[] = [];
  const document = {
    id: doc,
    workspace_id: w,
    kb_id: kb,
    title: "source.txt",
    active_revision_id: null,
    latest_revision_id: doc,
    status: "queued",
    created_at: stamp,
    version: 1,
  };
  await page.route("**/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    let data: unknown;
    if (path === "/v1/me")
      data = {
        user: { id: w, email: "fixture@example.test", display_name: "Reader" },
        csrf_token: "fixture",
        expires_at: "2099-01-01T00:00:00Z",
      };
    else if (path === `/v1/workspaces/${w}`)
      data = {
        id: w,
        name: "Research",
        role: "owner",
        version: 1,
        created_at: stamp,
      };
    else if (path.endsWith("/conversations"))
      data = { items: [], next_cursor: null };
    else if (path.endsWith("/knowledge-bases"))
      data = {
        items: [
          {
            id: kb,
            workspace_id: w,
            name: "Handbook",
            document_count: 0,
            created_at: stamp,
          },
        ],
        next_cursor: null,
      };
    else if (path.endsWith("/documents") && request.method() === "POST") {
      attempts++;
      keys.push(request.headers()["idempotency-key"]);
      expect(request.headers()["content-type"]).toContain(
        "multipart/form-data; boundary=",
      );
      expect(request.headers()["x-csrf-token"]).toBe("fixture");
      expect(request.postData()).toContain('filename="source.txt"');
      expect(request.postData()).toContain("Safe text fixture");
      if (attempts === 1) {
        await route.fulfill({
          status: 503,
          json: {
            detail: "Temporary storage outage.",
            code: "storage_unavailable",
          },
        });
        return;
      }
      data = {
        document,
        revision_id: doc,
        job: { job_id: doc, status: "queued" },
      };
    } else if (path.endsWith(`/jobs/${doc}/retry`)) {
      retries++;
      data = { job_id: doc, status: "queued" };
    } else if (path.endsWith(`/jobs/${doc}`)) {
      jobReads++;
      data = {
        id: doc,
        kind: "ingest",
        status: retries ? "succeeded" : jobFails ? "dead" : "running",
        stage: "parsing",
        attempts: 1,
        progress: retries ? 100 : 10,
        error_code: jobFails && !retries ? "parse_failed" : null,
        updated_at: stamp,
      };
    } else if (path.endsWith("/documents"))
      data = { items: attempts > 1 ? [document] : [], next_cursor: null };
    else {
      await route.fulfill({ status: 404, json: { detail: "Missing fixture" } });
      return;
    }
    await route.fulfill({ json: data });
  });
  await page.goto(`/workspaces/${w}/knowledge?kb=${kb}`);
  await page.getByLabel("Document file").setInputFiles({
    name: "source.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Safe text fixture"),
  });
  await page
    .getByRole("button", { name: "Upload document", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Temporary storage outage",
  );
  await page
    .getByRole("button", { name: "Upload document", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "File received. Revision status: queued.",
  );
  await expect(page.getByRole("heading", { name: "source.txt" })).toBeVisible();
  expect(attempts).toBe(2);
  expect(keys[0]).toBe(keys[1]);
  await expect(
    page.getByText("Processing: parsing", { exact: true }),
  ).toBeVisible();
  jobFails = true;
  await expect(
    page.getByRole("button", { name: "Retry processing" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry processing" }).click();
  await expect(
    page.getByText("Processing: succeeded", { exact: true }),
  ).toBeVisible();
  expect(retries).toBe(1);
  expect(jobReads).toBeGreaterThanOrEqual(3);
  expect(attempts).toBe(2);
});
