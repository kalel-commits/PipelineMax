"""Representative PR corpus for the guardrail performance benchmark.

Each fixture is a real GitHub ``pull_request`` webhook payload (the exact shape
GitHub POSTs to ``/webhook``) with an extra ``_files`` key holding the changed
files as ``GET /repos/{o}/{r}/pulls/{n}/files`` would return them. The benchmark
harness feeds ``_files`` through the in-process GitHub stub for the local run,
and ignores it for the real-GitHub run (which fetches from api.github.com).

The corpus is deliberately spread across the verdict space and the diff-size
space so the percentiles reflect the real mix of traffic, not one hot path:

  * docs / metadata / dependency-bump PRs (tiny, ALLOW)
  * ordinary feature & bugfix PRs (small/medium, ALLOW)
  * large refactors (20-40 files, ALLOW) -- exercises the per-file agent loops
  * adversarial-pattern PRs (bare except, eval, shell=True, SQL f-string,
    unchecked payload access) -- BLOCK, and the ones in a critical path also
    exercise the MemoryAgent regression lookup + persist + OpenAI remediation
"""

import copy

_HUNK = "@@ -{a},{b} +{c},{d} @@"


def _clean_py_patch(func: str, n: int = 1) -> str:
    lines = [f"+def {func}_{i}(x):" for i in range(n)]
    body = [f"+    return x * {i + 2}" for i in range(n)]
    out = []
    for h, bdy in zip(lines, body, strict=True):
        out.append(h)
        out.append(bdy)
    return _HUNK.format(a=1, b=1, c=1, d=2 * n) + "\n" + "\n".join(out) + "\n"


def _file(filename, patch, status="modified", adds=None, dels=0):
    added = patch.count("\n+") if adds is None else adds
    return {
        "filename": filename,
        "patch": patch,
        "status": status,
        "additions": added,
        "deletions": dels,
    }


def _bulk_clean(prefix: str, count: int):
    return [
        _file(f"{prefix}/module_{i}.py", _clean_py_patch(f"helper{i}", n=3))
        for i in range(count)
    ]


def _payload(number, repo_full, sha, action, files, branch="feature"):
    owner, name = repo_full.split("/")
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "head": {"sha": sha, "ref": branch},
            "base": {"ref": "main"},
        },
        "repository": {"name": name, "owner": {"login": owner}, "full_name": repo_full},
        "_files": files,
    }


# --- adversarial patches (BLOCK) -------------------------------------------------

_BARE_EXCEPT = (
    _HUNK.format(a=10, b=4, c=10, d=8)
    + "\n+def parse_token(raw):\n+    try:\n+        return decode(raw)\n"
    "+    except:\n+        return None\n"
)

_EVAL = (
    _HUNK.format(a=1, b=2, c=1, d=6)
    + "\n+def load_config(text):\n+    settings = eval(text)\n"
    "+    return settings\n"
)

_SHELL_TRUE = (
    _HUNK.format(a=5, b=2, c=5, d=6)
    + "\n+import subprocess\n+def run_hook(cmd):\n"
    "+    return subprocess.run(cmd, shell=True, capture_output=True)\n"
)

_SQL_FSTRING = (
    _HUNK.format(a=20, b=3, c=20, d=8)
    + "\n+def get_user(cur, uid):\n"
    "+    cur.execute(f\"SELECT * FROM users WHERE id = {uid}\")\n"
    "+    return cur.fetchone()\n"
)

_UNCHECKED_PAYLOAD = (
    _HUNK.format(a=1, b=2, c=1, d=6)
    + "\n+def handle(request):\n+    user = request['user']\n"
    "+    scope = payload['scope']\n+    return user, scope\n"
)


def _big_mixed_block():
    files = _bulk_clean("src/core", 14)
    files.append(_file("src/parser/lexer.py", _BARE_EXCEPT))
    return files


# --- the corpus ----------------------------------------------------------------
# (number, repo, sha, action, files, expected_verdict)
_SPEC = [
    (101, "octo/webapp", "a1" * 20, "opened", [_file("docs/CHANGELOG.md", _HUNK.format(a=1, b=1, c=1, d=2) + "\n+### 2.1.0\n+- bugfixes\n")], "ALLOW"),
    (102, "octo/webapp", "a2" * 20, "opened", [_file("requirements.txt", _HUNK.format(a=3, b=1, c=3, d=1) + "\n+httpx>=0.28.0\n", dels=1)], "ALLOW"),
    (103, "octo/webapp", "a3" * 20, "synchronize", [_file("README.md", _HUNK.format(a=8, b=1, c=8, d=2) + "\n+Fixed a typo in the setup section.\n")], "ALLOW"),
    (104, "octo/webapp", "a4" * 20, "opened", _bulk_clean("src/api", 2), "ALLOW"),
    (105, "octo/webapp", "a5" * 20, "opened", _bulk_clean("src/api", 4), "ALLOW"),
    (106, "octo/webapp", "a6" * 20, "synchronize", _bulk_clean("src/services", 8), "ALLOW"),
    (107, "octo/webapp", "a7" * 20, "opened", _bulk_clean("src/services", 12), "ALLOW"),
    (108, "octo/webapp", "a8" * 20, "opened", _bulk_clean("src/core", 22), "ALLOW"),
    (109, "octo/webapp", "a9" * 20, "opened", _bulk_clean("src/core", 38), "ALLOW"),
    (110, "octo/webapp", "b1" * 20, "opened", [_file("tests/test_users.py", _clean_py_patch("test_case", n=6))], "ALLOW"),
    (111, "octo/webapp", "b2" * 20, "synchronize", _bulk_clean("src/ui/components", 6), "ALLOW"),
    (112, "octo/webapp", "b3" * 20, "opened", [_file("src/utils/formatting.py", _clean_py_patch("fmt", n=4)), _file("src/utils/dates.py", _clean_py_patch("parse_date", n=3))], "ALLOW"),
    # non-python source (no AST path)
    (113, "octo/webapp", "b4" * 20, "opened", [_file("src/frontend/app.ts", _HUNK.format(a=1, b=1, c=1, d=3) + "\n+export const x = 1;\n+export const y = 2;\n")], "ALLOW"),
    # BLOCK cases
    (201, "octo/webapp", "c1" * 20, "opened", [_file("src/parser/lexer.py", _BARE_EXCEPT)], "BLOCK"),
    (202, "octo/webapp", "c2" * 20, "opened", [_file("src/config/loader.py", _EVAL)], "BLOCK"),
    (203, "octo/webapp", "c3" * 20, "synchronize", [_file("src/ci/hooks.py", _SHELL_TRUE)], "BLOCK"),
    (204, "octo/webapp", "c4" * 20, "opened", [_file("src/db/queries.py", _SQL_FSTRING)], "BLOCK"),
    # unchecked-payload access is severity 6: on its own it does NOT trip the gate
    # (needs a prior stored regression of the same pattern), so a fresh lexicon -> ALLOW.
    (205, "octo/webapp", "c5" * 20, "opened", [_file("src/auth/session.py", _UNCHECKED_PAYLOAD)], "ALLOW"),
    (206, "octo/webapp", "c6" * 20, "opened", [_file("src/auth/tokens.py", _BARE_EXCEPT)], "BLOCK"),
    (207, "octo/webapp", "c7" * 20, "synchronize", _big_mixed_block(), "BLOCK"),
    (208, "octo/webapp", "c8" * 20, "opened", [_file("src/parser/json_parser.py", _EVAL), _file("src/parser/yaml_parser.py", _BARE_EXCEPT)], "BLOCK"),
    (209, "octo/webapp", "c9" * 20, "opened", _bulk_clean("src/misc", 5) + [_file("src/config/settings.py", _EVAL)], "BLOCK"),
    (210, "octo/webapp", "d1" * 20, "opened", [_file("src/payment/charge.py", _SQL_FSTRING)], "BLOCK"),
    (211, "octo/webapp", "d2" * 20, "synchronize", _bulk_clean("src/big", 30) + [_file("src/security/acl.py", _SHELL_TRUE)], "BLOCK"),
]

FIXTURES = [
    {"payload": _payload(n, repo, sha, action, files), "expected": expected}
    for (n, repo, sha, action, files, expected) in _SPEC
]


def get_fixtures():
    """Fresh deep copies so a run that mutates payloads can't leak into the next."""
    return [copy.deepcopy(f) for f in FIXTURES]


# Real, public, already-merged PRs for the api.github.com-included benchmark.
# Small diffs on purpose: the point is to measure real GitHub round-trip latency
# on the retrieval step, not to stress GitHub. Rotated through by the harness.
REAL_PRS = [
    ("encode", "httpx", 3773),
    ("encode", "httpx", 3699),
    ("psf", "requests", 7609),
    ("pallets", "click", 3781),
    ("pallets", "flask", 6133),
]
