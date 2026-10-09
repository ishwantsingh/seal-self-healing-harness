const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  verifyCompletion,
  recordDemo,
} = require("../../scripts/record_demo.cjs");
const incident = { question: "Original question" };
const job = {
  status: "activated",
  rerun_run_id: "rerun",
  candidate_commit: "new",
  result: {
    selection: { accepted: true },
    promotion: { active_commit: "new" },
  },
};
const rerun = {
  run_id: "rerun",
  question: incident.question,
  outcome: "answered",
  version: "new",
};
test("recorder requires the same query answered on the accepted activated commit", () => {
  assert.equal(verifyCompletion(job, incident, rerun), "new");
  assert.throws(
    () =>
      verifyCompletion(job, incident, {
        ...rerun,
        question: "An easier query",
      }),
    /original query/,
  );
  assert.throws(
    () => verifyCompletion(job, incident, { ...rerun, outcome: "unsupported" }),
    /original query/,
  );
  assert.throws(
    () => verifyCompletion(job, incident, { ...rerun, version: "old" }),
    /activated candidate/,
  );
  assert.throws(
    () => verifyCompletion({ ...job, status: "rejected" }, incident, rerun),
    /accepted selection/,
  );
  assert.throws(
    () =>
      verifyCompletion(
        { ...job, result: { selection: { accepted: false } } },
        incident,
        rerun,
      ),
    /accepted selection/,
  );
  assert.throws(
    () => verifyCompletion(job, incident, { ...rerun, run_id: "unrelated" }),
    /this automatic verification/,
  );
});
test("recorder validates task and local destination before launching a browser", async () => {
  await assert.rejects(recordDemo({}), /Provide --question/);
  await assert.rejects(
    recordDemo({ question: "q", baseURL: "https://example.com" }),
    /local UI server/,
  );
  await assert.rejects(
    recordDemo({ question: "q", mode: "fake" }),
    /live or scripted/,
  );
});
