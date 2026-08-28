import re
from typing import List, Dict, Any

# Each rule is a deterministic pattern match against the *added* lines of a diff.
# These stand in for "adversarial testing": instead of executing the PR's code
# against malformed input in a sandbox, we statically flag the code shapes that are
# known to fail under malformed/adversarial input (unhandled exceptions, injection
# vectors). Same input always produces the same output — no randomness anywhere.
_RULES = [
    {
        "id": "bare_except",
        "pattern": re.compile(r"^\s*except\s*:\s*$", re.MULTILINE),
        "category": "error-handling",
        "severity": 7,
        "message": "Bare 'except:' swallows all errors, including from malformed/adversarial input.",
        "recommendation": "Catch a specific exception type (e.g. `except ValueError:`) and handle or re-raise it.",
    },
    {
        "id": "eval_exec",
        "pattern": re.compile(r"\b(eval|exec)\s*\("),
        "category": "security",
        "severity": 9,
        "message": "Use of eval()/exec() on data that may come from an adversarial payload.",
        "recommendation": "Replace eval/exec with explicit parsing (json.loads, ast.literal_eval, or a dedicated parser).",
    },
    {
        "id": "shell_injection",
        "pattern": re.compile(r"(os\.system\(|subprocess\.\w+\([^)]*shell\s*=\s*True)"),
        "category": "security",
        "severity": 9,
        "message": "Shell command built with shell=True / os.system is vulnerable to command injection.",
        "recommendation": "Use subprocess.run([...], shell=False) with an argument list instead of a shell string.",
    },
    {
        "id": "sql_string_concat",
        "pattern": re.compile(r"(execute|executemany)\s*\(\s*(f[\"']|[\"'].*%s.*[\"']\s*%|[\"'].*\+)"),
        "category": "security",
        "severity": 9,
        "message": "SQL query built via string concatenation/f-string is vulnerable to SQL injection.",
        "recommendation": "Use parameterized queries: cursor.execute(query, (param1, param2)).",
    },
    {
        "id": "unchecked_payload_access",
        "pattern": re.compile(r"\b(payload|data|body|request)\[[\"'][^\]]+[\"']\]"),
        "category": "reliability",
        "severity": 6,
        "message": "Direct key access on external input can raise KeyError under a malformed/adversarial payload.",
        "recommendation": "Use .get('key') with a default, or validate required keys before indexing.",
    },
]


class SimulationAgent:
    """Deterministic static scan that plays the role of 'adversarial testing':
    it looks for code shapes that are known to break under malformed/hostile
    input, without executing any submitted code."""

    def analyze(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []

        for f in files:
            filename = f.get("filename", "")
            patch = f.get("patch", "")
            if not patch:
                continue

            for line_no, line in enumerate(patch.splitlines(), start=1):
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                content = line[1:]
                for rule in _RULES:
                    if rule["pattern"].search(content):
                        issues.append(
                            {
                                "file": filename,
                                "line": line_no,
                                "category": rule["category"],
                                "severity": rule["severity"],
                                "rule_id": rule["id"],
                                "description": rule["message"],
                                "evidence": content.strip(),
                                "recommendation": rule["recommendation"],
                            }
                        )

        failed = any(issue["severity"] >= 7 for issue in issues)
        return {"failed": failed, "issues": issues}
