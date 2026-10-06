# Setup

This guide configures the implemented application. Run all commands from the repository root; configuration, evaluation scenarios, prompts, and local source versions are resolved there.

## Local prerequisites

- Python 3.11 or newer, Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- A reachable MongoDB Atlas cluster for live data and history.
- An OpenRouter API key and a model that supports tool calling for live inventory questions.
- LangSmith credentials and verified tracing for candidate selection.
- Docker with a running daemon for candidate execution, promoted versions, and real-container tests.

The local Python app runs on the host. The Dockerfile builds the restricted candidate runtime; it is not an image for the UI or the full application.

```sh
uv sync --frozen --extra dev
uv run --frozen pytest -q
cp .env.example .env
```

Edit `.env` locally. `uv run --env-file .env` loads it; the application does not automatically read that file. Environment variables can also be supplied by your shell. Never commit a filled environment file.

## Environment variables

| Variable | Required for | Value |
| --- | --- | --- |
| `ATLAS_URI` | Live commands | Your cluster connection URI; blank in the template |
| `ATLAS_DATABASE` | Live commands | A new database for this checkout, default `self_heal_review` |
| `OPENROUTER_API_KEY` | Live model calls | Your provider key; blank in the template |
| `OPENROUTER_AGENT_MODEL` | Inventory agent and comparisons | A tool-calling model ID available to your account |
| `OPENROUTER_EVOLUTION_MODEL` | Evolution | Model ID for diagnosis, scenario, and patch proposals |
| `LANGSMITH_TRACING` | Tracing/evolution | `false` for basic setup; `true` for evidence used by selection |
| `LANGSMITH_API_KEY` | Enabled tracing | Your LangSmith key; blank in the template |
| `LANGSMITH_PROJECT` | Tracing | Consistent project name, default `self-heal-review` |
| `LANGSMITH_WORKSPACE_ID` | Multi-workspace keys, when needed | Optional workspace ID |
| `SELF_HEAL_RUNNER_IMAGE` | Candidate runner | Default `self-heal-runner:local` |

Keep the agent model and configuration unchanged between baseline and candidate trials. A changed model, config, or Docker image invalidates comparison assumptions and may block final assessment.

## MongoDB Atlas

Create a database user with the access needed to read and write the chosen application database. Allow your machine's IP in Atlas's network access settings. Copy the cluster's Python driver URI and fill its password locally; URI-encode special characters in credentials.

Use a new database for this repository. Reusing the original application's history can point to active commits that are absent from the new Git history. Dataset and history collections/indexes are created by the application. Fixture publication is immutable: reseeding verifies the existing row count and hash rather than overwriting data.

```sh
uv run --frozen --env-file .env self-heal seed --fixture evals/analyst/data/small_inventory.json
uv run --frozen --env-file .env self-heal seed-logistics
```

## OpenRouter and LangSmith

Choose a tool-calling agent model and provide its ID and API key. Evolution may use a different model from the agent, but the agent's identity stays fixed during comparison.

Basic task runs permit disabled tracing. For evolution, set `LANGSMITH_TRACING=true`, provide the LangSmith key, and use one consistent project. If a key belongs to multiple workspaces, set the optional workspace ID. A trace marked `incomplete` is missing evidence even if the answer itself is correct. Selection requires verified traces under the default configuration.

Credentials remain on the host; the candidate container obtains model and bounded dataset operations through the trusted bridge.

## Run and inspect

```sh
uv run --frozen --env-file .env self-heal run --dataset small-inventory-v1 \
  --question "How many available units are in the East warehouse?" --json
uv run --frozen --env-file .env self-heal ui
```

Open [http://127.0.0.1:4173](http://127.0.0.1:4173). The server is for local use. For evaluation, evolution, assessment, and rollback commands, follow the [README](README.md#evaluation-process).

Before evolving, confirm a clean source commit and build the candidate image:

```sh
git status --short
uv run --frozen --env-file .env self-heal runner build
```

Commit any reviewed source changes before running evolution. Ignore secrets and runtime artifacts. The supervisor needs Git commit/worktree operations; it does not require a GitHub remote or token.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing configuration error | Run with `--env-file .env` and fill the relevant blank variables |
| Atlas timeout/authentication error | IP allowlist, database user permissions, URI password encoding, and cluster availability |
| Model error or unsupported tool calls | Agent model ID, tool support, provider key, and available credits |
| `trace.status=incomplete` | LangSmith key/project/workspace and service reachability; inspect actual evidence before evolution |
| Cannot connect to Docker | Start Docker; verify its daemon is reachable before building/running |
| Evolution blocked on source identity | Clean committed checkout and a fresh database with commits from this repository |
| Baseline command exits 0 after a budget failure | Inspect its declared baseline expectation; matching an expected failure is not task correctness |
| Final manifest exists or case already consumed | Use a new reservation path; consumed final cases cannot be retried as untouched |

Tests need no live credentials. Live service and model runs are separate checks and can consume provider credits.
