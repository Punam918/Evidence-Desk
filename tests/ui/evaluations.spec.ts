import AxeBuilder from "../../apps/web/node_modules/@axe-core/playwright";
import { test, expect } from "../../apps/web/node_modules/@playwright/test";

const w = "00000000-0000-4000-8000-000000000041";
const report = {
  id: w,
  dataset_version: "benchmark-v1-draft",
  dataset_sha256: "a".repeat(64),
  config_sha256: "b".repeat(64),
  git_sha: null,
  source_snapshot_sha256: "c".repeat(64),
  evidence_scope: "draft_diagnostic",
  variant: "dense",
  model_key: "retrieval-only",
  hardware: "CPU scripted browser fixture",
  status: "failed",
  case_count: 100,
  metrics: [
    { name: "recall_at_20", value: 0.5, n: 80, ci_low: 0.4, ci_high: 0.6 },
    { name: "ndcg_at_10", value: null, n: 0, ci_low: null, ci_high: null },
  ],
  artifact_sha256: "d".repeat(64),
  created_at: "2026-10-12T00:00:00Z",
};

for (const width of [360, 768, 1280])
  test(`evaluation reports at ${width}px: missing metrics, retry identity and revoked access`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    let revoked = false,
      imports = 0;
    const keys: (string | undefined)[] = [];
    await page.route("**/v1/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === "/v1/me")
        return route.fulfill({
          json: {
            user: {
              id: w,
              email: "fixture@example.test",
              display_name: "Reviewer",
            },
            csrf_token: "fixture",
            expires_at: "2099-01-01T00:00:00Z",
          },
        });
      if (path === `/v1/workspaces/${w}`)
        return route.fulfill({
          json: {
            id: w,
            name: "Review workspace",
            role: "owner",
            version: 1,
            created_at: report.created_at,
          },
        });
      if (path.endsWith("/conversations"))
        return route.fulfill({ json: { items: [], next_cursor: null } });
      if (path.endsWith("/evaluations")) {
        if (revoked)
          return route.fulfill({
            status: 403,
            json: {
              code: "forbidden",
              detail: "Your role does not allow this action.",
            },
          });
        if (request.method() === "POST") {
          imports++;
          keys.push(request.headers()["idempotency-key"]);
          expect(request.headers()["x-csrf-token"]).toBe("fixture");
          if (imports === 1)
            return route.fulfill({
              status: 503,
              json: { code: "unavailable", detail: "Please retry the import." },
            });
          return route.fulfill({ status: 201, json: report });
        }
        return route.fulfill({ json: { items: [report], next_cursor: null } });
      }
      return route.fulfill({ status: 404, json: {} });
    });
    await page.goto(`/workspaces/${w}/evaluations`);
    await expect(
      page.getByRole("heading", { name: "Evaluation evidence" }),
    ).toBeVisible();
    await expect(
      page.getByText("Draft diagnostic — not benchmark evidence"),
    ).toBeVisible();
    await expect(page.getByRole("row", { name: /ndcg_at_10/ })).toContainText(
      "Not measured",
    );
    await expect(page.getByRole("row", { name: /recall_at_20/ })).toContainText(
      "80",
    );
    await expect(page.getByRole("row", { name: /recall_at_20/ })).toContainText(
      "0.4 to 0.6",
    );
    await page.getByText("Provenance", { exact: true }).click();
    await expect(page.getByText("Not recorded", { exact: true })).toBeVisible();
    await page.getByLabel("Report file").setInputFiles({
      name: "aggregate.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify(
          Object.fromEntries(
            Object.entries(report).filter(
              ([key]) => !["id", "created_at"].includes(key),
            ),
          ),
        ),
      ),
    });
    await expect(
      page.getByRole("button", { name: "Import report", exact: true }),
    ).toBeEnabled();
    await page
      .getByRole("button", { name: "Import report", exact: true })
      .click();
    await expect(page.getByText("Please retry the import.")).toBeVisible();
    await page
      .getByRole("button", { name: "Import report", exact: true })
      .click();
    await expect(page.getByText("Report imported.")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).toBe(keys[1]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
      path: test.info().outputPath(`evaluations-${width}.png`),
      fullPage: true,
    });
    revoked = true;
    await expect(
      page.getByText("Your role does not allow this action."),
    ).toBeVisible({ timeout: 7000 });
    await expect(page.getByText("benchmark-v1-draft · dense")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Import report", exact: true }),
    ).toHaveCount(0);
  });
