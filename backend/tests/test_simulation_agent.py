from agents.simulation_agent import SimulationAgent

BARE_EXCEPT_PATCH = """@@ -1,3 +1,6 @@
 def parse(data):
     try:
         return data["value"]
+    except:
+        return None
"""

EVAL_PATCH = """@@ -1,2 +1,3 @@
 def run(cmd):
+    return eval(cmd)
"""

SQL_INJECTION_PATCH = """@@ -1,2 +1,3 @@
 def query(name):
+    cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")
"""

UNCHECKED_ACCESS_PATCH = """@@ -1,2 +1,3 @@
 def handler(request):
+    user_id = payload["user_id"]
"""

CLEAN_PATCH = """@@ -1,2 +1,3 @@
 def add(a, b):
+    return a + b
"""


def test_bare_except_flagged_and_blocks():
    result = SimulationAgent().analyze([{"filename": "backend/parser.py", "patch": BARE_EXCEPT_PATCH}])
    assert result["failed"] is True
    assert any(i["rule_id"] == "bare_except" for i in result["issues"])


def test_eval_flagged():
    result = SimulationAgent().analyze([{"filename": "backend/runner.py", "patch": EVAL_PATCH}])
    assert result["failed"] is True
    assert any(i["rule_id"] == "eval_exec" for i in result["issues"])


def test_sql_injection_flagged():
    result = SimulationAgent().analyze([{"filename": "backend/db.py", "patch": SQL_INJECTION_PATCH}])
    assert result["failed"] is True
    assert any(i["rule_id"] == "sql_string_concat" for i in result["issues"])


def test_unchecked_payload_access_flagged_but_low_severity():
    result = SimulationAgent().analyze([{"filename": "backend/api.py", "patch": UNCHECKED_ACCESS_PATCH}])
    issue_ids = [i["rule_id"] for i in result["issues"]]
    assert "unchecked_payload_access" in issue_ids
    # severity 6 alone shouldn't trip the fail threshold (>=7)
    assert result["failed"] is False


def test_clean_code_passes():
    result = SimulationAgent().analyze([{"filename": "backend/math_ops.py", "patch": CLEAN_PATCH}])
    assert result["failed"] is False
    assert result["issues"] == []


def test_deterministic_across_repeated_calls():
    agent = SimulationAgent()
    files = [{"filename": "backend/parser.py", "patch": BARE_EXCEPT_PATCH}]
    results = [agent.analyze(files) for _ in range(10)]
    assert all(r == results[0] for r in results)
