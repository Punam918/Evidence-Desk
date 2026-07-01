/** Real browser smoke for the local Compose stack. No mocked requests or stored auth state. */
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

// Playwright debug logging can include form input values. This runner never enables it.
delete process.env.DEBUG;
delete process.env.DEBUG_FILE;
delete process.env.PWDEBUG;

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(root, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");
const origin = "https://evidencedesk.localhost:8443";
const documentText = `# Fictional browser verification handbook

This document is synthetic and exists only to verify the EvidenceDesk upload workflow.
For the fictional Cedar team, release notes are reviewed every Thursday at 14:00 UTC.
The release coordinator stores the approved notes in the team's shared knowledge base.
Questions about these notes should cite the published document and its revision.
`;

function options() {
  const { values } = parseArgs({
    options: {
      project: { type: "string", default: "evidencedesk" },
      output: { type: "string" },
      scope: { type: "string", default: "knowledge" },
      "publication-timeout-ms": { type: "string", default: "180000" },
      help: { type: "boolean", default: false },
    },
  });
  if (values.help) {
    console.log(
      "Usage: node scripts/validate_compose_browser.mjs --project evidencedesk --output docs/10-testing/NEW.json --scope login|knowledge|published",
    );
    process.exit(0);
  }
  if (!/^[a-z0-9][a-z0-9_-]{0,60}$/.test(values.project)) {
    throw new Error("Invalid project");
  }
  if (
    !values.output ||
    !["login", "knowledge", "published"].includes(values.scope)
  ) {
    throw new Error("Output path and supported scope are required");
  }
  const output = path.resolve(root, values.output);
  if (existsSync(output)) throw new Error("Report already exists");
  const timeout = Number(values["publication-timeout-ms"]);
  if (!Number.isInteger(timeout) || timeout < 1000 || timeout > 300000) {
    throw new Error(
      "Publication timeout must be between 1000 and 300000 milliseconds",
    );
  }
  return { project: values.project, output, scope: values.scope, timeout };
}

function passwordFor(project) {
  const raw = execFileSync(
    path.join(root, ".venv/bin/python"),
    [
      "-c",
      "import json,sys; from scripts.validate_compose import mounted_file; print(json.dumps({'password':mounted_file(sys.argv[1], 'operator/demo_password').decode().strip()}))",
      project,
    ],
    { cwd: root, maxBuffer: 16384, stdio: ["ignore", "pipe", "ignore"] },
  );
  try {
    const value = JSON.parse(raw.toString("utf8")).password;
    if (!/^[a-f0-9]{48}$/.test(value))
      throw new Error("Unexpected credential format");
    return value;
  } finally {
    raw.fill(0);
  }
}

function safePath(value) {
  try {
    return new URL(value).pathname.replace(
      /[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/gi,
      "{id}",
    );
  } catch {
    return "unparsed";
  }
}

function imageIdentities(project) {
  const result = {};
  for (const service of ["api", "web", "identity"]) {
    const value = execFileSync(
      "docker",
      ["inspect", "--format", "{{.Image}}", `${project}-${service}-1`],
      { cwd: root, stdio: ["ignore", "pipe", "ignore"], encoding: "utf8" },
    ).trim();
    if (!/^sha256:[a-f0-9]{64}$/.test(value))
      throw new Error("Unexpected image identity");
    result[service] = value;
  }
  return result;
}

async function main() {
  const args = options();
  const started = Date.now();
  const report = {
    schema_version: 1,
    recorded_at: new Date().toISOString(),
    project: args.project,
    origin,
    scope: args.scope,
    passed: false,
    real_browser: true,
    mocked_routes: false,
    browser_https_certificate_errors_ignored: true,
    tls_note:
      "Browser context explicitly accepts the local development CA. Strict CA/hostname verification belongs to scripts/validate_compose.py and is not claimed by this browser run.",
    trace_recorded: false,
    auth_state_persisted: false,
    media_captured: false,
    provider_generation_requested: false,
    script_sha256: createHash("sha256")
      .update(readFileSync(fileURLToPath(import.meta.url)))
      .digest("hex"),
    document_sha256: createHash("sha256").update(documentText).digest("hex"),
    steps: [],
    diagnostics: {
      page_error_count: 0,
      console_error_count: 0,
      http_errors: [],
    },
  };
  let stage = "read_local_configuration";
  let browser;
  let context;
  let password = "";
  const step = async (name, operation) => {
    stage = name;
    const start = Date.now();
    const value = await operation();
    report.steps.push({ name, passed: true, duration_ms: Date.now() - start });
    console.log(`Browser check passed: ${name}`);
    return value;
  };
  try {
    password = passwordFor(args.project);
    report.images = imageIdentities(args.project);
    stage = "launch_browser";
    browser = await chromium.launch({
      headless: true,
      args: ["--host-resolver-rules=MAP evidencedesk.localhost 127.0.0.1"],
    });
    report.browser_version = browser.version();
    context = await browser.newContext({
      baseURL: origin,
      ignoreHTTPSErrors: true,
      viewport: { width: 1440, height: 960 },
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on("pageerror", () => {
      report.diagnostics.page_error_count += 1;
    });
    page.on("console", (message) => {
      if (message.type() === "error")
        report.diagnostics.console_error_count += 1;
    });
    page.on("response", (response) => {
      if (response.status() >= 400) {
        report.diagnostics.http_errors.push({
          path: safePath(response.url()),
          status: response.status(),
        });
      }
    });

    await step("anonymous_landing", async () => {
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await expect(
        page.getByRole("link", { name: "Sign in to your workspace" }),
      ).toBeVisible();
    });
    await step("keycloak_login", async () => {
      await page
        .getByRole("link", { name: "Sign in to your workspace" })
        .click();
      await page.waitForURL((url) =>
        url.pathname.startsWith("/identity/realms/evidencedesk/"),
      );
      await page.locator("#username").fill("demo");
      await page.locator("#password").fill(password);
      password = "";
      await page.locator("#kc-login").click();
      await page.waitForURL(
        (url) => url.origin === origin && url.pathname === "/",
      );
      await expect(
        page.getByRole("heading", { name: "A place for your evidence." }),
      ).toBeVisible();
    });

    if (args.scope !== "login") {
      const unique = randomUUID().slice(0, 8);
      const workspaceName = `Browser verification ${unique}`;
      const kbName = `Fictional handbook ${unique}`;
      await step("create_workspace", async () => {
        await page
          .getByRole("textbox", { name: "Workspace name" })
          .fill(workspaceName);
        await page
          .getByRole("button", { name: "Create workspace", exact: true })
          .click();
        await page.waitForURL((url) =>
          /^\/workspaces\/[a-f0-9-]{36}$/.test(url.pathname),
        );
        report.workspace_id = new URL(page.url()).pathname.split("/")[2];
        await expect(
          page.getByRole("link", { name: "Workspace sources" }),
        ).toBeVisible();
      });
      await step("workspace_list_persists_after_reload", async () => {
        await page.goto("/", { waitUntil: "domcontentloaded" });
        await expect(
          page.getByRole("heading", { name: workspaceName, exact: true }),
        ).toBeVisible();
        await page
          .getByRole("link")
          .filter({
            has: page.getByRole("heading", {
              name: workspaceName,
              exact: true,
            }),
          })
          .click();
        await page.getByRole("link", { name: "Workspace sources" }).click();
        await expect(
          page.getByRole("heading", { name: "Your knowledge bases" }),
        ).toBeVisible();
      });
      await step("create_knowledge_base", async () => {
        await page
          .getByRole("textbox", { name: "New knowledge base" })
          .fill(kbName);
        await page
          .getByRole("button", { name: "Create knowledge base", exact: true })
          .click();
        await expect(
          page.getByRole("heading", { name: kbName, exact: true }),
        ).toBeVisible();
      });
      await step("upload_document", async () => {
        await page.getByLabel("Document file", { exact: true }).setInputFiles({
          name: "fictional-browser-handbook.md",
          mimeType: "text/markdown",
          buffer: Buffer.from(documentText),
        });
        await page
          .getByRole("button", { name: "Upload document", exact: true })
          .click();
        await expect(
          page.getByText(/^File received\. Revision status:/),
        ).toBeVisible();
        await page
          .getByRole("link", { name: "View revisions", exact: true })
          .click();
        await expect(
          page.getByRole("heading", {
            name: "fictional-browser-handbook.md",
            exact: true,
          }),
        ).toBeVisible();
        await expect(
          page.getByRole("heading", { name: "Revision 1", exact: true }),
        ).toBeVisible();
        report.document_id = new URL(page.url()).pathname.split("/")[4];
      });
      if (args.scope === "published") {
        await step("document_published", async () => {
          await expect(
            page.getByText("Used for answers", { exact: true }),
          ).toBeVisible({ timeout: args.timeout });
          await expect(page.getByText(/\d+ searchable passages/)).toBeVisible();
        });
      }
      await page.goto("/", { waitUntil: "domcontentloaded" });
    }
    await step("logout_clears_session", async () => {
      await page.getByRole("button", { name: "Sign out", exact: true }).click();
      await expect(
        page.getByRole("link", { name: "Sign in to your workspace" }),
      ).toBeVisible();
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(
        page.getByRole("link", { name: "Sign in to your workspace" }),
      ).toBeVisible();
    });
    if (report.diagnostics.page_error_count !== 0)
      throw new Error("Unhandled browser failure");
    report.passed = true;
  } catch (error) {
    // Playwright errors may include a credential-bearing selector action/callback URL.
    // Persist only the step and a bounded class; never serialize errors or request headers.
    report.failure = {
      stage,
      kind: error?.name === "TimeoutError" ? "timeout" : "check_failed",
    };
  } finally {
    password = "";
    await context?.close().catch(() => {});
    await browser?.close().catch(() => {});
    report.duration_ms = Date.now() - started;
    mkdirSync(path.dirname(args.output), { recursive: true });
    writeFileSync(args.output, JSON.stringify(report, null, 2) + "\n", {
      flag: "wx",
    });
  }
  console.log(
    `Browser smoke ${report.passed ? "passed" : "failed"}: ${path.relative(root, args.output)}`,
  );
  if (!report.passed) process.exitCode = 1;
}

main().catch(() => {
  console.error(
    "Browser smoke could not initialize; check arguments, local tools, and output path.",
  );
  process.exitCode = 1;
});
