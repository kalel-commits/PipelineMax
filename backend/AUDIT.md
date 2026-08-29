# PipelineAI — Reality Audit

Written after **running** the system (52 tests, the CLI against real GitHub PRs,
the webhook endpoint, the benchmark, live GitHub + Gemini calls), not just reading
it. Verified against commit `d8c097a`.

## TL;DR — how real is this?

**It is a real, working, deterministic static-analysis gate for GitHub PRs with a
polished CLI.** Every piece of the pipeline runs and does what it says. The
weak points are in the *framing*, not the code:

- The "4 agents" are 4 plain classes in a fixed sequence. No autonomy, no LLM in the loop.
- "Semantic analysis" is `ast.parse` of added lines + filename keyword matching.
- "Adversarial testing" is 10 regex rules on the diff. No code is executed.
- "Regression detection" is an occurrence counter in a JSON file.
- The LLM never touches the ALLOW/BLOCK decision.
- The "<2 s" is real for the **verdict** (p95 ~1.25 s incl. real GitHub API); it is
  **not** true once the LLM remediation is included (~+1.5 s), which is why that
  step was moved off the verdict path.
- The "30–40 minute" comparison is an assertion, not a measurement.

## What was verified live (with the repo's own credentials)

| Capability | Result |
|---|---|
| `pytest` | **52 passed** |
| `pipelineai check pallets/flask 6144` (real PR) | fetched 4 files in ~1.4 s → ALLOW, score 0 |
| `pipelineai check psf/requests 6963` (real CVE-fix PR) | fetched 2 files → ALLOW (patch is test-only, AST 0/2) |
| `pipelineai check --local examples/vulnerable_sample.py` | **BLOCK, score 100/100, 7 findings** across 6 rules |
| `pipelineai check --local examples/safe_sample.py` | ALLOW, score 12 (critical-path hit, no findings) |
| Real LLM remediation (Gemini `gemini-flash-lite-latest`) | `source=openai`, grounded fixes, ~1.8–3.0 s |
| HMAC verification (live uvicorn) | no-sig → 401, bad-sig → 401, valid HMAC → 200 |
| Replayed webhook (same `X-GitHub-Delivery`) | 2nd call → `{"status":"duplicate"}`, pipeline ran once |
| GitHub token — identity | `kalel-commits`, authenticates in ~0.5–1.0 s |
| GitHub token — **commit-status write** | **403 — token lacks "Commit statuses: write"** |
| Benchmark — local verdict path | p50 0.6 ms · p95 2.4 ms · p99 3.2 ms (n=144) |
| Benchmark — GitHub-inclusive verdict | p50 ~0.8 s · p95 ~1.25 s · p99 ~1.7 s (n=30) |

## What could NOT be verified

- **Posting a real ALLOW/BLOCK commit status to a PR.** The provided `GITHUB_TOKEN`
  is a fine-grained PAT without the "Commit statuses: write" permission; every
  `POST /statuses/{sha}` returns 403. The code path is exercised and the 403 is
  handled (verdict still computed and logged), but a real `201 Created` was never
  observed. → regenerate the token with that permission, or run
  `pipelineai check --post` after doing so.
- **`docker build`.** No Docker daemon on this machine. The Dockerfile is standard
  and the entrypoint (`python -m pipelineai webhook`, `$PORT` binding) was verified
  by running it directly.

---

## Interviewer simulation — 30 questions, answered from the code

**1. Show me exactly where the four agents run.**
`pipelineai/pipeline.py::AnalysisPipeline._analyze_files` calls, in order,
`ImpactAgent.analyze` → `SimulationAgent.analyze` → `MemoryAgent.check_lexicon` →
`RiskAgent.synthesize`. The webhook path (`integrations/webhook_handler.py::handle_pr_event`)
calls `RiskAgent.decide`, which runs the same three analyzers then the same
`synthesize`. One test (`test_pipeline_and_riskagent_agree`) asserts both paths
return the identical verdict.

**2. Why are these agents actually agents rather than four Python classes?**
They are four Python classes. There is no planning, no tool use, no message
passing, no LLM. "Agent" here is a naming choice for "a bounded analyzer with one
responsibility." In an interview call them **analyzers** or **stages**; do not
imply autonomous/LLM agents.

**3. Where is the AI?**
`utils/ai_remediation.py`. It is called **once, only on BLOCK, only after the
verdict is already published** (`webhook_handler._enrich`, a detached task). It
turns the already-found issues into a remediation paragraph. It cannot add,
remove, or change a finding or the verdict.

**4. What model are you using?**
Whatever `OPENAI_MODEL` says, via any OpenAI-compatible endpoint (`OPENAI_BASE_URL`).
This repo is configured for Gemini `gemini-flash-lite-latest`. With no key it uses
a deterministic fallback built from each rule's `recommendation` string.

**5. What does semantic analysis actually mean in your implementation?**
`impact_agent.py`: it takes the added (`+`) lines of each `.py` file's diff hunk,
runs `ast.parse` on them, and `ast.walk`s for `FunctionDef` / `AsyncFunctionDef` /
`ClassDef` names — "which functions/classes does this change define". Plus a
substring match of the filename and those function names against
`["auth","parser","config","security","payment","token","session"]`. It is **not**
data-flow, taint, call-graph, or type analysis. When the hunk isn't standalone-
parseable it returns `[]` and records `ast_parseable: false` for that file
(honest, visible — `pipelineai check` prints "AST 1/3").

**6. How do you detect regressions?**
`memory_agent.py`: on a BLOCK, `store_failure` writes an entry to
`data/regression_lexicon.json` keyed by `sha256("{category}:{rule_id}")[:16]`,
incrementing an `occurrences` count. `check_lexicon` looks up the current PR's
issues by the same key and returns a message if `occurrences >= 1`. It's an
**occurrence counter with a stable key**, persisted to JSON. No learning, no model.
It only recognizes patterns that previously caused a BLOCK.

**7. What exactly is adversarial testing?**
`simulation_agent.py`: 10 compiled regexes run against each added diff line.
Examples: `\b(eval|exec)\s*\(`, `subprocess\.\w+\([^)]*shell\s*=\s*True`,
f-string/format/concat containing a SQL keyword, `pickle\.loads?`,
`verify\s*=\s*False`. Each rule carries a severity (5–9) and a recommendation.

**8. Are you executing attacker-controlled code?**
No. Nothing from the PR is imported, `exec`'d, or run in a subprocess. The
`SimulationAgent` docstring and `backend/README.md` both state this explicitly.
That is the correct trade-off but means "adversarial *testing*" overstates it —
it's adversarial *pattern detection*.

**9. How do you prevent false positives?**
Weakly. Rules only match **added** lines (not context/removed). Severities are
tuned so a single medium finding (sev 6) doesn't cross the threshold. There is no
suppression/allowlist mechanism, no "// nosec"-style ignore, no confidence score
per finding. This is a real gap for production use.

**10. How is the risk score calculated?**
`agents/risk_agent.py::compute_risk_score` — a transparent weighted sum:
`base = max(finding severity) × 9` (0–81); `+4` per extra finding capped at 3
extras; `+12` if a critical path is hit; `+15` if a stored regression matches.
Then two floors: any severity ≥ 7 → score ≥ 75; critical-path + regression →
score ≥ 72. Clamped to 0–100.

**11. What makes a PR BLOCK rather than ALLOW?**
`score >= 70` (`BLOCK_THRESHOLD`). The floors mean this reproduces the guardrail's
two long-standing hard rules (any sev-7+ finding; or a regression match on a
critical-path change), and additionally blocks large pile-ups of medium findings.

**12. Where does HMAC verification happen?**
`guardrail_main.py::verify_signature` (`hmac.new(secret, body, sha256)` +
`hmac.compare_digest`), called in `handle_webhook` before any parsing. Verified
live: missing/invalid signature → 401. Skipped (with a startup WARNING and a
`/health` flag) only when `WEBHOOK_SECRET` is unset.

**13. What happens if GitHub is unavailable?**
`github_client.get_pull_request_files` raises `GitHubAPIError` (timeout / network /
4xx). `webhook_handler` catches it, publishes an `error` commit status, and
returns `{"verdict":"ERROR"}`. The CLI renders an ERROR panel and exits 2. It does
**not** fail open to ALLOW.

**14. What happens if the LLM is unavailable?**
`ai_remediation.suggest_fix` catches every exception and returns
`_fallback_suggestion` (source `fallback`) built from the rules' own text. The
verdict is unaffected — it was already published. Tested (`test_ai_remediation.py`,
5 degradation cases) and observed live (Gemini quota-exhaustion → clean fallback).

**15. What happens if the webhook is replayed?**
`guardrail_main._already_processed` keeps a bounded (2048) LRU of
`X-GitHub-Delivery` IDs. A repeat → `200 {"status":"duplicate"}`, pipeline not
re-run (`test_webhook_deduplicates_redelivered_delivery_id`). Note: an attacker
can change the delivery-ID header — **HMAC is the real anti-forgery control**;
dedup is idempotency + defense in depth, and is per-process (a redeploy forgets).

**16. How do you handle duplicate webhooks?**
Same as 15.

**17. How did you measure <2 seconds?**
`benchmarks/bench_guardrail.py` — `time.perf_counter()` around the real production
functions. `--mode local` stubs the network to isolate compute; `--mode github`
makes 3 real `api.github.com` round trips over a corpus of real public PRs. Full
methodology and raw runs in `benchmarks/BENCHMARK_REPORT.md`.

**18. Does the <2 seconds include GitHub network latency?**
The **verdict** figure does: GitHub-inclusive p50 ~0.8 s, p95 ~1.25 s, p99 ~1.7 s
(3 real round trips, from a high-RTT location — a US-region host would be faster).
The **local** figure (p95 ~2 ms) explicitly does not and is labelled as such.

**19. Does it include the LLM?**
No — and it must not be claimed to. The LLM remediation is ~1.5 s on its own and
runs **after** the verdict is published, as a detached task
(`webhook_handler._enrich`). Verdict + LLM together was ~2.3–5.6 s before that
split; that combined number is not < 2 s.

**20. What exactly took 30–40 minutes?**
Unspecified in the code. The intended meaning is "a full CI/integration-test run
before a human looks at the PR." Nothing in this project measures or reproduces
that number.

**21. Is the 30–40 minute number actually measured?**
No. It is an industry-typical figure asserted for contrast. Treat it as such.

**22. Can your system replace CI?**
No, and it doesn't try to. It runs no tests, no build, no type-check, no coverage.
It is a fast pre-review triage gate that runs *before* CI.

**23. Why isn't this just a linter?**
Largely it is — a diff-scoped one with a persistence layer and a GitHub
commit-status integration. The honest differentiators: it only looks at *added*
lines (PR-scoped), it keeps cross-PR memory of what previously blocked, and it
ships as a commit-status gate + CLI. It is not a novel analysis technique.

**24. How does the Memory Agent actually learn?**
It doesn't "learn" in an ML sense. `store_failure` appends/increments a JSON entry;
`check_lexicon` reads it back. "Institutional memory" = a dictionary on disk.

**25. Where is the historical data stored?**
`backend/data/regression_lexicon.json` (path configurable). Gitignored. On a
container it's ephemeral — resets on redeploy unless a volume is attached.

**26. Can you demonstrate a real malicious PR being blocked?**
Yes, offline and reproducibly: `pipelineai check --local examples/vulnerable_sample.py`
→ BLOCK, score 100, 7 findings (shell injection, eval, SQL f-string, pickle,
`verify=False`, unchecked payload, MD5). A branch `demo/injection-sample` is
pushed for a real GitHub-PR demo (open the PR, then `pipelineai check kalel-commits/PipelineMax <n>`).

**27. Can you demonstrate a real safe PR being allowed?**
`pipelineai check --local examples/safe_sample.py` → ALLOW. Also every real
public PR tested (`flask#6144`, `requests#6963`, …) → ALLOW.

**28. What happens with a large PR?**
`github_client.py` caps at `MAX_FILES_ANALYZED = 50` files and
`MAX_PATCH_CHARS = 4000` per file (truncated with a marker). Binary/vendored/
lockfiles are skipped. So a 500-file PR is analyzed as its first 50 analyzable
files — a real limitation, documented.

**29. What happens if AST parsing fails?**
`_touched_function_names` catches `SyntaxError`/`ValueError` and returns `[]`.
`ast_parseable` is recorded `false` for that file and shown in the CLI ("AST
1/3"). The `SimulationAgent` regexes still run (they don't need a parse), so
detection degrades gracefully rather than failing.

**30. What happens if the GitHub API rate limit is reached?**
`_raise_for_status` maps `403 + X-RateLimit-Remaining: 0` to `GitHubRateLimitError`
→ `webhook_handler` publishes an `error` status ("GitHub rate limit hit") and
returns ERROR. Authenticated = 5000 req/hr; the guardrail makes ≤ 3 calls/PR.

---

## Trying to disprove the résumé

> **"4-agent AI guardrail"** — The word **AI** is the stretch. No AI/LLM
> participates in the guardrail decision. An LLM writes an optional remediation
> note afterwards. Defensible phrasing: "rule-based guardrail with optional
> LLM-generated remediation."

> **"reviews GitHub Pull Requests using FastAPI, GitHub Webhooks, and
> HMAC-verified authentication"** — All true and verified. FastAPI app exists,
> webhook route exists, HMAC verified live. Only nuance: "authentication" is
> webhook *payload* authentication (HMAC), not user auth — fine, but be precise if
> asked.

> **"semantic code analysis"** — Overstated. It's `ast.parse` for
> function/class names + filename keyword matching. Say **"AST-based change
> analysis"**; if you say "semantic", immediately qualify it.

> **"regression detection"** — Real but modest: a persisted occurrence counter
> keyed by `(category, rule_id)`. Say **"tracks recurrence of previously-blocked
> issue patterns across PRs"**, not "detects regressions" (which implies behavioural
> diffing or test analysis).

> **"adversarial testing"** — Misleading. No testing, no execution. It's
> **"static detection of injection / unsafe-API patterns in the diff"**. Drop
> "testing".

> **"Automated ALLOW/BLOCK merge decisions"** — True. Deterministic score →
> threshold → commit status. Verified. (Actually *blocking* the merge button also
> needs branch protection configured on the repo — the guardrail supplies the
> failing status; the repo owner must require it.)

> **"reducing manual review effort"** — Plausible as triage; unquantified. No
> before/after study. Say "intended to reduce" or drop the causal claim.

> **"Cut CI/CD feedback time from a 30–40-minute manual review cycle to under 2
> seconds"** — Two problems: (1) the 30–40 min is an unmeasured baseline; (2) "under
> 2 seconds" is true for the **verdict** (p95 ~1.25 s with the real GitHub API) but
> not for the full output including LLM remediation. Also it doesn't *replace* CI —
> it runs before it. Defensible: **"delivers an automated pre-review risk verdict
> in ~1 s (p95, real GitHub API included), ahead of the CI cycle."**

---

## RESUME CLAIM AUDIT

| Claim | Verdict | Why |
|---|---|---|
| **4-agent architecture** | **PARTIAL** | 4 real classes, fixed sequence, one orchestrator, real distinct jobs — but not "agents" in the autonomous/LLM sense. Fine to say "4-stage" or "4-analyzer". |
| **AI guardrail** | **PARTIAL / misleading** | No AI in the gate. LLM only writes post-verdict remediation text, and only if configured. The guardrail is fully functional with zero AI. |
| **Semantic analysis** | **PARTIAL** | Real `ast.parse` extracting defined function/class names + keyword matching. Not data-flow/taint/type analysis. "AST-based" is the honest term. |
| **Regression detection** | **PARTIAL** | Real, persisted, deterministic — but it's an occurrence counter keyed by `(category, rule_id)`, not behavioural/test regression analysis. |
| **Adversarial testing** | **PARTIAL / misleading** | 10 real deterministic detection rules for injection/unsafe-API shapes. No code executed, no fuzzing, no sandbox. "Static security pattern detection" is accurate. |
| **ALLOW/BLOCK decisions** | **REAL** | `compute_risk_score` → threshold → commit status. Deterministic, tested, demonstrated on real vulnerable + safe inputs. |
| **HMAC authentication** | **REAL** | `hmac.compare_digest` over the raw body; verified live (401/401/200). Startup warning + `/health` flag when disabled. |
| **GitHub integration** | **REAL (read) / UNVERIFIED (write)** | PR-file fetch verified against real public PRs. Commit-status **write** could not be verified — the supplied token lacks "Commit statuses: write" (403). Code path is correct; needs a properly-scoped token. |
| **<2 second verdict** | **REAL, scoped** | Verdict delivery p95 ~1.25 s incl. 3 real GitHub API round trips (p50 ~0.8 s); local compute p95 ~2 ms. **NOT** true if the LLM remediation (~1.5 s) is included — which is why it's off the verdict path. |
| **30–40 minute comparison** | **FALSE (as a measurement)** | Not measured, not in the code. An asserted industry-typical baseline. Also the system runs *before* CI, it doesn't replace a 30–40 min pipeline. |

---

## SAFE RESUME VERSION

> **PipelineAI — pre-merge PR risk guardrail (Python, FastAPI, CLI).**
> Built a terminal-first guardrail that analyzes a GitHub Pull Request diff
> through four sequential analyzers — AST-based change analysis, 10 deterministic
> injection/unsafe-API detection rules, a persisted cross-PR issue-recurrence
> lexicon, and a transparent 0–100 risk score — and publishes a deterministic
> ALLOW/BLOCK commit status.
>
> FastAPI webhook server with HMAC-SHA256 (`X-Hub-Signature-256`) verification and
> `X-GitHub-Delivery` de-duplication; `pipelineai` CLI (`check`, `analyze`,
> `webhook`, `benchmark`, `doctor`) that shares the exact pipeline, exits 0/1/2
> for CI, and renders per-stage timing and findings.
>
> Delivers the risk verdict in **~1 second at p95 including real GitHub API round
> trips** (local analysis p95 ~2 ms), measured with a `perf_counter` harness over
> real public PRs — a fast automated triage pass ahead of the CI cycle. Optional
> LLM (any OpenAI-compatible endpoint) generates issue-grounded remediation text
> *after* the verdict, with a deterministic fallback; it never affects the gate.
> 52 tests; the analyzers are deterministic (same input → identical verdict).

Bullet-sized version:

- Built a **deterministic pre-merge PR guardrail** (FastAPI + `pipelineai` CLI):
  HMAC-verified GitHub webhooks → AST change analysis + 10 static injection/
  unsafe-API rules + a persisted issue-recurrence lexicon → a 0–100 risk score →
  an ALLOW/BLOCK GitHub commit status.
- **~1 s p95 verdict latency including real GitHub API calls** (local analysis
  p95 ~2 ms), `perf_counter`-measured over real public PRs; runs as a fast triage
  pass ahead of CI. 52 tests; verdict is LLM-independent.
- Optional LLM remediation (any OpenAI-compatible endpoint) runs **after** the
  verdict as a detached task, with a deterministic fallback.

### Words to avoid in interviews
"AI guardrail" (the gate has no AI) · "semantic analysis" unqualified · "adversarial
testing" (nothing is executed) · "cut CI from 30–40 min to 2 s" (unmeasured
baseline; it doesn't replace CI) · "the agents" (they're analyzer classes).
