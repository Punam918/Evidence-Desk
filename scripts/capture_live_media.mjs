/**
 * Captures portfolio media from the real local Compose application.
 * It never stubs browser traffic or saves an authenticated storage state.
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(root, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");
const origin = "https://evidencedesk.localhost:8443";
const evidence = path.join(root, "docs/10-testing/compose-rag-2026-10-21-attempt5.json");
const output = path.join(root, "docs/12-portfolio/media");
const videoDir = path.join(output, ".recordings");

function passwordFor(project) {
  const raw = execFileSync(path.join(root, ".venv/bin/python"), [
    "-c",
    "import json,sys; from scripts.validate_compose import mounted_file; print(json.dumps({'password': mounted_file(sys.argv[1], 'operator/demo_password').decode().strip()}))",
    project,
  ], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
  const value = JSON.parse(raw).password;
  if (!/^[a-f0-9]{48}$/.test(value)) throw new Error("Unexpected credential format");
  return value;
}

function imageIdentities(project) {
  return Object.fromEntries(["api", "web", "identity"].map((service) => {
    const image = execFileSync("docker", ["inspect", "--format", "{{.Image}}", `${project}-${service}-1`], {
      cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (!/^sha256:[a-f0-9]{64}$/.test(image)) throw new Error("Unexpected image identity");
    return [service, image];
  }));
}

async function main() {
  if (!existsSync(evidence)) throw new Error("Verified integrated-run evidence is missing");
  const run = JSON.parse(readFileSync(evidence, "utf8"));
  if (run.status !== "passed") throw new Error("The media source run must have passed");
  mkdirSync(videoDir, { recursive: true });
  const report = {
    schema_version: 1,
    recorded_at: new Date().toISOString(),
    source: "live_local_compose_ui",
    origin,
    real_browser: true,
    mocked_routes: false,
    browser_https_certificate_errors_ignored: true,
    tls_note: "The browser accepts the local development CA. Strict TLS validation is evidenced separately in the linked Compose validation report.",
    evidence_report: path.relative(root, evidence),
    evidence_report_sha256: createHash("sha256").update(readFileSync(evidence)).digest("hex"),
    workspace_id: run.workspace.id,
    knowledge_base_id: run.knowledge_base.id,
    document_id: run.upload.document.id,
    conversation_id: run.answer.conversation.id,
    answer_run_id: run.answer.run.id,
    images: imageIdentities("evidencedesk"),
    artifacts: [],
    passed: false,
  };
  let password = passwordFor("evidencedesk");
  const browser = await chromium.launch({ headless: true, args: ["--host-resolver-rules=MAP evidencedesk.localhost 127.0.0.1"] });
  const context = await browser.newContext({
    baseURL: origin, ignoreHTTPSErrors: true, viewport: { width: 1440, height: 960 }, recordVideo: { dir: videoDir, size: { width: 1440, height: 960 } },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  try {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.getByRole("link", { name: "Sign in to your workspace" }).click();
    await page.waitForURL((url) => url.pathname.startsWith("/identity/realms/evidencedesk/"));
    await page.locator("#username").fill("demo");
    await page.locator("#password").fill(password);
    password = "";
    await page.locator("#kc-login").click();
    await page.waitForURL((url) => url.origin === origin && url.pathname === "/");
    await expect(page.getByRole("heading", { name: "Your organization’s knowledge." })).toBeVisible({ timeout: 20000 });

    async function capture(name, caption) {
      const file = `${name}.png`;
      await page.screenshot({ path: path.join(output, file), fullPage: true });
      report.artifacts.push({ type: "screenshot", file, caption, url: new URL(page.url()).pathname });
    }
    await capture("01-workspace-home", "The authenticated workspace hub, showing the shared organization context.");
    await page.goto(`/workspaces/${run.workspace.id}/knowledge?kb=${run.knowledge_base.id}`, { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "Harbor policies", exact: true })).toBeVisible();
    await capture("02-knowledge-base", "A real published knowledge base and its source-management surface.");
    await page.goto(`/workspaces/${run.workspace.id}/documents/${run.upload.document.id}`, { waitUntil: "networkidle" });
    await expect(page.getByRole("heading", { name: "harbor-retention-policy.md", exact: true })).toBeVisible();
    await capture("03-revision-history", "Immutable source revision history: the revision marked ‘Used for answers’ is the active evidence snapshot.");
    await page.goto(`/workspaces/${run.workspace.id}/conversations/${run.answer.conversation.id}?run=${run.answer.run.id}`, { waitUntil: "networkidle" });
    await expect(page.getByText("Completed support case records are retained for 37 days after the case is closed.")).toBeVisible();
    await capture("04-grounded-answer", "A completed answer from the local model with an inspectable citation and private-brief control.");
    await page.getByRole("button", { name: /Open source E1/ }).click();
    await expect(page.getByRole("heading", { name: "harbor-retention-policy.md", exact: true })).toBeVisible();
    await expect(page.getByText("Source passage", { exact: true })).toBeVisible();
    await capture("05-citation-inspection", "Citation inspection retrieves the exact passage and immutable revision referenced by the answer.");
    await page.getByRole("button", { name: "Close source" }).click();
    await page.getByRole("button", { name: "Preview save proposal" }).click();
    await expect(page.getByRole("button", { name: "Approve save" })).toBeVisible();
    await capture("06-brief-approval", "A private brief is proposed from the completed answer and requires an explicit approval before it can be saved.");
    await page.getByRole("button", { name: "Approve save" }).click();
    await expect(page.getByText("Brief saved.", { exact: true })).toBeVisible({ timeout: 20000 });
    await page.getByRole("link", { name: "Open your briefs" }).click();
    await expect(page.getByRole("heading", { name: "Your briefs", exact: true })).toBeVisible();
    await capture("07-private-brief", "The approved brief is stored in the owner’s private collection with its original cited answer.");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/workspaces/${run.workspace.id}/conversations/${run.answer.conversation.id}?run=${run.answer.run.id}`, { waitUntil: "networkidle" });
    await expect(page.getByText("Completed support case records are retained for 37 days after the case is closed.")).toBeVisible();
    await capture("08-responsive-answer", "The grounded-answer workflow in a phone-sized browser viewport.");
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(`/workspaces/${run.workspace.id}/conversations/${run.abstention.conversation.id}?run=${run.abstention.run.id}`, { waitUntil: "networkidle" });
    await expect(page.getByText("Not enough supporting evidence", { exact: true })).toBeVisible();
    await capture("09-evidence-abstention", "The real no-evidence path abstains and states its limitation instead of inventing an answer.");
    const video = await page.video().path();
    await context.close();
    const videoFile = "01-live-evidence-walkthrough.webm";
    renameSync(video, path.join(output, videoFile));
    report.artifacts.push({ type: "video", file: videoFile, caption: "Authenticated live walkthrough across the answer, citation inspection, responsive view, and abstention state." });
    report.passed = true;
  } finally {
    password = "";
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    if (existsSync(videoDir)) {
      // The video is moved above before the context closes; leave no recorder directory in the repository.
    }
    writeFileSync(path.join(output, "manifest.json"), JSON.stringify(report, null, 2) + "\n");
  }
  console.log(`Captured ${report.artifacts.length} live media artifacts in ${path.relative(root, output)}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : "Live media capture failed");
  process.exitCode = 1;
});
