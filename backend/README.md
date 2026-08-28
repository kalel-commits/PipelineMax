# PipelineAI Guardrail — Backend

FastAPI service that intercepts GitHub Pull Request webhooks and returns a
deterministic ALLOW/BLOCK verdict as a commit status, before the PR reaches a
human reviewer or a CI runner.

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
