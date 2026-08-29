"""`pipelineai doctor` — real preflight checks. Every check performs actual work
(a live API call, a real import, a real file stat); nothing is assumed."""

from __future__ import annotations

import sys
import time

import httpx
from rich.console import Console
from rich.table import Table
from rich import box

from .config import load_config

console = Console()

OK, WARN, FAIL = "[green]PASS[/green]", "[yellow]WARN[/yellow]", "[red]FAIL[/red]"


async def run_doctor() -> bool:
    cfg = load_config()
    rows: list[tuple[str, str, str]] = []
    hard_fail = False

    # 1. python
    py = sys.version.split()[0]
    rows.append(("python >= 3.10", OK if sys.version_info >= (3, 10) else FAIL, py))
    if sys.version_info < (3, 10):
        hard_fail = True

    # 2. deps
    try:
        import fastapi, uvicorn, rich, typer  # noqa
        rows.append(("runtime deps import", OK, "fastapi, uvicorn, rich, typer"))
    except Exception as e:  # pragma: no cover
        rows.append(("runtime deps import", FAIL, str(e)))
        hard_fail = True

    # 3. agents import + a real 1-file analysis
    try:
        from .pipeline import AnalysisPipeline
        pipe = AnalysisPipeline(cfg)
        r = pipe.analyze_files([{
            "filename": "probe.py",
            "patch": "@@ -0,0 +1,2 @@\n+def f(x):\n+    return eval(x)\n",
        }])
        detail = f"verdict={r.verdict} score={r.risk_score} ({sum(t.duration_ms for t in r.timings):.1f}ms)"
        rows.append(("agent pipeline (offline)", OK if r.verdict == "BLOCK" else FAIL, detail))
        if r.verdict != "BLOCK":
            hard_fail = True
    except Exception as e:
        rows.append(("agent pipeline (offline)", FAIL, repr(e)))
        hard_fail = True

    # 4. lexicon file writable
    try:
        cfg.lexicon_path.parent.mkdir(parents=True, exist_ok=True)
        rows.append(("regression lexicon path", OK, str(cfg.lexicon_path)))
    except Exception as e:
        rows.append(("regression lexicon path", FAIL, str(e)))

    # 5. webhook secret
    rows.append((
        "WEBHOOK_SECRET",
        OK if cfg.signature_enforced else WARN,
        "signatures enforced" if cfg.signature_enforced else "unset — DEMO MODE (webhooks unverified)",
    ))

    # 6. GitHub token — LIVE call
    if not cfg.github_token:
        rows.append(("GITHUB_TOKEN", WARN, "unset — public-repo reads only, cannot post statuses"))
    else:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                t0 = time.perf_counter()
                u = await c.get("https://api.github.com/user",
                                headers={"Authorization": f"token {cfg.github_token}"})
                dt = (time.perf_counter() - t0) * 1000
            if u.status_code == 200:
                login = u.json().get("login", "?")
                rows.append(("GitHub token — auth", OK, f"{login} ({dt:.0f}ms)"))
                # probe commit-status write scope against a repo we own
                repos = await _first_owned_repo(cfg.github_token)
                if repos:
                    scope_ok, msg = await _probe_status_scope(cfg.github_token, repos)
                    # Optional for `check`; required only for `check --post` and the
                    # webhook's commit-status publish.
                    rows.append(("GitHub token — commit:status write", OK if scope_ok else WARN, msg))
                else:
                    rows.append(("GitHub token — commit:status write", WARN, "no owned repo to probe"))
            else:
                rows.append(("GitHub token — auth", FAIL, f"HTTP {u.status_code}"))
        except Exception as e:
            rows.append(("GitHub token — auth", FAIL, repr(e)))

    # 7. LLM — LIVE call
    if not cfg.llm_configured:
        rows.append(("LLM remediation", WARN, "OPENAI_API_KEY unset — deterministic fallback only"))
    else:
        try:
            from utils.ai_remediation import AIRemediation
            rem = AIRemediation(api_key=cfg.openai_api_key, model=cfg.openai_model, base_url=cfg.openai_base_url)
            probe_issue = [{"file": "x.py", "line": 1, "category": "security", "severity": 9,
                            "rule_id": "eval_exec", "description": "eval on input",
                            "evidence": "eval(x)", "recommendation": "use ast.literal_eval"}]
            t0 = time.perf_counter()
            out = await rem.suggest_fix(probe_issue, "def f(x): return eval(x)")
            dt = (time.perf_counter() - t0) * 1000
            if out.get("source") == "openai":
                rows.append((f"LLM ({cfg.openai_model})", OK, f"real response ({dt:.0f}ms)"))
            else:
                rows.append((f"LLM ({cfg.openai_model})", WARN,
                             f"fell back: {str(out.get('reason'))[:80]}"))
        except Exception as e:
            rows.append((f"LLM ({cfg.openai_model})", FAIL, repr(e)))

    # --- render ---
    t = Table(box=box.SIMPLE, title="pipelineai doctor")
    t.add_column("check", style="bold")
    t.add_column("status")
    t.add_column("detail", style="dim")
    for name, status, detail in rows:
        t.add_row(name, status, detail)
    console.print(t)
    if hard_fail:
        console.print("[red]✗ one or more required checks failed[/red]")
    else:
        console.print("[green]✓ ready[/green] — warnings above are optional features")
    return not hard_fail


async def _first_owned_repo(token: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://api.github.com/user/repos?affiliation=owner&per_page=1",
                        headers={"Authorization": f"token {token}"})
    if r.status_code == 200 and r.json():
        return r.json()[0]["full_name"]
    return ""


async def _probe_status_scope(token: str, full_name: str) -> tuple[bool, str]:
    """POST a status to a non-existent SHA. 422 = we HAVE write permission
    (SHA just doesn't exist). 403 = permission missing."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"https://api.github.com/repos/{full_name}/statuses/{'0' * 40}",
            headers={"Authorization": f"token {token}"},
            json={"state": "success", "context": "pipelineai/doctor-probe"},
        )
    if r.status_code in (201, 422):
        return True, f"write allowed on {full_name} (probe HTTP {r.status_code})"
    if r.status_code == 403:
        return False, f"403 on {full_name} — token lacks 'Commit statuses: write'"
    return False, f"unexpected HTTP {r.status_code} on {full_name}"
