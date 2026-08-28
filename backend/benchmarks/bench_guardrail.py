"""End-to-end performance benchmark for the PipelineAI guardrail pipeline.

Measures the full production path for a GitHub ``pull_request`` webhook:

    webhook received
      -> HMAC SHA-256 signature verification      (guardrail_main.verify_signature)
      -> JSON payload parse                        (json.loads, == await request.json())
      -> process_guardrail() timeout wrapper       (guardrail_main.process_guardrail)
         -> WebhookHandler.handle_pr_event
            -> GitHubChecksAPI.set_pending  ] overlapped  (GitHub commit-status POST)
            -> GitHubClient.get_pull_request_files ]       (GitHub PR files GET)
            -> RiskAgent.decide                    (Impact + Simulation + Memory -> ALLOW/BLOCK)
            -> GitHubChecksAPI.publish_verdict     (GitHub commit-status POST -- the merge gate)
            -> [BLOCK] detached RiskAgent.enrich   (MemoryAgent.store_failure + AIRemediation LLM)

Every function called here is the real production function imported from
``guardrail_main`` -- nothing is reimplemented. What varies between modes is only
whether the *GitHub* and *LLM* network calls are real or replaced by a
zero-latency in-process stub:

  MODE "local"   GitHub + LLM stubbed in-process. No network. Isolates the cost of
                 the guardrail's own compute (HMAC, parse, 3 analyzers, lexicon
                 lookup, verdict synthesis). Also runs a 1b pass through the real
                 ASGI app (real /webhook route + BackgroundTask).

  MODE "github"  Real api.github.com: 3 real round trips/analysis over the real
                 public merged PRs in fixtures.REAL_PRS (ALLOW verdicts, no LLM).

  MODE "openai"  Real LLM for the remediation call on BLOCK verdicts, GitHub
                 stubbed. Isolates the LLM cost (now OFF the verdict path).
                 Requires OPENAI_API_KEY[/OPENAI_BASE_URL/OPENAI_MODEL].

  MODE "full"    Complete real path for a BLOCK verdict: real api.github.com x3,
                 BLOCK forced by appending one synthetic `except:` file to the
                 real PR diff. Timed value = webhook -> verdict on the PR.

No mode inserts artificial sleeps or fabricated timings. A stub returns
immediately with a canned value and that fact is stated in the output.

Usage:
    python -m benchmarks.bench_guardrail --mode local  --rounds 15
    python -m benchmarks.bench_guardrail --mode github --rounds 12
    python -m benchmarks.bench_guardrail --mode openai --llm-rounds 2 --llm-pace 14
    python -m benchmarks.bench_guardrail --mode full   --llm-rounds 6
    python -m benchmarks.bench_guardrail --mode all
"""

import argparse
import asyncio
import hashlib
import hmac
import importlib
import io
import json
import os
import statistics
import sys
import time
from contextlib import contextmanager, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

from benchmarks.fixtures import get_fixtures, REAL_PRS  # noqa: E402

_SECRET = "benchmark-webhook-secret"

# Captured ONCE, before load_guardrail() starts overwriting os.environ per-mode.
_REAL_ENV = {
    "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
    "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
}


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #
def _percentile(sorted_vals, pct):
    """Nearest-rank percentile (no interpolation) -- conservative for latency."""
    if not sorted_vals:
        return float("nan")
    k = max(1, int(round(pct / 100.0 * len(sorted_vals))))
    return sorted_vals[min(k, len(sorted_vals)) - 1]


def summarize(samples_ms):
    s = sorted(samples_ms)
    return {
        "n": len(s),
        "min": s[0],
        "max": s[-1],
        "avg": statistics.fmean(s),
        "stdev": statistics.pstdev(s) if len(s) > 1 else 0.0,
        "p50": _percentile(s, 50),
        "p95": _percentile(s, 95),
        "p99": _percentile(s, 99),
    }


def print_summary(title, stats, note=""):
    print(f"\n=== {title} ===")
    if note:
        print(note)
    print(f"  samples : {stats['n']}")
    print(f"  min     : {stats['min']:8.1f} ms")
    print(f"  avg     : {stats['avg']:8.1f} ms   (stdev {stats['stdev']:.1f})")
    print(f"  p50     : {stats['p50']:8.1f} ms")
    print(f"  p95     : {stats['p95']:8.1f} ms")
    print(f"  p99     : {stats['p99']:8.1f} ms")
    print(f"  max     : {stats['max']:8.1f} ms")
    ok = stats["p95"] < 2000
    print(f"  --> p95 {'<' if ok else '>='} 2000 ms : {'<2s CLAIM SUPPORTED' if ok else '<2s CLAIM NOT SUPPORTED'}")


# --------------------------------------------------------------------------- #
# module wiring
# --------------------------------------------------------------------------- #
def load_guardrail(store_path, keep_github_token=False, keep_openai_key=False):
    """Import guardrail_main fresh with a benchmark environment."""
    os.environ["WEBHOOK_SECRET"] = _SECRET
    os.environ["AGENT_TIMEOUT_SECONDS"] = "60"  # real timeout wrapper, set high so it never trips
    os.environ["GITHUB_TOKEN"] = _REAL_ENV["GITHUB_TOKEN"] if keep_github_token else ""
    if keep_openai_key:
        os.environ["OPENAI_API_KEY"] = _REAL_ENV["OPENAI_API_KEY"]
        os.environ["OPENAI_BASE_URL"] = _REAL_ENV["OPENAI_BASE_URL"]
        os.environ["OPENAI_MODEL"] = _REAL_ENV["OPENAI_MODEL"]
    else:
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["OPENAI_BASE_URL"] = ""

    sys.modules.pop("guardrail_main", None)
    gm = importlib.import_module("guardrail_main")
    gm.memory_agent.store_path = os.path.abspath(store_path)
    gm.memory_agent._write({})
    return gm


class _StubGitHub:
    """Zero-latency in-process stand-in for GitHubClient. Returns the fixture's
    changed files directly and acks every commit-status write. No sleeps."""

    def __init__(self):
        self.calls = {"files": 0, "status": 0}
        self.token = ""
        self._files_by_pr = {}

    def load(self, fixtures):
        for fx in fixtures:
            self._files_by_pr[fx["payload"]["pull_request"]["number"]] = fx["payload"]["_files"]

    async def get_pull_request_files(self, owner, repo, pull_number):
        self.calls["files"] += 1
        return self._files_by_pr.get(pull_number, [])

    async def set_commit_status(self, owner, repo, sha, state, description,
                                context="PipelineAI / Guardrail", target_url=None):
        self.calls["status"] += 1
        return True


@contextmanager
def quiet():
    """Swallow the guardrail's per-verdict stdout prints during measurement loops."""
    sink = io.StringIO()
    with redirect_stdout(sink):
        yield


@contextmanager
def stub_github(gm):
    real = gm.github_client
    stub = _StubGitHub()
    gm.github_client = stub
    gm.github_checks.github_client = stub
    gm.webhook_handler.github_client = stub
    try:
        yield stub
    finally:
        gm.github_client = real
        gm.github_checks.github_client = real
        gm.webhook_handler.github_client = real


@contextmanager
def capture_verdict(gm):
    """Stash the verdict the real handler returns, without a second invocation."""
    box = {"last": None}
    orig = gm.webhook_handler.handle_pr_event

    async def wrapper(payload):
        v = await orig(payload)
        box["last"] = v
        return v

    gm.webhook_handler.handle_pr_event = wrapper
    try:
        yield box
    finally:
        gm.webhook_handler.handle_pr_event = orig


# --------------------------------------------------------------------------- #
# one production-path invocation
# --------------------------------------------------------------------------- #
def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def run_once(gm, payload_obj):
    """Execute the exact production sequence for a single webhook.
    Returns (total_ms, hmac_ms)."""
    body = json.dumps(payload_obj).encode()
    sig = _sign(body)

    t0 = time.perf_counter()
    if not gm.verify_signature(body, sig):          # 1. HMAC verification (real)
        raise RuntimeError("signature verification failed in benchmark")
    t_hmac = time.perf_counter()
    payload = json.loads(body)                       # 2. JSON parse (== await request.json())
    await gm.process_guardrail(payload)              # 3. full orchestration + real timeout wrapper
    t1 = time.perf_counter()
    return (t1 - t0) * 1000.0, (t_hmac - t0) * 1000.0


async def run_asgi_once(client, payload_obj) -> float:
    """End-to-end through the real ASGI app. Starlette awaits BackgroundTasks
    before the request coroutine returns, so this time covers webhook receipt +
    response + the full background guardrail run."""
    body = json.dumps(payload_obj).encode()
    headers = {"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}
    t0 = time.perf_counter()
    resp = await client.post("/webhook", content=body, headers=headers)
    dt = (time.perf_counter() - t0) * 1000.0
    if resp.status_code != 200:
        raise RuntimeError(f"ASGI webhook returned {resp.status_code}: {resp.text}")
    return dt


# --------------------------------------------------------------------------- #
# per-stage instrumentation (local mode only)
# --------------------------------------------------------------------------- #
@contextmanager
def instrument_stages(gm):
    keys = ("fetch", "impact", "sim", "memory", "risk_synth", "publish")
    timings = {k: [] for k in keys}

    ra = gm.risk_agent
    wh = gm.webhook_handler
    originals = {
        "impact": ra.impact.analyze,
        "sim": ra.sim.analyze,
        "memory": ra.memory.check_lexicon,
        "risk_synth": ra.evaluate_pr,
        "fetch": wh.github_client.get_pull_request_files,
        "publish": gm.github_checks.publish_verdict,
    }

    def timed_sync(fn, key):
        def w(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                timings[key].append((time.perf_counter() - t) * 1000.0)
        return w

    def timed_async(fn, key):
        async def w(*a, **k):
            t = time.perf_counter()
            try:
                return await fn(*a, **k)
            finally:
                timings[key].append((time.perf_counter() - t) * 1000.0)
        return w

    ra.impact.analyze = timed_sync(originals["impact"], "impact")
    ra.sim.analyze = timed_sync(originals["sim"], "sim")
    ra.memory.check_lexicon = timed_sync(originals["memory"], "memory")
    ra.evaluate_pr = timed_async(originals["risk_synth"], "risk_synth")
    wh.github_client.get_pull_request_files = timed_async(originals["fetch"], "fetch")
    gm.github_checks.publish_verdict = timed_async(originals["publish"], "publish")
    try:
        yield timings
    finally:
        ra.impact.analyze = originals["impact"]
        ra.sim.analyze = originals["sim"]
        ra.memory.check_lexicon = originals["memory"]
        ra.evaluate_pr = originals["risk_synth"]
        wh.github_client.get_pull_request_files = originals["fetch"]
        gm.github_checks.publish_verdict = originals["publish"]


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
async def bench_local(rounds, warmup=1):
    store = os.path.join(os.path.dirname(__file__), "_bench_lexicon_local.json")
    gm = load_guardrail(store)
    fixtures = get_fixtures()

    samples, hmac_samples = [], []
    verdicts = {"ALLOW": 0, "BLOCK": 0, "ERROR": 0, "mismatch": 0}

    with quiet(), stub_github(gm) as stub, capture_verdict(gm) as box, instrument_stages(gm) as stages:
        stub.load(fixtures)
        for _ in range(warmup):
            for fx in fixtures:
                await run_once(gm, fx["payload"])
        gm.memory_agent._write({})
        for v in stages.values():
            v.clear()

        for _ in range(rounds):
            for fx in fixtures:
                total_ms, hmac_ms = await run_once(gm, fx["payload"])
                samples.append(total_ms)
                hmac_samples.append(hmac_ms)
                v = box["last"]["verdict"]
                verdicts[v] = verdicts.get(v, 0) + 1
                if v != fx["expected"]:
                    verdicts["mismatch"] += 1

    stats = summarize(samples)
    print_summary(
        "MODE 1: LOCAL / IN-PROCESS  (GitHub + OpenAI stubbed, zero-latency, no network)",
        stats,
        note=(f"  {rounds} rounds x {len(fixtures)} fixtures = {stats['n']} analyses. "
              f"verdicts: {verdicts['ALLOW']} ALLOW / {verdicts['BLOCK']} BLOCK / {verdicts['ERROR']} ERROR, "
              f"expectation mismatches: {verdicts['mismatch']}"),
    )
    print("\n  per-stage mean (ms), local mode:")
    print(f"    {'hmac_verify':12s}: {statistics.fmean(hmac_samples):8.4f}  (max {max(hmac_samples):.4f})")
    for key in ("fetch", "impact", "sim", "memory", "risk_synth", "publish"):
        vals = stages[key]
        if vals:
            print(f"    {key:12s}: {statistics.fmean(vals):8.4f}  (max {max(vals):.4f}, n={len(vals)})")
    print("    NOTE: 'fetch'/'publish' above are the in-process stub, NOT GitHub network time.")
    print("          'risk_synth' includes impact+sim+memory (+ remediation fallback on BLOCK).")
    return stats


async def bench_local_asgi(rounds):
    import httpx

    store = os.path.join(os.path.dirname(__file__), "_bench_lexicon_asgi.json")
    gm = load_guardrail(store)
    fixtures = get_fixtures()
    samples = []
    with quiet(), stub_github(gm) as stub:
        stub.load(fixtures)
        transport = httpx.ASGITransport(app=gm.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            for fx in fixtures:
                await run_asgi_once(client, fx["payload"])  # warmup
            for _ in range(rounds):
                for fx in fixtures:
                    samples.append(await run_asgi_once(client, fx["payload"]))
    stats = summarize(samples)
    print_summary(
        "MODE 1b: LOCAL END-TO-END THROUGH ASGI  (real /webhook route + BackgroundTask, GitHub stubbed)",
        stats,
        note=f"  {rounds} rounds x {len(fixtures)} fixtures. Includes Starlette request/response + BackgroundTasks.",
    )
    return stats


async def bench_github(rounds):
    token = _REAL_ENV["GITHUB_TOKEN"].strip()
    store = os.path.join(os.path.dirname(__file__), "_bench_lexicon_github.json")
    gm = load_guardrail(store, keep_github_token=bool(token))
    if token:
        gm.github_client.token = token

    payloads = []
    for i, (owner, repo, num) in enumerate(REAL_PRS):
        payloads.append({
            "action": "opened",
            "number": num,
            "pull_request": {"number": num, "head": {"sha": f"{'0' * 39}{i}"}, "base": {"ref": "main"}},
            "repository": {"name": repo, "owner": {"login": owner}, "full_name": f"{owner}/{repo}"},
        })

    writes_real = bool(token)
    if not writes_real:
        async def _noop_status(*a, **k):
            return True
        gm.github_client.set_commit_status = _noop_status

    # Guard against silently benchmarking 403 rate-limit responses: verify a real
    # PR fetch returns real files before trusting any timing.
    try:
        probe = await gm.github_client.get_pull_request_files(*REAL_PRS[0])
    except Exception as e:
        print(f"\n[github mode] cannot fetch a real PR ({type(e).__name__}: {e}).")
        if not token:
            print("  Unauthenticated GitHub is capped at 60 req/hr. Set GITHUB_TOKEN to run this mode.")
        return None
    if not probe:
        print("\n[github mode] real PR fetch returned no analyzable files; aborting to avoid bad data.")
        return None

    # Track whether the 2 commit-status POSTs/analysis actually write (201) or
    # fast-fail on a missing scope (403). Either way they are real round trips,
    # but a 403 may be marginally cheaper server-side than a 201 -- report which.
    status_results = {"ok": 0, "fail": 0}
    _real_status = gm.github_client.set_commit_status

    async def _counting_status(*a, **k):
        r = await _real_status(*a, **k)
        status_results["ok" if r else "fail"] += 1
        return r
    gm.github_client.set_commit_status = _counting_status

    samples, errors, err_notes = [], 0, []
    with capture_verdict(gm) as box:
        for _ in range(rounds):
            for p in payloads:
                try:
                    with quiet():
                        total_ms, _ = await run_once(gm, p)
                except Exception as e:
                    errors += 1
                    err_notes.append(f"  [github mode] error on {p['repository']['full_name']}#{p['number']}: {e}")
                    continue
                if box["last"] and box["last"]["verdict"] == "ERROR":
                    errors += 1
                    err_notes.append(f"  [github mode] guardrail ERROR for #{p['number']}: {box['last']['reason']}")
                    continue
                samples.append(total_ms)
    for n in err_notes[:8]:
        print(n)

    if not samples:
        print("\n[github mode] no successful samples (all iterations errored/rate-limited)")
        return None
    if errors:
        print(f"  [github mode] {errors} iteration(s) excluded (error/rate-limit); {len(samples)} clean samples kept")

    stats = summarize(samples)
    if writes_real:
        w = status_results
        if w["ok"] and not w["fail"]:
            write_note = f"REAL, all {w['ok']} writes returned 201 Created"
        elif w["ok"]:
            write_note = f"REAL round trips ({w['ok']} x 201, {w['fail']} x non-2xx)"
        else:
            write_note = (f"REAL round trips but all {w['fail']} returned non-2xx "
                          f"(token lacks 'repo:status' scope) -- 403 may be marginally cheaper than 201")
    else:
        write_note = "STUBBED (GitHub requires a token to write statuses)"
    note = (f"  {rounds} rounds x {len(payloads)} real public PRs = {stats['n']} analyses.\n"
            f"  Retrieval (get_pull_request_files): REAL api.github.com "
            f"{'(authenticated)' if token else '(UNauthenticated: ~60 req/hr cap)'}.\n"
            f"  Commit-status writes (set_pending + publish_verdict, 2/analysis): {write_note}.\n"
            f"  Connection: single pooled httpx.AsyncClient reused across all 3 calls/analysis.")
    print_summary("MODE 2: GITHUB API INCLUDED  (real api.github.com round trips)", stats, note=note)
    return stats


_INJECT_BAD_FILE = {
    "filename": "src/parser/lexer.py",
    "patch": "@@ -10,4 +10,8 @@\n+def parse_token(raw):\n+    try:\n+        return decode(raw)\n+    except:\n+        return None\n",
    "status": "modified", "additions": 5, "deletions": 0,
}


async def bench_full_real(rounds, pace_seconds=0.0, real_llm=False):
    """MODE 4 -- complete real-world path for a BLOCK verdict: real api.github.com
    for all 3 calls, driving the real handler. A BLOCK is forced deterministically
    by appending one synthetic `except:` file to the *real* PR-files response.

    Since the LLM remediation is now a detached task (published verdict does not
    wait for it), the timed value is 'webhook received -> BLOCK verdict on the PR'.
    real_llm=True still fires the real LLM in the background (does not change the
    measured number); default stubs it to a deterministic fallback."""
    token = _REAL_ENV["GITHUB_TOKEN"].strip()
    if not token:
        print("\n=== MODE 4: COMPLETE REAL PATH ===\n  SKIPPED: needs GITHUB_TOKEN.")
        return None

    store = os.path.join(os.path.dirname(__file__), "_bench_lexicon_full.json")
    gm = load_guardrail(store, keep_github_token=True, keep_openai_key=real_llm)
    gm.github_client.token = token

    real_get = gm.github_client.get_pull_request_files

    async def get_plus_bad(owner, repo, pull_number):
        files = await real_get(owner, repo, pull_number)   # real GitHub round trip
        return list(files) + [dict(_INJECT_BAD_FILE)]      # + 1 synthetic file to force BLOCK

    gm.github_client.get_pull_request_files = get_plus_bad
    gm.webhook_handler.github_client = gm.github_client

    payloads = []
    for i, (owner, repo, num) in enumerate(REAL_PRS):
        payloads.append({
            "action": "opened", "number": num,
            "pull_request": {"number": num, "head": {"sha": f"{'0' * 39}{i}"}, "base": {"ref": "main"}},
            "repository": {"name": repo, "owner": {"login": owner}, "full_name": f"{owner}/{repo}"},
        })

    samples, verdobserved = [], {"BLOCK": 0, "ALLOW": 0, "ERROR": 0}
    with quiet(), capture_verdict(gm) as box:
        try:
            await run_once(gm, payloads[0])  # warmup
        except Exception as e:
            print(f"\n=== MODE 4 ===\n  warmup failed: {e}")
            return None
        first = True
        for _ in range(rounds):
            for p in payloads:
                if pace_seconds and not first:
                    await asyncio.sleep(pace_seconds)
                first = False
                try:
                    total_ms, _ = await run_once(gm, p)
                except Exception as e:
                    print(f"  [mode4] {p['repository']['full_name']}#{p['number']}: {e}")
                    continue
                v = (box["last"] or {}).get("verdict", "ERROR")
                verdobserved[v] = verdobserved.get(v, 0) + 1
                if v == "BLOCK":
                    samples.append(total_ms)
        # let any detached enrichment tasks finish before the loop tears down
        pending = [t for t in getattr(gm.webhook_handler, "_enrich_tasks", set())]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if not samples:
        print("\n=== MODE 4: COMPLETE REAL PATH ===")
        print(f"  NOT MEASURABLE: no BLOCK samples (verdicts observed: {verdobserved}).")
        return None
    stats = summarize(samples)
    print_summary(
        f"MODE 4: COMPLETE REAL PATH  (real GitHub x3, forced BLOCK, LLM {'REAL/detached' if real_llm else 'stubbed/detached'})",
        stats,
        note=(f"  n={stats['n']} BLOCK samples over {rounds} round(s) x {len(payloads)} real PRs.\n"
              f"  Timed: webhook received -> HMAC -> real files GET -> agents -> real publish_verdict POST\n"
              f"  (set_pending POST overlapped). LLM remediation runs detached AFTER this point.\n"
              f"  One synthetic `except:` file appended to each real PR diff to force a BLOCK.\n"
              f"  GitHub status POSTs return 403 (token lacks repo:status) -- real round trips; a 201 may be modestly slower."),
    )
    return stats


async def bench_openai(rounds, pace_seconds=0.0):
    key = _REAL_ENV["OPENAI_API_KEY"].strip()
    base_url = _REAL_ENV["OPENAI_BASE_URL"].strip()
    provider = "Gemini (OpenAI-compat)" if "generativelanguage.googleapis" in base_url else (
        "custom OpenAI-compat endpoint" if base_url else "OpenAI")
    if not key:
        print("\n=== MODE 3: REAL LLM API INCLUDED ===")
        print("  SKIPPED: OPENAI_API_KEY is not set in this environment.")
        print("  This mode is never simulated. Set OPENAI_API_KEY (+ OPENAI_BASE_URL / OPENAI_MODEL")
        print("  for a non-OpenAI provider) and re-run to measure it.")
        return None

    store = os.path.join(os.path.dirname(__file__), "_bench_lexicon_openai.json")
    gm = load_guardrail(store, keep_openai_key=True)
    if not gm.remediation_ai.configured:
        print("\n=== MODE 3: REAL LLM API INCLUDED ===")
        print("  SKIPPED: AIRemediation is not configured (openai SDK import failed?).")
        return None
    fixtures = [f for f in get_fixtures() if f["expected"] == "BLOCK"]
    samples = []
    sources = {"openai": 0, "fallback": 0, "other": 0}
    last_fallback_reason = None
    if pace_seconds:
        print(f"  [llm mode] pacing {pace_seconds:.0f}s between calls to stay under the provider's rate limit "
              f"(~{rounds * len(fixtures)} calls -> ~{pace_seconds * rounds * len(fixtures) / 60:.0f} min)")
    with quiet(), stub_github(gm) as stub, capture_verdict(gm) as box:
        stub.load(fixtures)
        try:
            await run_once(gm, fixtures[0]["payload"])  # warmup (also warms the TLS connection)
        except Exception as e:
            print(f"  LLM warmup failed: {e}")
            return None
        first = True
        for _ in range(rounds):
            for fx in fixtures:
                if pace_seconds and not first:
                    await asyncio.sleep(pace_seconds)
                first = False
                total_ms, _ = await run_once(gm, fx["payload"])
                sug = (box["last"] or {}).get("suggestion", {})
                src = sug.get("source", "other")
                sources[src] = sources.get(src, 0) + 1
                if src == "fallback":
                    last_fallback_reason = sug.get("reason")
                if src == "openai":                # keep ONLY analyses that made a real LLM round trip
                    samples.append(total_ms)

    if sources["openai"] == 0:
        print("\n=== MODE 3: REAL LLM API INCLUDED ===")
        print("  NOT MEASURABLE: every remediation call fell back (0 real LLM responses).")
        print(f"  last fallback reason: {last_fallback_reason}")
        return None
    if sources["fallback"]:
        print(f"\n  [llm mode] {sources['fallback']}/{sources['openai'] + sources['fallback']} calls "
              f"fell back and are EXCLUDED from the stats below (last reason: "
              f"{str(last_fallback_reason)[:90]}...).")

    stats = summarize(samples)
    reliability = ("all real LLM responses" if not sources["fallback"]
                   else f"ONLY {sources['openai']} clean samples; provider rate-limited the rest")
    print_summary(
        "MODE 3: REAL LLM API INCLUDED  (BLOCK verdict full analysis incl. real remediation call, GitHub stubbed)",
        stats,
        note=(f"  Provider: {provider}, model: {gm.remediation_ai.model}. {reliability}.\n"
              f"  Each sample = HMAC + parse + 4 agents + store_failure + ONE real LLM call (no GitHub).\n"
              f"  NOTE: this LLM call fires only on BLOCK and never affects the ALLOW/BLOCK gate."),
    )
    return stats


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["local", "github", "openai", "full", "all"], default="all")
    ap.add_argument("--rounds", type=int, default=5,
                    help="passes over the fixture corpus (local corpus = 24 fixtures/round)")
    ap.add_argument("--llm-rounds", type=int, default=None,
                    help="override rounds for mode 3 (default: 2)")
    ap.add_argument("--llm-pace", type=float, default=0.0,
                    help="seconds to wait between mode-3 LLM calls (use ~13 for Gemini free tier: 5 req/min)")
    args = ap.parse_args()

    print("PipelineAI Guardrail -- End-to-End Performance Benchmark")
    print(f"Python {sys.version.split()[0]} | perf_counter resolution "
          f"{time.get_clock_info('perf_counter').resolution * 1e9:.1f} ns | host {sys.platform}")
    print("claim under test: 'risk verdicts in <2 seconds'  (evaluated against p95)")

    if args.mode in ("local", "all"):
        await bench_local(args.rounds)
        await bench_local_asgi(max(2, args.rounds // 2))
    if args.mode in ("github", "all"):
        await bench_github(max(3, args.rounds))
    if args.mode in ("openai", "all"):
        await bench_openai(args.llm_rounds or 2, pace_seconds=args.llm_pace)
    if args.mode in ("full", "all"):
        await bench_full_real(args.llm_rounds or 1, pace_seconds=args.llm_pace)

    # Give any still-open pooled SSL transports a beat to close before the loop
    # tears down (avoids a benign Proactor/SSL segfault on interpreter exit on Windows).
    await asyncio.sleep(0.25)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
