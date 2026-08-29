"""Rich rendering for the CLI. Terminal output only — no business logic here."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .pipeline import AnalysisResult

# Auto-size to a real terminal (respecting $COLUMNS); widen when piped so the
# panels don't crush down to 80.
_w = None
if not sys.stdout.isatty() and "COLUMNS" not in os.environ:
    _w = 110
console = Console(width=_w)

_VERDICT_STYLE = {"ALLOW": "bold green", "BLOCK": "bold red", "ERROR": "bold yellow"}
_VERDICT_MARK = {"ALLOW": "●", "BLOCK": "●", "ERROR": "▲"}
_SEV_STYLE = {"HIGH": "bold red", "MEDIUM": "yellow", "LOW": "dim", "NONE": "dim"}


def _dur(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1:
        return f"{ms * 1000:.0f}µs"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def _score_bar(score: int, threshold: int, width: int = 32) -> Text:
    filled = round(score / 100 * width)
    style = "red" if score >= threshold else "yellow" if score >= 40 else "green"
    bar = Text()
    bar.append("█" * filled, style=style)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f"  {score}/100", style=f"bold {style}")
    return bar


def render_error(result: AnalysisResult) -> None:
    body = Table.grid(padding=(0, 1))
    body.add_column()
    body.add_row(Text(f"PR #{result.pr_number}  •  {result.owner}/{result.repo}", style="bold"))
    body.add_row("")
    body.add_row(Text("▲ ERROR — could not complete analysis", style="bold yellow"))
    body.add_row(Text(result.error or result.reason, style="yellow"))
    if result.fetch_ms is not None:
        body.add_row(Text(f"\nfailed after {result.fetch_ms:.0f}ms at the GitHub fetch stage", style="dim"))
    console.print(Panel(body, title="[bold]PipelineAI[/bold]", border_style="yellow", box=box.ROUNDED, padding=(1, 2)))


def render_result(result: AnalysisResult, *, elapsed_note: str = "") -> None:
    if result.verdict == "ERROR":
        render_error(result)
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    vstyle = _VERDICT_STYLE.get(result.verdict, "white")

    # --- header ---------------------------------------------------------------
    head = Table.grid(padding=(0, 1))
    head.add_column(justify="left")
    repo_line = f"{result.owner}/{result.repo}" if result.owner else "(offline diff)"
    head.add_row(Text(f"PR #{result.pr_number}  •  {result.branch or 'unknown branch'}", style="bold"))
    head.add_row(Text(f"repository: {repo_line}", style="dim"))
    head.add_row(Text(f"analyzed:   {ts}", style="dim"))

    # --- provenance ---------------------------------------------------------
    prov = Table.grid(padding=(0, 1))
    prov.add_column()
    nf = result.files_analyzed
    src = (f"✓ GitHub PR fetched   ({nf} file{'s' if nf != 1 else ''}, {_dur(result.fetch_ms)})"
           if result.owner else
           f"· local diff   ({nf} file{'s' if nf != 1 else ''})")
    prov.add_row(Text(src, style="green" if result.owner else "dim"))

    # --- agents table ------------------------------------------------------
    agents = Table(box=box.SIMPLE_HEAD, expand=False, pad_edge=False)
    agents.add_column("AGENT", style="bold")
    agents.add_column("RESULT")
    agents.add_column("DURATION", justify="right", style="cyan")
    labels = {
        "impact": ("Impact Agent", lambda d: f"{d['files_analyzed']} files · {d['functions_touched']} fns · AST {d['ast_parseable']}"),
        "simulation": ("Simulation Agent", lambda d: f"{d['attack_patterns']} patterns · {d['added_lines_scanned']} lines · {d['findings']} findings"),
        "memory": ("Memory Agent", lambda d: f"{d['historical_patterns']} known patterns · {d['matches']} match"),
        "risk": ("Risk Agent", lambda d: f"score {d['risk_score']}/{d['threshold']} → {d['decision']}"),
    }
    for stage in ("impact", "simulation", "memory", "risk"):
        t = result.timing(stage)
        if not t:
            continue
        name, fmt = labels[stage]
        agents.add_row(f"✓ {name}", fmt(t.detail), _dur(t.duration_ms))

    # --- verdict block ---------------------------------------------------
    verdict = Table.grid(padding=(0, 1))
    verdict.add_column()
    verdict.add_row(Text(f"{_VERDICT_MARK.get(result.verdict, '?')} {result.verdict}", style=vstyle))
    verdict.add_row(_score_bar(result.risk_score, result.block_threshold))
    verdict.add_row(Text(result.reason, style="dim"))

    # --- findings ------------------------------------------------------
    body = [head, "", prov, "", Text("AGENTS", style="bold dim"), agents, "",
            Text("RISK VERDICT", style="bold dim"), verdict]

    if result.findings:
        ft = Table(box=box.SIMPLE, pad_edge=False, collapse_padding=True)
        ft.add_column("SEV", style="bold", min_width=6, no_wrap=True)
        ft.add_column("RULE", style="dim", min_width=20, no_wrap=True)
        ft.add_column("FINDING", ratio=1)
        ft.add_column("LOCATION", style="cyan", min_width=16, no_wrap=True)
        for f in result.findings[:12]:
            loc = os.path.basename(f["file"]) if f.get("file") else ""
            if f.get("line"):
                loc += f":{f['line']}"
            title = f["title"].rstrip(".")
            rule = f.get("rule_id") or f["category"]
            ft.add_row(
                Text(f["severity"], style=_SEV_STYLE.get(f["severity"], "white")),
                rule, title, loc,
            )
        extra = len(result.findings) - 12
        body += ["", Text("FINDINGS", style="bold dim"), ft]
        if extra > 0:
            body.append(Text(f"  … and {extra} more", style="dim"))

    if result.suggestion and result.suggestion.get("summary"):
        s = result.suggestion
        sug = Table.grid(padding=(0, 1))
        sug.add_column()
        sug.add_row(Text(f"source: {s.get('source')}   ({_dur(result.remediation_ms)})", style="dim"))
        sug.add_row(Text(s["summary"]))
        for fx in s.get("fixes", [])[:5]:
            sug.add_row(Text(f"  {fx.get('file', '')}: {fx.get('fix', '')}", style="dim"))
        body += ["", Text("AI REMEDIATION", style="bold dim"), sug]

    if result.github_status_posted is not None:
        mark = "✓ posted" if result.github_status_posted else "✗ not posted (check token permissions)"
        style = "green" if result.github_status_posted else "yellow"
        body += ["", Text(f"GitHub commit status: {mark}", style=style)]

    tl = Table.grid(padding=(0, 2))
    tl.add_column(style="dim")
    tl.add_row(f"verdict path (fetch + agents): {_dur(result.verdict_ms)}"
               + (f"   |   + remediation: {_dur(result.remediation_ms)}" if result.remediation_ms else "")
               + f"   |   wall: {_dur(result.total_ms)}")
    if elapsed_note:
        tl.add_row(elapsed_note)
    body += ["", tl]

    console.print(Panel(
        Group(*body),
        title="[bold]PipelineAI[/bold]  •  Multi-Agent PR Guardrail",
        border_style=vstyle,
        box=box.ROUNDED,
        padding=(1, 2),
    ))


def render_agent_detail(result: AnalysisResult) -> None:
    """The `--detail` view: one block per agent, TF-plan style."""
    for stage in ("impact", "simulation", "memory", "risk"):
        t = result.timing(stage)
        if not t:
            continue
        console.print(Text(f"\n{stage.upper()} AGENT", style="bold"))
        console.print(Text("─" * 40, style="dim"))
        rows = dict(t.detail)
        rows["Duration"] = _dur(t.duration_ms)
        for k, v in rows.items():
            console.print(f"  {str(k).replace('_', ' ').title():22} {v}")
