"""`pipelineai` — terminal-first interface to the Multi-Agent PR Guardrail.

    pipelineai check    acme/payment-service 142     # fetch a real PR, run the pipeline, show the verdict
    pipelineai analyze  acme/payment-service 142 --json
    pipelineai webhook                                # run the GitHub webhook server
    pipelineai benchmark                              # measured latency of the verdict path
    pipelineai doctor                                 # verify config / connectivity
    pipelineai config                                 # show effective config (secrets redacted)
    pipelineai version
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich import box

from . import __version__
from .config import load_config
from .pipeline import AnalysisPipeline, AnalysisResult

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="PipelineAI — Multi-Agent GitHub Pull Request Guardrail (terminal-first).",
    rich_markup_mode="rich",
)
console = Console()

_STAGES = [
    ("fetch", "Fetching PR from GitHub"),
    ("impact", "Impact Agent — semantic blast radius"),
    ("simulation", "Simulation Agent — adversarial pattern scan"),
    ("memory", "Memory Agent — regression lexicon"),
    ("risk", "Risk Agent — score & verdict"),
    ("github-status", "Publishing GitHub commit status"),
    ("remediation", "AI remediation"),
]


def _split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        console.print(f"[red]error:[/red] repository must be 'owner/name', got '{repo}'")
        raise typer.Exit(2)
    owner, name = repo.split("/", 1)
    return owner, name


class _ProgressTracker:
    """Renders a live checklist as the pipeline calls back on each stage start.
    Every line corresponds to actual work — a stage only appears once the
    pipeline has genuinely begun it."""

    def __init__(self, live: Live, active_stages: list[str]):
        self.live = live
        self.order = [s for s in active_stages]
        self.started: dict[str, float] = {}
        self.done: dict[str, float] = {}
        self._labels = dict(_STAGES)

    def __call__(self, stage: str):
        now = time.perf_counter()
        for s in self.started:
            self.done.setdefault(s, now)
        self.started[stage] = now
        self._render()

    def finish(self):
        now = time.perf_counter()
        for s in self.started:
            self.done.setdefault(s, now)
        self._render()

    def _render(self):
        t = Table.grid(padding=(0, 1))
        t.add_column(width=2)
        t.add_column()
        for s in self.order:
            label = self._labels.get(s, s)
            if s in self.done:
                dt = (self.done[s] - self.started[s]) * 1000
                t.add_row(Text("✓", style="green"), Text(f"{label}  [dim]{dt:.0f}ms[/dim]"))
            elif s in self.started:
                t.add_row(Spinner("dots", style="cyan"), Text(label, style="bold"))
            else:
                t.add_row(Text("·", style="dim"), Text(label, style="dim"))
        self.live.update(t)


async def _run_check(repo: str, pr: int, *, post: bool, no_remediate: bool, quiet: bool) -> AnalysisResult:
    owner, name = _split_repo(repo)
    cfg = load_config()
    pipe = AnalysisPipeline(cfg)

    active = ["fetch", "impact", "simulation", "memory", "risk"]
    if post:
        active.append("github-status")
    if not no_remediate:
        active.append("remediation")

    try:
        if quiet:
            result = await pipe.analyze_pr(owner, name, pr, remediate=not no_remediate, post_status=post)
        else:
            with Live(console=console, refresh_per_second=12, transient=True) as live:
                tracker = _ProgressTracker(live, active)
                result = await pipe.analyze_pr(
                    owner, name, pr, remediate=not no_remediate, post_status=post, progress=tracker,
                )
                tracker.finish()
        return result
    finally:
        await pipe.aclose()


def _run_local(path: str, *, remediate: bool, quiet: bool) -> AnalysisResult:
    """Analyze local source as if every line were added in a PR. Fully offline —
    used for demos and CI where there is no GitHub PR to point at."""
    import pathlib

    p = pathlib.Path(path)
    if not p.exists():
        console.print(f"[red]error:[/red] path not found: {path}")
        raise typer.Exit(2)
    paths = sorted(p.rglob("*.py")) if p.is_dir() else [p]
    files = []
    for fp in paths:
        src = fp.read_text(encoding="utf-8", errors="replace")
        patch = f"@@ -0,0 +1,{len(src.splitlines())} @@\n" + "\n".join("+" + ln for ln in src.splitlines())
        files.append({"filename": str(fp), "patch": patch,
                      "additions": len(src.splitlines()), "deletions": 0, "status": "added"})

    cfg = load_config()
    pipe = AnalysisPipeline(cfg)
    result = pipe.analyze_files(files, "", "", 0, f"local:{path}")
    if remediate and result.verdict == "BLOCK":
        t0 = time.perf_counter()
        vd = {"verdict": "BLOCK", "issues": result.issues,
              "suggestion": {"source": "pending", "summary": "", "fixes": []}}
        asyncio.run(pipe.risk.enrich(vd, {"id": 0, "files": files}))
        result.suggestion = vd["suggestion"]
        result.remediation_ms = (time.perf_counter() - t0) * 1000
    return result


# --------------------------------------------------------------------------- #
@app.command()
def check(
    repo: str = typer.Argument(None, help="owner/name, e.g. pallets/flask  (omit with --local)"),
    pr: int = typer.Argument(None, help="pull request number"),
    local: Optional[str] = typer.Option(None, "--local", help="analyze a local file/dir as an all-added diff (no GitHub)"),
    post: bool = typer.Option(False, "--post", help="publish the ALLOW/BLOCK commit status to GitHub"),
    no_remediate: bool = typer.Option(False, "--no-remediate", help="skip the LLM remediation step on BLOCK"),
    detail: bool = typer.Option(False, "--detail", help="also print the per-agent detail blocks"),
    output_json: bool = typer.Option(False, "--json", help="emit the raw result as JSON instead of the panel"),
):
    """Fetch a real PR, run the full 4-agent pipeline, and show the verdict."""
    from .render import render_result, render_agent_detail

    if local:
        result = _run_local(local, remediate=not no_remediate, quiet=output_json)
    else:
        if not repo or pr is None:
            console.print("[red]error:[/red] give 'owner/name PR' or use --local <path>")
            raise typer.Exit(2)
        result = asyncio.run(_run_check(repo, pr, post=post, no_remediate=no_remediate, quiet=output_json))

    if output_json:
        console.print_json(json.dumps(_result_to_dict(result)))
        raise typer.Exit(0 if result.verdict == "ALLOW" else 1 if result.verdict == "BLOCK" else 2)

    render_result(result)
    if detail:
        render_agent_detail(result)
    raise typer.Exit(0 if result.verdict == "ALLOW" else 1 if result.verdict == "BLOCK" else 2)


@app.command()
def analyze(
    repo: str = typer.Argument(...),
    pr: int = typer.Argument(...),
    output_json: bool = typer.Option(True, "--json/--no-json"),
):
    """Machine-readable analysis (JSON by default). No commit-status write."""
    result = asyncio.run(_run_check(repo, pr, post=False, no_remediate=False, quiet=True))
    if output_json:
        console.print_json(json.dumps(_result_to_dict(result)))
    else:
        from .render import render_result
        render_result(result)
    raise typer.Exit(0 if result.verdict == "ALLOW" else 1 if result.verdict == "BLOCK" else 2)


@app.command()
def webhook(
    host: str = typer.Option("0.0.0.0", help="bind address"),
    port: Optional[int] = typer.Option(None, help="port (default: $PORT or 8000)"),
    reload: bool = typer.Option(False, "--reload", help="auto-reload on code changes (dev)"),
):
    """Run the FastAPI GitHub webhook server (the production entrypoint)."""
    import uvicorn

    cfg = load_config()
    bind_port = port or cfg.port

    panel = Table.grid(padding=(0, 1))
    panel.add_column()
    panel.add_row(Text("PipelineAI webhook server", style="bold"))
    panel.add_row(Text(f"  listening       http://{host}:{bind_port}", style="cyan"))
    panel.add_row(Text("  webhook path    POST /webhook", style="dim"))
    panel.add_row(Text("  health          GET  /health", style="dim"))
    sig = "[green]enforced[/green]" if cfg.signature_enforced else "[yellow]DEMO MODE — not enforced[/yellow]"
    panel.add_row(Text.from_markup(f"  HMAC signature  {sig}"))
    gh = "[green]authenticated[/green]" if cfg.github_authenticated else "[yellow]unauthenticated[/yellow]"
    panel.add_row(Text.from_markup(f"  GitHub token    {gh}"))
    llm = f"[green]{cfg.openai_model}[/green]" if cfg.llm_configured else "[dim]fallback only[/dim]"
    panel.add_row(Text.from_markup(f"  LLM remediation {llm}"))
    console.print(panel)

    uvicorn.run("guardrail_main:app", host=host, port=bind_port, reload=reload)


@app.command()
def benchmark(
    mode: str = typer.Option("local", help="local | github | all"),
    rounds: int = typer.Option(8, help="passes over the fixture corpus"),
    raw: bool = typer.Option(False, "--raw", help="show the full benchmark harness output"),
):
    """Measured latency of the verdict path. Every number below is a real
    ``perf_counter`` measurement of the real pipeline — nothing is fabricated,
    and local / GitHub-inclusive / LLM timings are never mixed."""
    from benchmarks import bench_guardrail as bg
    from rich.rule import Rule

    console.print(Rule("[bold]PipelineAI Benchmark[/bold]", style="cyan"))

    async def _go():
        results: dict[str, object] = {}
        buf = _Suppress() if not raw else None
        with (buf or _nullctx()):
            if mode in ("local", "all"):
                results["Local analysis (in-process, no network)"] = await bg.bench_local(rounds)
                results["Local end-to-end via ASGI"] = await bg.bench_local_asgi(max(2, rounds // 2))
            if mode in ("github", "all"):
                results["GitHub-inclusive verdict (real api.github.com x3)"] = await bg.bench_github(max(3, rounds))
        return results

    results = asyncio.run(_go())

    t = Table(box=box.SIMPLE_HEAD)
    t.add_column("path", style="bold")
    t.add_column("p50", justify="right", style="cyan")
    t.add_column("p95", justify="right", style="cyan")
    t.add_column("p99", justify="right", style="cyan")
    t.add_column("min", justify="right", style="dim")
    t.add_column("max", justify="right", style="dim")
    t.add_column("n", justify="right", style="dim")

    def ms(v):
        return f"{v:.1f} ms" if v < 1000 else f"{v / 1000:.2f} s"

    for label, stats in results.items():
        if not stats:
            t.add_row(label, "—", "—", "—", "—", "—", "0")
            continue
        s = stats
        t.add_row(label, ms(s["p50"]), ms(s["p95"]), ms(s["p99"]),
                  ms(s["min"]), ms(s["max"]), str(s["n"]))
    console.print(t)
    console.print(Text.from_markup(
        "\n  [dim]Local numbers exclude all network by design. GitHub-inclusive numbers are "
        "real round trips.\n  The LLM remediation call (~1.5 s) runs [bold]after[/bold] the verdict is "
        "published and is never in these figures.[/dim]"
    ))
    if not raw:
        console.print("[dim]  (run with --raw for the full per-stage harness output)[/dim]")


class _Suppress:
    def __enter__(self):
        import io
        self._r = _redirect(io.StringIO())
        self._r.__enter__()
        return self

    def __exit__(self, *a):
        return self._r.__exit__(*a)


def _redirect(target):
    from contextlib import redirect_stdout
    return redirect_stdout(target)


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@app.command()
def doctor():
    """Verify configuration and live connectivity to GitHub and the LLM."""
    from .doctor import run_doctor

    ok = asyncio.run(run_doctor())
    raise typer.Exit(0 if ok else 1)


@app.command("config")
def config_cmd():
    """Show the effective configuration (secret values redacted)."""
    cfg = load_config()
    t = Table(box=box.SIMPLE, title="effective config")
    t.add_column("key", style="bold")
    t.add_column("value")
    for k, v in cfg.redacted().items():
        t.add_row(k, str(v))
    console.print(t)
    console.print(Text.from_markup(
        f"\n  GitHub: [{'green' if cfg.github_authenticated else 'yellow'}]"
        f"{'authenticated' if cfg.github_authenticated else 'unauthenticated (public repos only, rate-limited)'}[/]"
        f"\n  Webhook signatures: [{'green' if cfg.signature_enforced else 'yellow'}]"
        f"{'enforced' if cfg.signature_enforced else 'DEMO MODE'}[/]"
        f"\n  LLM: [{'green' if cfg.llm_configured else 'dim'}]"
        f"{cfg.llm_provider + ' / ' + cfg.openai_model if cfg.llm_configured else 'deterministic fallback only'}[/]"
    ))


@app.command()
def version():
    """Print version and component status."""
    console.print(f"[bold]pipelineai[/bold] {__version__}")
    console.print(f"  python           {sys.version.split()[0]}")
    try:
        import fastapi, httpx, openai
        console.print(f"  fastapi          {fastapi.__version__}")
        console.print(f"  httpx            {httpx.__version__}")
        console.print(f"  openai-sdk       {openai.__version__ if hasattr(openai, '__version__') else 'installed'}")
    except Exception:
        pass
    from agents.simulation_agent import RULE_COUNT
    from agents.risk_agent import BLOCK_THRESHOLD
    console.print(f"  attack patterns  {RULE_COUNT}")
    console.print(f"  block threshold  {BLOCK_THRESHOLD}/100")


def _result_to_dict(r: AnalysisResult) -> dict:
    return {
        "repository": f"{r.owner}/{r.repo}" if r.owner else None,
        "pr_number": r.pr_number,
        "branch": r.branch,
        "verdict": r.verdict,
        "risk_score": r.risk_score,
        "block_threshold": r.block_threshold,
        "reason": r.reason,
        "error": r.error,
        "files_analyzed": r.files_analyzed,
        "findings": r.findings,
        "historical_match": r.historical_match,
        "impact": r.impact,
        "suggestion": r.suggestion or None,
        "github_status_posted": r.github_status_posted,
        "timings_ms": {t.name: round(t.duration_ms, 3) for t in r.timings}
        | ({"fetch": round(r.fetch_ms, 3)} if r.fetch_ms is not None else {})
        | ({"remediation": round(r.remediation_ms, 3)} if r.remediation_ms is not None else {}),
        "verdict_path_ms": round(r.verdict_ms, 3),
        "wall_ms": round(r.total_ms, 3),
    }


if __name__ == "__main__":
    app()
