# PipelineAI Guardrail — Backend

Terminal-first guardrail that analyzes a GitHub Pull Request and returns a
deterministic ALLOW/BLOCK risk verdict, before the PR reaches a human reviewer or
a CI runner. Two entrypoints over one pipeline:

- **`pipelineai` CLI** — the primary interface (`pipelineai check owner/repo 142`)
- **`POST /webhook`** — the FastAPI server GitHub calls in production
  (`python -m pipelineai webhook`)

**No frontend, no user login/session/JWT.** The only credential is the
server-side `GITHUB_TOKEN` for the GitHub REST API.

## Architecture

```
pipelineai check owner/repo N        GitHub PR webhook (POST /webhook)
        │                                    │  HMAC-SHA256 signature verified
        │                                    │  X-GitHub-Delivery de-duplicated
        └──────────────┬─────────────────────┘
                       ▼
   GitHubClient.get_pull_request_files()        (real GitHub REST call)
                       ▼
   ┌─ ImpactAgent      AST parse of added lines → touched functions/classes;
   │                   critical-path detection (auth/config/parser/…)
   ├─ SimulationAgent  10 deterministic rules over added diff lines
   │                   (eval/exec, shell=True, SQL build, pickle/yaml.load,
   │                    verify=False, hardcoded secret, weak hash, bare except…)
   ├─ MemoryAgent      JSON-file regression lexicon; counts how often each
   │                   (category, rule) has BLOCKed a PR before
   └─ RiskAgent.synthesize()
                       │  compute_risk_score() → 0-100  (transparent weighted sum)
                       ▼
             score ≥ 70  →  BLOCK          score < 70  →  ALLOW
                       ▼
   GitHubChecksAPI.publish_verdict()            (real commit-status API call)
                       ▼
   [BLOCK only, detached] MemoryAgent.store_failure() + AIRemediation.suggest_fix()
```

The gate is **always deterministic** — `compute_risk_score` is a pure function of
the analyzer outputs, and the LLM runs *after* the verdict is published. A flaky
or absent LLM never changes ALLOW/BLOCK; the remediation just falls back to each
rule's own recommendation text.

The risk score, the threshold, and exactly what each analyzer does and doesn't do
are spelled out under **[What the analysis is — and is not](#what-the-analysis-is--and-is-not)**.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows  (source .venv/bin/activate on POSIX)
pip install -r requirements.txt
pip install -e .              # optional — puts `pipelineai` on PATH
```

Copy `.env.example` to `.env` (see the env-var table below).

## CLI

```bash
pipelineai check   owner/repo 142         # fetch a real PR, run the pipeline, show the verdict panel
pipelineai check   --local path/to/file   # analyze a local file as an all-added diff (offline)
pipelineai check   owner/repo 142 --post  # also publish the commit status to GitHub
pipelineai analyze owner/repo 142         # same analysis, JSON output (for scripts / CI)
pipelineai webhook                        # run the FastAPI webhook server
pipelineai benchmark                      # measured latency of the verdict path
pipelineai doctor                         # live preflight: GitHub auth, token scope, LLM reachability
pipelineai config                         # effective config, secrets redacted
pipelineai version
```

`check` / `analyze` exit **0** on ALLOW, **1** on BLOCK, **2** on ERROR — usable
as a CI gate. (Without `pip install -e .`, use `python -m pipelineai …`.)

### Webhook server

```bash
python -m pipelineai webhook              # binds 0.0.0.0:$PORT (default 8000)
```

For local end-to-end testing, tunnel it to GitHub:
`npx smee-client --url https://smee.io/<id> --target http://127.0.0.1:8000/webhook`
then point a repo webhook (Pull requests events, content type `application/json`,
secret = your `WEBHOOK_SECRET`) at the Smee URL.

## Environment variables

| Variable | Required? | Used by | If missing |
|---|---|---|---|
| `WEBHOOK_SECRET` | **Yes in production** (any public URL) | `verify_signature` in `guardrail_main.py` | **Demo mode**: signatures not verified, startup WARNING logged, `/health` reports `webhook_signature_enforced: false`. Never silently "secure". |
| `GITHUB_TOKEN` | **Yes to post commit statuses**; optional for public-repo diff fetch | `GitHubClient` (`Authorization` header) | Public-repo diffs still fetch (60 req/hr). `set_commit_status` gets 403 → verdict computed & logged but **not posted to the PR**. Private repos → 404 → error status. |
| `OPENAI_API_KEY` | No | `AIRemediation` | BLOCK verdicts still fire; the remediation suggestion falls back to each analyzer's own `recommendation` text instead of an LLM-authored one. |
| `OPENAI_BASE_URL` | No | `AIRemediation` (OpenAI-compatible endpoint override) | Defaults to OpenAI's endpoint. |
| `OPENAI_MODEL` | No | `AIRemediation` | Defaults to `gpt-4o-mini`. |
| `AGENT_TIMEOUT_SECONDS` | No | `process_guardrail` | Defaults to `10`. Non-numeric value → falls back to `10`. |
| `ALLOWED_ORIGINS` | No | CORS middleware in `guardrail_main.py` | No CORS granted (fine — the webhook flow is server-to-server). |
| `PORT` | No (injected by Render/most PaaS) | Dockerfile `CMD`, `pipelineai webhook`, `__main__` | Falls back to `8000`. |

The CLI reads the identical variables (via `pipelineai/config.py`), so `pipelineai check`
and the webhook server behave the same.

`GITHUB_TOKEN` is **not** needed to *receive* a webhook — only to fetch diffs
(private repos / rate limits) and to post the resulting status.

## Deploy to Render (Docker)

`backend/Dockerfile` builds a slim, non-root image whose `CMD` is
`python -m pipelineai webhook --host 0.0.0.0 --port $PORT` (Render injects `PORT`).

- **Blueprint:** the repo-root `render.yaml` defines the service
  (`dockerfilePath: backend/Dockerfile`, `dockerContext: backend`,
  `healthCheckPath: /health`). Render dashboard → New → Blueprint.
- **Manual:** New → Web Service → this repo → Runtime **Docker**, Root Directory
  **`backend`**, Health Check Path **`/health`**. Add the env vars above in the
  Environment tab.
- GitHub webhook → `https://<service>.onrender.com/webhook`, content type
  `application/json`, secret = your `WEBHOOK_SECRET`, event = *Pull requests*.

The regression lexicon (`backend/data/regression_lexicon.json`) lives on the
container's ephemeral disk — it survives restarts within a deploy but resets on
redeploy. Fine for a demo; attach a Render disk if you need it to persist.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v
```

52 tests. GitHub and OpenAI are mocked at the network boundary — nothing in the
suite makes a real external API call. Coverage: HMAC verification, delivery-ID
de-duplication, the AST impact analysis, all 10 Simulation rules, the regression
lexicon (determinism + simulated-restart persistence), `compute_risk_score`, the
full `AnalysisPipeline` (per-stage timing is real and asserted), the OpenAI
client's degradation paths, and the CLI (`check` exit codes, JSON schema, secret
redaction).

For a live check against real services: `pipelineai doctor`.

## What the analysis is — and is not

- **The four "agents" are four Python classes** (`ImpactAgent`, `SimulationAgent`,
  `MemoryAgent`, `RiskAgent`) run in a fixed sequence by one orchestrator. They do
  not plan, use tools, or talk to each other — "agent" here means "a bounded
  analyzer with one job," not an LLM agent.
- **"Semantic analysis" = `ast.parse` of the added diff lines** to extract the
  function/class names a change defines, plus critical-path keyword matching on
  file paths. It is not data-flow, taint, or type analysis.
- **"Adversarial testing" = 10 deterministic regex rules** over added lines. **No
  submitted code is ever executed.** It catches the code *shapes* that fail under
  hostile input (injection sinks, unsafe deserialization, disabled TLS, …); it is
  a targeted linter, not a fuzzer or a sandbox.
- **"Regression detection" = an occurrence counter** keyed by `(category, rule_id)`,
  persisted to JSON. The Memory Agent recognizes a pattern only after a prior PR
  was BLOCKed on it. It does not train a model.
- **AI is not in the decision.** `compute_risk_score` → threshold is the whole
  gate. The LLM only writes the remediation paragraph attached to a BLOCK, after
  the verdict is already published.

## Known limitations

- **No sandboxed code execution** (see above).
- **AST parsing only succeeds when a diff hunk is syntactically standalone.**
  Partial hunks fall back to filename heuristics; `ImpactAgent` records
  `ast_parseable` per file so this is visible, not hidden.
- **Regex rules have false negatives** (e.g. multi-line injection sinks,
  indirection through helpers) and can have false positives on unusual code.
- **No database.** Regression history is one JSON file, single-writer.
- **Delivery de-dup is in-memory and per-process** — a redeploy or a second
  instance forgets recent delivery IDs. HMAC is the real anti-forgery control.
- **Unauthenticated webhooks if `WEBHOOK_SECRET` is unset.** Set it for any
  internet-reachable deployment.
