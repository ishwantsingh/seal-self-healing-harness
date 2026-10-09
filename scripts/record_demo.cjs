#!/usr/bin/env node
/** Record an actual backend evolution through the UI; never manufacture success. */
const fs = require("node:fs/promises");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { chromium } = require("../ui/node_modules/playwright");

function verifyCompletion(job, incident, rerun) {
  if (job.status !== "activated" || job.result?.selection?.accepted !== true)
    throw Error("No accepted selection and completed activation are recorded.");
  if (!job.rerun_run_id || rerun.run_id !== job.rerun_run_id)
    throw Error("Activation did not record this automatic verification rerun.");
  if (rerun.outcome !== "answered" || rerun.question !== incident.question)
    throw Error("The original query did not succeed on automatic rerun.");
  const active = job.result?.promotion?.active_commit || job.candidate_commit;
  if (!active || rerun.version !== active)
    throw Error(
      "The successful rerun is not attributable to the activated candidate commit.",
    );
  return active;
}

async function recordDemo({
  baseURL = "http://127.0.0.1:4173",
  output = "artifacts/demo",
  question,
  datasetId,
  mode = "live",
  timeout = 900000,
  hold = 3000,
  channel,
  baselineNote,
  resumeJobId,
}) {
  if (!question)
    throw Error(
      "Provide --question with an answerable task the baseline cannot perform.",
    );
  if (!["live", "scripted"].includes(mode))
    throw Error("--mode must be live or scripted.");
  const url = new URL(baseURL);
  if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
    throw Error("The recorder only connects to a local UI server.");
  const dir = path.resolve(output);
  await fs.mkdir(dir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    ...(channel ? { channel } : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    recordVideo: { dir, size: { width: 1600, height: 1000 } },
    timezoneId: "America/New_York",
    reducedMotion: "reduce",
  });
  const page = await context.newPage(),
    video = page.video(),
    chapters = [],
    started = Date.now();
  let evidence = { mode, question, baseURL, baselineNote, success: false },
    failure;
  const api = async (endpoint) => {
    const response = await context.request.get(baseURL + endpoint);
    if (!response.ok())
      throw Error(`Could not read ${endpoint}: HTTP ${response.status()}`);
    return response.json();
  };
  async function chapter(title, detail) {
    chapters.push({
      title,
      seconds: Number(((Date.now() - started) / 1000).toFixed(2)),
    });
    console.log(title);
    await page.evaluate(
      ({ title, detail, mode, baselineNote }) => {
        let overlay = document.querySelector("#demo-caption");
        if (!overlay) {
          overlay = document.createElement("aside");
          overlay.id = "demo-caption";
          overlay.style.cssText =
            "position:fixed;bottom:24px;left:32px;right:32px;padding:18px 22px;background:#171717;color:#fff;border-radius:8px;z-index:1000;font-family:system-ui;box-shadow:0 8px 24px #0002;pointer-events:none;";
          document.body.append(overlay);
        }
        overlay.replaceChildren();
        const label = document.createElement("div");
        label.textContent =
          mode === "scripted"
            ? "SCRIPTED LOCAL DEMO · SYNTHETIC DATA"
            : baselineNote || "LIVE EVOLUTION · RECORDED EVIDENCE";
        label.style.cssText =
          "font-size:11px;letter-spacing:.08em;color:#bbb;margin-bottom:6px";
        const heading = document.createElement("strong");
        heading.textContent = title;
        heading.style.fontSize = "20px";
        const description = document.createElement("p");
        description.textContent = detail;
        description.style.cssText = "font-size:14px;margin:6px 0 0;color:#ddd";
        overlay.append(label, heading, description);
      },
      { title, detail, mode, baselineNote },
    );
    await page.waitForTimeout(hold);
  }
  async function navigate(hash) {
    await page.evaluate((hash) => {
      location.hash = hash;
      window.scrollTo(0, 0);
    }, hash);
  }
  async function events(jobId) {
    return (
      (
        await api(
          "/api/evolution-jobs/" +
            encodeURIComponent(jobId) +
            "/events?after=0&limit=100",
        )
      ).events || []
    );
  }
  async function waitEvent(jobId, stage) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const [job, recorded] = await Promise.all([
        api("/api/evolution-jobs/" + encodeURIComponent(jobId)),
        events(jobId),
      ]);
      const found = recorded.find((event) => event.stage === stage);
      if (found) return { job, event: found };
      if (
        ["rejected", "blocked", "needs_contract", "operational_error"].includes(
          job.status,
        )
      )
        throw Error(
          `Evolution ended at ${job.status}: ${job.reason || "No successful promotion"}`,
        );
      await page.waitForTimeout(500);
    }
    throw Error(`Timed out waiting for recorded stage: ${stage}`);
  }
  async function tab(name) {
    await page.locator(`[data-evolution-tab="${name}"]`).click();
  }
  try {
    await page.goto(baseURL + "/#ask", { waitUntil: "networkidle" });
    await page.locator("[data-run-button]").waitFor({ state: "visible" });
    let incident, jobId;
    if (resumeJobId) {
      const stored = await api(
        "/api/evolution-jobs/" + encodeURIComponent(resumeJobId),
      );
      incident = await api(
        "/api/runs/" + encodeURIComponent(stored.incident_run_id),
      );
      if (
        incident.outcome !== "unsupported" ||
        incident.limitation_kind !== "capability_gap"
      )
        throw Error("The resumed job has no recorded capability-gap incident.");
      if (incident.question !== question)
        throw Error("The resumed job belongs to a different original query.");
      jobId = resumeJobId;
      evidence.resumed_job = true;
      evidence.incident = {
        run_id: incident.run_id,
        outcome: incident.outcome,
        version: incident.version,
      };
      evidence.job_id = jobId;
      await navigate("#evolve/" + jobId);
      await page
        .locator("[data-evolve-id]")
        .filter({ hasText: jobId })
        .waitFor();
      await tab("evaluations");
      await waitEvent(jobId, "activated");
      await waitEvent(jobId, "rerun_completed");
    } else {
      if (datasetId) {
        const options = await page
          .locator("#data-source option")
          .evaluateAll((options) =>
            options.map((option) => ({
              value: option.value,
              label: option.textContent,
            })),
          );
        const selected = options.find((option) =>
          option.value.endsWith(":" + datasetId),
        );
        if (!selected)
          throw Error("Requested operator dataset is not available.");
        await page.locator("#data-source").selectOption(selected.value);
      }
      await page
        .getByLabel("Question for the analyst", { exact: true })
        .fill(question);
      await page
        .getByLabel("Question for the analyst", { exact: true })
        .press("Escape");
      await chapter(
        "1. Ask a question the current harness cannot answer",
        question,
      );
      const responsePromise = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/runs" &&
          response.request().method() === "POST",
        { timeout: 110000 },
      );
      await page.locator("[data-run-button]").click();
      const response = await responsePromise;
      if (!response.ok()) throw Error("The initial analysis request failed.");
      const initial = await response.json();
      incident = await api("/api/runs/" + encodeURIComponent(initial.run_id));
      if (
        incident.outcome !== "unsupported" ||
        incident.limitation_kind !== "capability_gap"
      )
        throw Error(
          "The initial run did not establish a capability gap. Choose a task the baseline cannot perform.",
        );
      jobId = initial.evolution_job_id || incident.evolution_job_id;
      if (!jobId)
        throw Error("The failure did not create an automatic evolution job.");
      evidence.incident = {
        run_id: incident.run_id,
        outcome: incident.outcome,
        version: incident.version,
      };
      evidence.job_id = jobId;
      // Submission routes to evolution after loading the stored run. Wait for
      // that route before navigating, so its completion cannot hide the incident.
      await page.waitForURL((url) => url.hash === "#evolve/" + jobId);
      await navigate("#run-details/" + incident.run_id);
      await page
        .locator("[data-outcome]")
        .filter({ hasText: "Capability gap" })
        .waitFor();
      await chapter(
        "2. The failure is captured as evidence",
        incident.limitation_reason ||
          "The incident, input, version, and bounded execution evidence are recorded automatically.",
      );
      await page.locator('[data-tab="trace"]').click();
      await chapter(
        "3. Read the relevant execution log",
        "The evolution worker reads the recorded trace and incident evidence.",
      );
      const traceRead = await waitEvent(jobId, "trace_read");
      if (mode === "live" && traceRead.event.payload?.available !== true)
        throw Error("The live evolution did not capture an available trace.");
      await navigate("#evolve/" + jobId);
      await page
        .locator("[data-evolve-id]")
        .filter({ hasText: jobId })
        .waitFor();
      await tab("events");
      await chapter(
        "4. Automatic diagnosis starts",
        "Recorded events link diagnosis to the original failure and saved workflow.",
      );
      await waitEvent(jobId, "proposal_received");
      await tab("changes");
      await page.locator("[data-evolve-changes]").waitFor();
      await chapter(
        "5. Propose and screen a harness change",
        "Inspect the candidate hypothesis and source diff. Evaluation results decide whether it can be activated.",
      );
      const sourceDiff = page.locator("[data-evolve-changes] details").filter({
        has: page.locator("summary", { hasText: "Candidate source diff" }),
      });
      if (await sourceDiff.count()) {
        await sourceDiff.locator("summary").click();
        await chapter(
          "Candidate source change",
          "The candidate edits the harness; the protected evaluator remains separate.",
        );
      }
      await waitEvent(jobId, "evaluating");
      await tab("evaluations");
      await chapter(
        "6. Run protected evaluations",
        "Previous and candidate versions are compared on the frozen evaluation plan.",
      );
      await waitEvent(jobId, "activated");
      await waitEvent(jobId, "rerun_completed");
    }
    const job = await api("/api/evolution-jobs/" + encodeURIComponent(jobId));
    if (!job.rerun_run_id)
      throw Error("Activation did not record an automatic verification rerun.");
    const rerun = await api(
      "/api/runs/" + encodeURIComponent(job.rerun_run_id),
    );
    const activeCommit = verifyCompletion(job, incident, rerun);
    const evaluations = await api(
      "/api/evolution-jobs/" +
        encodeURIComponent(jobId) +
        "/evaluations?limit=100",
    );
    await page
      .locator("[data-evolve-evaluation-status]")
      .filter({
        hasText: String(job.result.selection.trial_count) + " trials",
      })
      .waitFor();
    await page.locator("[data-evolve-chart]").evaluate((element) => {
      window.scrollTo(
        0,
        window.scrollY + element.getBoundingClientRect().top - 130,
      );
    });
    const candidateTrials = (evaluations.trials || []).filter(
      (trial) => trial.version === "candidate",
    );
    await chapter(
      "7. Evaluation gates pass",
      `${candidateTrials.filter((trial) => trial.passed).length}/${candidateTrials.length} candidate trials passed. Baseline failures remain recorded; the protected selection accepted the repair.`,
    );
    await navigate("#versions");
    await page
      .locator("[data-version-family]")
      .selectOption(job.task_family || "inventory-totals");
    await page
      .locator("[data-versions-list]")
      .filter({ hasText: activeCommit })
      .waitFor();
    await chapter(
      "8. Activate the evaluated harness",
      "The active version points to the candidate that passed selection.",
    );
    await navigate("#harness/" + job.result.promotion.workflow_revision_id);
    await page.locator("[data-harness-workflow] svg").waitFor();
    await chapter(
      "The saved harness now includes the change",
      "The activated revision and component evidence are available for inspection.",
    );
    await navigate("#evolve/" + jobId);
    await page
      .locator("[data-evolve-verification]")
      .filter({ hasText: "Answered" })
      .waitFor();
    await chapter(
      "9. Automatically rerun the original query",
      "The verification result is stored against the activated version.",
    );
    await page.locator("[data-evolve-verification] a").click();
    await page
      .locator("[data-outcome]")
      .filter({ hasText: "Answered" })
      .waitFor();
    await chapter(
      "10. The same query now succeeds",
      rerun.message ||
        "The repaired harness returned an answer with recorded evidence.",
    );
    await page.screenshot({ path: path.join(dir, "result.png") });
    evidence = {
      ...evidence,
      success: true,
      active_commit: activeCommit,
      rerun: {
        run_id: rerun.run_id,
        question: rerun.question,
        outcome: rerun.outcome,
        message: rerun.message,
        version: rerun.version,
      },
      selection: {
        accepted: job.result.selection.accepted,
        plan_id: job.result.selection.plan_id,
        trial_count: job.result.selection.trial_count,
      },
      events: (await events(jobId)).map(({ sequence, stage }) => ({
        sequence,
        stage,
      })),
      trials: (evaluations.trials || []).map(
        ({ case_id, case_label, version, role, passed }) => ({
          case_id,
          case_label,
          version,
          role,
          passed,
        }),
      ),
    };
  } catch (error) {
    failure = error;
    evidence.error = error.message;
    console.error(error.message);
    await page
      .screenshot({ path: path.join(dir, "failure.png") })
      .catch(() => {});
  } finally {
    await context.close();
    const raw = path.join(
      dir,
      evidence.success ? "demo.webm" : "incomplete-demo.webm",
    );
    await video.saveAs(raw);
    await browser.close();
    evidence.chapters = chapters;
    await fs.writeFile(
      path.join(dir, "evidence.json"),
      JSON.stringify(evidence, null, 2),
    );
    if (evidence.success) {
      const converted = spawnSync(
        "ffmpeg",
        [
          "-y",
          "-i",
          raw,
          "-c:v",
          "libx264",
          "-preset",
          "medium",
          "-crf",
          "20",
          "-pix_fmt",
          "yuv420p",
          "-movflags",
          "+faststart",
          path.join(dir, "demo.mp4"),
        ],
        { encoding: "utf8" },
      );
      if (converted.status !== 0)
        console.warn(
          "MP4 conversion unavailable; the complete WebM recording is saved.",
        );
    }
  }
  if (failure) throw failure;
  return evidence;
}
module.exports = { recordDemo, verifyCompletion };
if (require.main === module) {
  const args = process.argv.slice(2),
    options = {};
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i].replace(/^--/, "");
    options[
      {
        url: "baseURL",
        dataset: "datasetId",
        "baseline-note": "baselineNote",
        "resume-job": "resumeJobId",
      }[key] || key
    ] = args[i + 1];
  }
  if (options.hold) options.hold = Number(options.hold);
  if (options.timeout) options.timeout = Number(options.timeout);
  recordDemo(options)
    .then(() => console.log("Demo recording complete"))
    .catch((error) => {
      console.error(error.message);
      process.exitCode = 1;
    });
}
