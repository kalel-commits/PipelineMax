# PipelineAI Guardrail — Backend

FastAPI service that intercepts GitHub Pull Request webhooks and returns a
deterministic ALLOW/BLOCK verdict as a commit status, before the PR reaches a
human reviewer or a CI runner.

**This service is headless — there is no frontend.** It exposes `GET /`,
`GET /health`, and `POST /webhook`. You "use" it by pointing a GitHub webhook at
`POST /webhook` and watching the commit status appear on your PRs. There is no
user login/session/JWT anywhere in the codebase — the only credential is the
server-side `GITHUB_TOKEN` used to call the GitHub REST API.

## Architecture

```
GitHub PR event (webhook)
        │  HMAC-SHA256 signature verified
        ▼
WebhookHandler.handle_pr_event()
        │  GitHubClient.get_pull_request_files()  (real GitHub REST call)
        ▼
RiskAgent.evaluate_pr()
        ├── ImpactAgent      — AST-based semantic blast-radius analysis
        ├── SimulationAgent  — deterministic static "adversarial" pattern scan
        └── MemoryAgent      — persistent (JSON-file) regression lexicon
        │
        ├── ALLOW  → done
        └── BLOCK  → AIRemediation.suggest_fix() (OpenAI, optional)
                      grounded strictly in the issues the static agents found
        ▼
GitHubChecksAPI.publish_verdict()  (real GitHub commit-status API call)
```

The ALLOW/BLOCK gate is always deterministic — it never depends on the LLM.
`AIRemediation` only enriches the human-readable explanation attached to a
BLOCK, and gracefully falls back to the static analyzers' own recommendation
text when no `OPENAI_API_KEY` is configured, or when the OpenAI call fails,
times out, or returns something unparseable.

## What "adversarial testing" means here

This does **not** execute submitted PR code. Running arbitrary GitHub PR code,
even sandboxed, is a real security undertaking (container isolation, network
denial, resource limits) that's out of scope for this project. Instead,
`SimulationAgent` deterministically scans the diff for code shapes that are
known to fail under malformed/hostile input — bare `except:`, `eval`/`exec`,
shell/SQL injection patterns, unchecked payload indexing — so the same input
always produces the same verdict.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:
- `GITHUB_TOKEN` — needed to fetch PR files and post commit statuses
- `WEBHOOK_SECRET` — required for signature verification (without it, incoming
  webhooks are accepted unverified — fine for local testing, not for production)
- `OPENAI_API_KEY` — optional; enables AI-generated remediation suggestions

Run the server:
```bash
python guardrail_main.py
# or: uvicorn guardrail_main:app --reload
```

Expose it to GitHub via a tunnel (e.g. `npx smee-client --url https://smee.io/<id> --target http://127.0.0.1:8000/webhook`)
and point a repository webhook at that Smee URL for `pull_request` events.

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
| `PORT` | No (injected by Render/most PaaS) | Dockerfile `CMD`, and `__main__` | Falls back to `8000`. |

`GITHUB_TOKEN` is **not** needed to *receive* a webhook — only to fetch diffs
(private repos / rate limits) and to post the resulting status.

## Deploy to Render (Docker)

`backend/Dockerfile` builds a slim, non-root image whose `CMD` binds
`uvicorn` to `0.0.0.0:$PORT` (Render injects `PORT`).

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

All GitHub and OpenAI calls are mocked at the network boundary in tests —
nothing in the test suite makes a real external API call. `ImpactAgent`,
`SimulationAgent`, and `MemoryAgent` are tested directly against crafted diffs
with real assertions on the output, including a determinism check (same input
→ identical output across repeated calls) and a persistence check (fresh
`MemoryAgent` instance over the same file simulates a process restart).

## Known limitations

- **No sandboxed code execution.** "Adversarial testing" is static pattern
  matching on the diff, not real execution against injected malformed input.
- **AST parsing only succeeds when a diff hunk happens to be syntactically
  standalone.** Partial hunks fall back to the filename/pattern heuristics;
  `ImpactAgent` records `ast_parseable` per file so this is visible, not hidden.
- **No database.** Regression history is a single JSON file
  (`backend/data/regression_lexicon.json`), sufficient for a single-instance
  deployment; it is not safe for concurrent writers.
- **Unauthenticated webhooks if `WEBHOOK_SECRET` is unset.** Set it in any
  deployment reachable from the internet.
