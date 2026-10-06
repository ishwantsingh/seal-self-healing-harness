# Self-Heal local UI

This interface is a local operator surface for the implemented system. It uses the same
trusted execution path as `self-heal run`: an analysis reads only its automatically selected
operator Atlas dataset, records compact run history, and optionally links a verified LangSmith
root trace. Generated evaluation tables are excluded from automatic selection. Once a version
is active, both surfaces execute that pinned commit in Docker.

The data-source selector offers the public logistics bundle and inventory tables. If no logistics
bundle exists, **Load logistics demo** materializes the immutable public fixture (8 customers,
3 warehouses, 74 shipments). The logistics question uses that bundle and records the baseline
capability gap until an evaluated logistics harness is available. The single-line question field
remains visible on Ask, Runs, Evaluations, Versions, and run detail
pages. It opens suggested questions on focus. Use arrow keys and Enter to choose one, or Escape to
close the list. Recent runs open a detailed evidence view with Atlas data, Tool calls, and Trace tabs.

Start it from the repository root after configuring `.env` and materializing a dataset:

```sh
uv run --env-file .env self-heal seed --fixture evals/analyst/data/small_inventory.json
uv run --env-file .env self-heal ui
```

Open [http://127.0.0.1:4173](http://127.0.0.1:4173). The server listens on loopback by default;
use `--host` and `--port` only when an explicitly different local setup is required.

New runs store bounded, redacted snapshots of rows actually read and tool calls actually made.
The evidence view shows those rows by read page, tool arguments and result previews, resource
counts, an execution timeline, and LangSmith span metadata when tracing is available. Raw payloads
are collapsed. The Evaluations page shows recorded protected trials and one-use final assessments;
the Versions page shows retained commits and the active version.
Older run records without row snapshots explicitly say so; available LangSmith traces can
still supply redacted tool-call details. Capability gaps link to recorded
evaluation cases and candidate diffs when present; correctness and regression status use the
stored selection trials. Candidate proposal, protected evaluation, promotion, and rollback
controls remain CLI operations.
