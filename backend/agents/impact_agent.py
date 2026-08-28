import ast
import re
from typing import List, Dict, Any

CRITICAL_PATH_KEYWORDS = ["auth", "parser", "config", "security", "payment", "token", "session"]

_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)


def _added_lines(patch: str) -> List[str]:
    """Pull just the added (+) lines out of a unified diff hunk, stripped of the marker."""
    return [m.group(1) for m in _ADDED_LINE_RE.finditer(patch or "")]


def _touched_function_names(added_source: str) -> List[str]:
    """Real AST parse of the added lines. Diff hunks are often not standalone-valid
    Python (partial blocks, dangling indentation), so this only returns names when
    the hunk happens to parse — callers must not assume a non-empty result on every
    file. This is the "semantic" signal: which functions/classes this change defines
    or touches, as opposed to a filename substring guess."""
    try:
        tree = ast.parse(added_source)
    except (SyntaxError, ValueError):
        return []

    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


class ImpactAgent:
    """Semantic blast-radius analysis: does this change reach a critical path
    (auth/config/parsing/etc.), and how confidently do we know that (AST-parseable
    vs. filename-keyword fallback)?"""

    def __init__(self, critical_keywords: List[str] = None):
        self.critical_keywords = critical_keywords or CRITICAL_PATH_KEYWORDS

    def analyze(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        records = []
        critical_hit = False
        max_severity = 1
        touched_flow = "Unknown"
        touched_path = ""

        for f in files:
            filename = f.get("filename", "")
            patch = f.get("patch", "")
            added_source = "\n".join(_added_lines(patch))

            functions_touched = _touched_function_names(added_source) if filename.endswith(".py") else []
            ast_parseable = bool(functions_touched) or (filename.endswith(".py") and not added_source.strip())

            filename_critical = any(k in filename.lower() for k in self.critical_keywords)
            function_critical = any(
                any(k in fn.lower() for k in self.critical_keywords) for fn in functions_touched
            )
            file_critical = filename_critical or function_critical

            severity = 1
            if file_critical:
                severity = 8 if function_critical else 6
                critical_hit = True
                touched_flow = "Authentication/Config Flow" if any(
                    k in filename.lower() for k in ("auth", "config", "session", "token")
                ) else "Parsing Flow"
                touched_path = filename

            max_severity = max(max_severity, severity)
            records.append(
                {
                    "file": filename,
                    "functions_touched": functions_touched,
                    "ast_parseable": ast_parseable,
                    "critical_path": file_critical,
                    "severity": severity,
                }
            )

        return {
            "flow": touched_flow,
            "path": touched_path,
            "severity": max_severity,
            "critical_hit": critical_hit,
            "records": records,
        }
