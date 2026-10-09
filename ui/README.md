# Self-Heal local UI

A local operator console for analyses, recorded evidence, evolution jobs, and harness
revisions. Horizontal navigation separates each task. **New analysis** returns to Ask
without discarding an unfinished question. Geist Sans and Geist Mono are served locally;
no external font service or frontend framework is required.

## Start

Configure `.env`, then run from the repository root:

```sh
uv run --env-file .env self-heal seed --fixture evals/analyst/data/small_inventory.json
uv run --env-file .env self-heal ui
```

Open <http://127.0.0.1:4173>. The server listens on loopback by default.
After updating an installed package, reinstall it and restart the local server.

## Use

- **Ask:** choose an inventory table or logistics bundle, write a multiline question,
  and submit with **Run analysis** or **Cmd/Ctrl+Enter**. Enter inserts a newline.
  Suggested questions support arrow keys, Enter to select, and Escape to close.
  Drafts survive navigation, failed requests, and page reloads in the same session.
  **Load demo data** appears when the public logistics bundle has not been loaded
  (8 customers, 3 warehouses, 74 shipments).
- **Runs:** search and filter the latest 100 recorded runs, with 10 rows per page.
  Links contain run IDs and can be reopened directly. Data, Tool calls, and Trace
  show recorded evidence; the timeline and related evolution provide context.
- **Evolve:** inspect job metadata, recorded stages, candidate source changes,
  protected evaluation results, and events. Rejected or blocked jobs never imply
  successful activation. Running jobs refresh every two seconds while this page is open.
- **Harness:** select a family and revision, inspect components, zoom or fit the
  workflow, and compare retained revisions. On phones, a revision selector replaces
  the revision sidebar. A keyboard-accessible component list supplements the diagram.
- **Evaluations / Versions:** inspect and filter the latest 50 stored records, with
  10 rows per page. Versions has its own explicit harness-family selector.

Loading, empty, and unavailable states are distinct. Failed reads offer Retry;
connection failures disable analysis and show a recovery action. Primary actions,
neutral surfaces, semantic status badges, focus indicators, and reduced-motion
support share a single CSS token system. Tab groups support Left/Right and Home/End.

## Evidence and execution

Analyses use the same trusted execution path as `self-heal run`. Operator dataset
selection excludes generated evaluation and final assessment tables. Activated
versions execute their pinned commit in Docker. New runs store bounded, redacted
row snapshots and tool calls; older records explicitly explain missing evidence.
LangSmith links appear only when a verified HTTPS trace is available.

Evaluation results and resource values come from stored records. Missing pass/fail
results are shown as pending. Comparison bars show the proportion of expected
trials that passed. Source and structural diffs are read from existing API endpoints.
Promotion and rollback controls remain CLI operations.

## Verify

```sh
uv run --extra dev pytest -q
npm ci --prefix ui
npm test --prefix ui
```

The frontend tests use jsdom only as a development dependency. They cover routing,
draft preservation, keyboard submission and tabs, filtering and pagination,
connection failures, evolution rejection/discovery failure, workflow inspection,
and stale asynchronous responses. Python tests cover HTTP assets (including local
fonts and traversal rejection), evidence contracts, and trusted execution.

Geist fonts are distributed under the SIL Open Font License in `fonts/LICENSE.txt`.

## Record an evolution demo

The Playwright recorder follows the real UI and backend. It requires an answerable
question that the baseline cannot perform and a configured automatic evolution worker.
It records the incident, trace, proposal, protected trials, activation, saved workflow,
and automatic rerun. The UI now exposes the rerun's stored answer and an evidence link.
It never treats activation alone as proof that the original query succeeds.

```sh
npm ci --prefix ui
npm exec --prefix ui -- playwright install chromium
npm run --prefix ui record:demo -- \
  --question "YOUR BASELINE CAPABILITY-GAP QUESTION" \
  --dataset YOUR_OPERATOR_DATASET \
  --output ../artifacts/demo \
  --mode live
```

Output paths are resolved relative to `ui` when using the npm command. The output
contains `demo.webm`, an MP4 when FFmpeg is installed, `result.png`, and a compact
`evidence.json` with chapter timestamps and the recorded source/trial lineage.
Failed or rejected evolution produces `incomplete-demo.webm`, a failure screenshot,
and an evidence manifest instead of a successful demonstration.

`--mode scripted` adds a persistent **Scripted local demo · Synthetic data** label;
it does not simulate a backend or bypass evaluation. Such a recording requires a
separate controlled test backend. `--channel chrome` can use an installed Chrome
instead of bundled Chromium. The standard recorder configuration preserves the
production trace, resource, correctness, and promotion gates.

For an intentionally limited isolated baseline, pass `--baseline-note` with a
persistent, explicit description (for example, **Live evolution · Isolated
capability-gap baseline · Synthetic data**). Keep a clean committed copy and a
separate database; retain its baseline commit and provenance alongside the video.
The shipped logistics harness already answers the public threshold query, so
recording that query against the shipped version will correctly refuse to claim
that a failure-to-recovery experiment occurred.

During selection, the evaluation view resolves the already-persisted plan by
candidate ID and displays completed trials before a final decision is recorded.
Private case identifiers and diagnostics remain hidden in this partial view.

`--resume-job JOB_ID` captures a retained job's completed evaluations, activation,
and exact automatic rerun without submitting another query. It verifies that the
stored incident matches `--question` and applies the same acceptance and commit
attribution checks. If assembling captures, retain their original manifests and
record the join and any accelerated playback in the delivered evidence.

## Add ElevenLabs narration

Set `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, and `ELEVENLABS_MODEL` in the
ignored `.env`. A narration plan is a JSON list of `{start, end, text}` segments
aligned to the video's playback timestamps. Times are seconds.

```sh
uv run --frozen --env-file .env python scripts/narrate_demo.py \
  --video artifacts/demo/demo.mp4 \
  --plan artifacts/demo/narration-plan.json \
  --output artifacts/demo/demo-narrated.mp4
```

The narrator uses the configured ElevenLabs voice, caches speech with timing,
normalizes audio, and adds optional English captions. It preserves the video
stream exactly and refuses overlapping windows or speech that would require
excessive acceleration or truncation. The `narration/` folder retains individual
speech segments, the audio master, captions, timing, and transcript. Credentials
remain in the environment. Use a separate output path to preserve the silent cut.
