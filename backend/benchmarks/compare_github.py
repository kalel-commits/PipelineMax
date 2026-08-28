"""A/B comparison of the GitHub-path optimizations against the previous implementation.

Same real production path, same real api.github.com calls, same corpus of real
public PRs -- the only thing that changes is whether the two optimizations are in
effect:

  BEFORE:
    * a fresh httpx.AsyncClient per GitHub call (new TCP + TLS handshake every time),
      exactly like the old `async with httpx.AsyncClient(...)` blocks
    * set_pending awaited to completion BEFORE get_pull_request_files starts (serial)

  AFTER (current code):
    * one pooled httpx.AsyncClient reused for every call (keep-alive)
    * set_pending overlapped with get_pull_request_files via asyncio.ensure_future

Run from backend/:  python -m benchmarks.compare_github --rounds 8
"""

import argparse
import asyncio
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with contextlib.suppress(Exception):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import httpx  # noqa: E402
from benchmarks.bench_guardrail import load_guardrail, run_once, summarize, print_summary  # noqa: E402
from benchmarks.fixtures import REAL_PRS  # noqa: E402
from integrations.github_client import (  # noqa: E402
    GitHubAPIError, GitHubAuthError, GitHubNotFoundError, GitHubRateLimitError,
)


def _payloads():
    out = []
    for i, (owner, repo, num) in enumerate(REAL_PRS):
        out.append({
            "action": "opened",
            "number": num,
            "pull_request": {"number": num, "head": {"sha": f"{'0' * 39}{i}"}, "base": {"ref": "main"}},
            "repository": {"name": repo, "owner": {"login": owner}, "full_name": f"{owner}/{repo}"},
        })
    return out


# ---- BEFORE: fresh client per call ------------------------------------------- #
@contextlib.contextmanager
def no_connection_reuse(gm):
    gc = gm.github_client
    orig = gc._get_client

    class _PerCall:
        """Every .get/.post opens and closes its own AsyncClient, like the old code."""
        async def get(self, url, headers=None, params=None):
            async with httpx.AsyncClient(timeout=gc.timeout) as c:
                return await c.get(url, headers=headers, params=params)

        async def post(self, url, headers=None, json=None):
            async with httpx.AsyncClient(timeout=gc.timeout) as c:
                return await c.post(url, headers=headers, json=json)

    gc._get_client = lambda: _PerCall()
    try:
        yield
    finally:
        gc._get_client = orig


# ---- BEFORE: serial set_pending then fetch ---------------------------------- #
@contextlib.contextmanager
def serial_pending(gm):
    wh = gm.webhook_handler
    orig = wh.handle_pr_event

    async def old_handle_pr_event(payload):
        pr = payload["pull_request"]
        repo_data = payload["repository"]
        owner = repo_data["owner"]["login"]
        repo = repo_data["name"]
        pull_number = pr["number"]
        sha = pr["head"]["sha"]
        await wh.github_checks.set_pending(owner, repo, sha)          # serial (old behaviour)
        try:
            files = await wh.github_client.get_pull_request_files(owner, repo, pull_number)
        except (GitHubAuthError, GitHubRateLimitError, GitHubNotFoundError, GitHubAPIError) as e:
            await wh.github_checks.publish_error(owner, repo, sha, str(e))
            return {"verdict": "ERROR", "reason": str(e)}
        pr_data = {"id": pull_number, "branch": pr.get("head", {}).get("ref", "main"), "files": files}
        verdict = await wh.risk_agent.evaluate_pr(pr_data)
        await wh.github_checks.publish_verdict(owner, repo, sha, verdict)
        return verdict

    wh.handle_pr_event = old_handle_pr_event
    try:
        yield
    finally:
        wh.handle_pr_event = orig


async def _measure(gm, payloads, rounds, label):
    samples = []
    # warmup
    with contextlib.suppress(Exception):
        await run_once(gm, payloads[0])
    for _ in range(rounds):
        for p in payloads:
            try:
                ms, _ = await run_once(gm, p)
                samples.append(ms)
            except Exception as e:
                print(f"  [{label}] {p['repository']['full_name']}#{p['number']}: {e}")
    return summarize(samples) if samples else None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=8)
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("compare_github needs GITHUB_TOKEN set (in backend/.env). Aborting.")
        return

    payloads = _payloads()
    print("GitHub-path A/B  --  real api.github.com, real production path, "
          f"{len(payloads)} real public PRs x {args.rounds} rounds\n")

    store = os.path.join(os.path.dirname(__file__), "_cmp_lexicon.json")

    # AFTER first (warm network), then BEFORE, then AFTER again to bracket drift.
    gm = load_guardrail(store, keep_github_token=True)
    gm.github_client.token = token
    after1 = await _measure(gm, payloads, args.rounds, "after")

    gm = load_guardrail(store, keep_github_token=True)
    gm.github_client.token = token
    with no_connection_reuse(gm), serial_pending(gm):
        before = await _measure(gm, payloads, args.rounds, "before")

    gm = load_guardrail(store, keep_github_token=True)
    gm.github_client.token = token
    after2 = await _measure(gm, payloads, args.rounds, "after")

    for name, st in (("BEFORE (fresh client/call + serial set_pending)", before),
                     ("AFTER run 1 (pooled client + overlapped set_pending)", after1),
                     ("AFTER run 2 (repeat, to show run-to-run drift)", after2)):
        if st:
            print_summary(name, st)
        else:
            print(f"\n=== {name} ===\n  no samples")

    if before and after1:
        for k in ("p50", "p95", "p99", "avg"):
            b, a = before[k], after1[k]
            print(f"  {k:4s}: {b:8.1f} ms -> {a:8.1f} ms   ({(b - a) / b * 100:+.0f}%)")


if __name__ == "__main__":
    asyncio.run(main())
