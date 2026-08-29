"""PipelineAI — multi-agent GitHub Pull Request guardrail.

Terminal-first. The public surface is the ``pipelineai`` CLI (``pipelineai.cli``);
the analysis pipeline it drives lives in ``pipelineai.pipeline`` and is the exact
same code path the FastAPI webhook server (``guardrail_main``) runs.
"""

import sys as _sys

# Windows consoles still default to a legacy code page; force UTF-8 so the box
# drawing / symbols render instead of mojibake. No-op where already UTF-8.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

__version__ = "0.3.0"
