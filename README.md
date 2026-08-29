# PipelineAI — Multi-Agent PR Guardrail

A terminal-first guardrail that analyzes a GitHub Pull Request and returns a
deterministic **ALLOW / BLOCK** risk verdict as a commit status — before the PR
reaches a reviewer or a CI runner.

```
$ pipelineai check acme/payment-service 142

┌──────────────── PipelineAI  •  Multi-Agent PR Guardrail ────────────────┐
│  PR #142  •  feature/payment-validation                                 │
│  repository: acme/payment-service                                       │
│                                                                        │
│  AGENTS                                                                 │
│    ✓ Impact Agent       17 files · 43 fns · AST 16/17          84 ms    │
│    ✓ Simulation Agent   10 patterns · 612 lines · 2 findings   31 ms    │
│    ✓ Memory Agent       14 known patterns · 1 match             3 ms    │
│    ✓ Risk Agent         score 82/70 → BLOCK                     7 ms    │
│                                                                        │
│  RISK VERDICT   ● BLOCK    ████████████████████░░░░  82/100             │
│                                                                        │
│  FINDINGS                                                               │
│    HIGH   sql_dynamic_query   SQL query built via f-string   db.py:41   │
│    HIGH   memory_match        Regression pattern seen 3x     auth.py    │
└────────────────────────────────────────────────────────────────────────┘
```

*(numbers above are illustrative; run `pipelineai check --local examples/vulnerable_sample.py` for a real one)*

---

## What it actually does

| Stage | Implementation | Not |
|---|---|---|
| **Impact Agent** | `ast.parse()` of the added diff lines → the functions/classes a change defines; critical-path keyword match on file paths | data-flow / taint / type analysis |
| **Simulation Agent** | 10 deterministic regex rules over added lines (`eval`/`exec`, `shell=True`, SQL string-building, `pickle`/`yaml.load`, `verify=False`, hardcoded secret, weak hash, bare `except`, unchecked payload access) | executing submitted code; fuzzing; a sandbox |
| **Memory Agent** | occurrence counter keyed by `(category, rule_id)`, persisted to a JSON file; flags a pattern that BLOCKed a prior PR | a trained model |
| **Risk Agent** | `compute_risk_score()` — a transparent weighted sum (0–100) of the above → `BLOCK` at ≥ 70 | an LLM making the decision |

**The gate is 100% deterministic.** The optional LLM (`OPENAI_API_KEY`, any
OpenAI-compatible endpoint) only writes the remediation paragraph attached to a
BLOCK — *after* the verdict is published — and degrades to each rule's own
recommendation text if it's absent or fails.

The four "agents" are four Python classes run in a fixed sequence by one
orchestrator (`pipelineai/pipeline.py`). They don't plan, use tools, or message
each other — "agent" here means "a bounded analyzer with one job."

---

## Quick start

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate     # source .venv/bin/activate on POSIX
pip install -r requirements.txt
pip install -e .                                      # puts `pipelineai` on PATH (optional)
cp .env.example .env                                  # then fill in what you need

pipelineai check --local examples/vulnerable_sample.py   # offline BLOCK demo
pipelineai check --local examples/safe_sample.py          # offline ALLOW demo
pipelineai check pallets/flask 6144                       # real PR from GitHub
pipelineai doctor                                         # live preflight checks
```

`check` / `analyze` exit **0** ALLOW · **1** BLOCK · **2** ERROR — drop it into CI.

### As a webhook server (production)

```bash
python -m pipelineai webhook          # FastAPI on 0.0.0.0:$PORT
```

GitHub → repo Settings → Webhooks → `https://<host>/webhook`, content type
`application/json`, secret = your `WEBHOOK_SECRET`, events = *Pull requests*.
The server verifies `X-Hub-Signature-256` (HMAC SHA-256), de-duplicates on
`X-GitHub-Delivery`, returns `200` immediately, and analyzes in the background.

---

## Deploy (Render)

Docker image, binds `$PORT`, non-root, health-checked at `/health`.

1. Render → New → **Blueprint** → this repo ([`render.yaml`](render.yaml)) — or
   New → Web Service → Docker, Root Directory `backend`, Health Check `/health`.
2. Environment tab: `WEBHOOK_SECRET` (required for a public URL), `GITHUB_TOKEN`
   (required to *post* statuses — needs "Commit statuses: write"), optional
   `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`. **Do not set `PORT`.**

Full env-var table with behavior-if-missing: [backend/README.md](backend/README.md#environment-variables).

There is **no frontend** — this is a headless CLI + webhook service.

---

## Testing

```bash
cd backend
pip install -r requirements-dev.txt
pytest -q          # 52 tests
pipelineai doctor  # live: GitHub auth + token scope + LLM reachability
```

GitHub and OpenAI are mocked at the network boundary in the unit suite. Per-stage
timings in `AnalysisPipeline` are real `perf_counter` measurements and are
asserted (`verdict_ms == sum(stage timings)`). See
[backend/README.md](backend/README.md) for the architecture, the risk-score
formula, and known limitations.
