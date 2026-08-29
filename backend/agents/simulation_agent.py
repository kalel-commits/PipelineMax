import re
from typing import List, Dict, Any

# Each rule is a deterministic pattern match against the *added* lines of a diff.
# This stands in for "adversarial testing": instead of executing the PR's code
# against malformed input in a sandbox (which would mean running attacker-
# controlled code), we statically flag the code shapes that are known to fail --
# or be exploited -- under malformed/adversarial input. Same input always
# produces the same output; there is no randomness and nothing is executed.
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
        "message": "SQL query passed to execute() is built via string formatting/concatenation (SQL injection).",
        "recommendation": "Use parameterized queries: cursor.execute(query, (param1, param2)).",
    },
    {
        # Catches the assign-then-execute pattern the inline rule above misses:
        #   q = f\"SELECT ... {user_input} ...\"; db.execute(q)
        "id": "sql_dynamic_query",
        "pattern": re.compile(
            r"""=\s*(f["']|["'].*["']\s*\.format\(|["'].*['"]\s*[+%])"""
            r""".*\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|FROM|WHERE)\b""",
            re.IGNORECASE,
        ),
        "category": "security",
        "severity": 8,
        "message": "SQL query string assembled with an f-string / .format / concatenation of user data.",
        "recommendation": "Build the query with placeholders and pass parameters to the driver, never interpolate.",
    },
    {
        "id": "insecure_deserialization",
        "pattern": re.compile(
            r"\b(pickle\.loads?|cPickle\.loads?|marshal\.loads|yaml\.load)\s*\(",
        ),
        "category": "security",
        "severity": 8,
        "message": "Deserializing untrusted data with pickle/marshal/yaml.load can execute arbitrary code.",
        "recommendation": "Use json, or yaml.safe_load, or a schema-validated parser for external input.",
    },
    {
        "id": "disabled_tls_verification",
        "pattern": re.compile(
            r"(verify\s*=\s*False|ssl\._create_unverified_context|check_hostname\s*=\s*False|"
            r"CERT_NONE|PYTHONHTTPSVERIFY\s*=\s*['\"]?0)",
        ),
        "category": "security",
        "severity": 7,
        "message": "TLS certificate verification is disabled — traffic can be silently MITM'd.",
        "recommendation": "Remove verify=False / unverified context; pin or trust the correct CA bundle instead.",
    },
    {
        "id": "hardcoded_secret",
        "pattern": re.compile(
            r"\b(password|passwd|secret|secret_key|api_key|apikey|access_key|private_key|auth_token)\b"
            r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
            re.IGNORECASE,
        ),
        "category": "security",
        "severity": 6,
        "message": "Possible hardcoded credential/secret assigned to a literal string.",
        "recommendation": "Load secrets from the environment or a secrets manager; never commit them.",
    },
    {
        "id": "weak_crypto",
        "pattern": re.compile(r"hashlib\.(md5|sha1)\s*\(|\bDES\b|\bRC4\b"),
        "category": "security",
        "severity": 5,
        "message": "Weak/broken hash or cipher (MD5/SHA1/DES/RC4) — unsuitable for security use.",
        "recommendation": "Use SHA-256+ for integrity and a KDF (bcrypt/argon2/scrypt/PBKDF2) for passwords.",
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

# Number of deterministic attack/failure patterns this agent evaluates.
RULE_COUNT = len(_RULES)


class SimulationAgent:
    """Deterministic static scan that plays the role of 'adversarial testing':
    it looks for code shapes known to break or be exploited under malformed /
    hostile input, without executing any submitted code."""

    def analyze(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        issues: List[Dict[str, Any]] = []
        added_lines_scanned = 0

        for f in files:
            filename = f.get("filename", "")
            patch = f.get("patch", "")
            if not patch:
                continue

            for line_no, line in enumerate(patch.splitlines(), start=1):
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                added_lines_scanned += 1
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
        return {
            "failed": failed,
            "issues": issues,
            "rules_evaluated": RULE_COUNT,
            "added_lines_scanned": added_lines_scanned,
        }
