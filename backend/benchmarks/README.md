# Guardrail performance benchmark

Measures the full production webhook-to-verdict path:

```
webhook received -> HMAC SHA-256 verify -> JSON parse -> process_guardrail()
  -> WebhookHandler.handle_pr_event
     -> GitHubChecksAPI.set_pending          (GitHub commit-status POST)   ┐ overlapped
     -> GitHubClient.get_pull_request_files   (GitHub PR-files GET)         ┘
     -> RiskAgent.evaluate_pr
        -> ImpactAgent / SimulationAgent / MemoryAgent
        -> [BLOCK only] MemoryAgent.store_failure + AIRemediation.suggest_fix (LLM)
     -> GitHubChecksAPI.publish_verdict       (GitHub commit-status POST)
```

Every function is the real one imported from `guardrail_main`. Nothing is
reimplemented. The only thing that varies per mode is whether the GitHub and LLM
**network** calls are real or replaced by a zero-latency in-process stub — and
each report states which. No mode inserts artificial sleeps or fabricated
timings.

## Run

```bash
cd backend
# local compute only (no network) + local end-to-end through the real ASGI app
python -m benchmarks.bench_guardrail --mode local  --rounds 15

# real api.github.com (needs GITHUB_TOKEN in backend/.env)
python -m benchmarks.bench_guardrail --mode github --rounds 12

# real LLM remediation call on BLOCK verdicts (needs OPENAI_API_KEY[, OPENAI_BASE_URL, OPENAI_MODEL])
# --llm-pace 14 keeps calls under Gemini's free-tier 5 req/min limit
python -m benchmarks.bench_guardrail --mode openai --llm-rounds 2 --llm-pace 14

# everything
python -m benchmarks.bench_guardrail --mode all

# A/B: current pooled-connection + overlapped-call path vs the previous implementation
python -m benchmarks.compare_github --rounds 10

# live Smee.io tunnel delivery check (needs node/npx)
python -m benchmarks.check_smee                       # no webhook secret (documented Smee local-testing setup)
SMEE_CHECK_USE_SECRET=1 python -m benchmarks.check_smee   # shows Smee's JSON-reserialization HMAC caveat
```

## Modes

| mode | GitHub calls | LLM call | what it isolates |
|---|---|---|---|
| `local` (Mode 1) | in-process stub | fallback (no key) | guardrail compute: HMAC, parse, 4 agents, lexicon I/O, synthesis |
| `local` ASGI (Mode 1b) | in-process stub | fallback | Mode 1 + real Starlette request/response + BackgroundTask |
| `github` (Mode 2) | **real api.github.com** | fallback | + real GitHub retrieval and commit-status round trips |
| `openai` (Mode 3) | in-process stub | **real** | full BLOCK analysis including one real remediation LLM call |

`results/` holds the captured runs referenced by `BENCHMARK_REPORT.md`.

## Corpus

`fixtures.py` — 24 representative PR webhook payloads spanning the verdict space
(docs/deps/tests/refactors -> ALLOW; bare-except / eval / shell=True / SQL
f-string / unchecked-payload, some in `auth`/`config`/`parser` critical paths ->
BLOCK) and the diff-size space (1 file to ~40 files). `REAL_PRS` lists the real
public merged PRs used for the api.github.com round trips.
