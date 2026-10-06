# Reviewing and developing Self-Heal

## A focused review path

1. Read the README's evaluation process and `config/analyst.yaml` to understand the supported task contract, fixed budgets, and acceptance thresholds.
2. Inspect `evals/analyst/oracle.py` and `evals/logistics/oracle.py`. Ground truth must be independent of candidate tools. Follow the corresponding generators and hand-checked tests.
3. Trace `controller.py` → `repository.py` → `runner.py` → `evaluation.py` → `promotion.py` in `src/self_heal/`. Look for frozen case/source identities, patch scope, external grading/counters, complete trial retention, and conditional activation.
4. Read `final_assessment.py` and its tests. Verify reserve-before-selection, exclusion of final datasets, claim-before-run, one-use assessment, and retained failures.
5. Check `table_store.py`, `logistics_store.py`, `telemetry.py`, and `runner_support/` for scoped access, bounded reads, redaction, and container/host trust boundaries.

The two deliberately separate trees named `self_heal` serve different roles: `src/self_heal/` is the full trusted host application; `runner_support/self_heal/` contains only the minimal runtime interfaces copied into the candidate container. Do not replace the latter with the host package.

## Local checks

```sh
uv sync --frozen --extra dev
uv run --frozen pytest -q
uv run --frozen self-heal --help
```

For real-container checks, start Docker, build the image, then enable the opt-in suite as described in the README. Mocked subprocess tests verify bridge behavior, but do not establish Docker isolation.

Keep changes focused. Tests should cover behavior and the relevant trust boundary. Update setup/docs when a contract, command, configuration key, or acceptance rule changes. After dependency changes, regenerate `uv.lock` and verify `uv sync --frozen --extra dev`.

## Protected changes and evidence

Generated proposals may change only `harness/`. Human-reviewed supervisor, contract, generator, and oracle extensions are a separate step; establish independent ground truth before comparing a candidate against a new capability. Keep refusal cases for behavior still outside scope.

Do not weaken gates or erase failures to demonstrate improvement. Keep all comparative trials, distinguish mocked checks from live experiments, and distinguish reviewed capabilities from model-generated promotions. Final feedback used to guide a new patch requires new untouched final cases.

Keep API keys, filled environment files, database exports, detailed service traces, local candidate worktrees, and private final manifests out of commits and review attachments. Use synthetic fixtures and redacted summaries to reproduce issues.
