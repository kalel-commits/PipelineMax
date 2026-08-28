from agents.impact_agent import ImpactAgent

AUTH_PATCH = """@@ -1,3 +1,6 @@
 def existing():
     pass
+def validate_token(token):
+    if token is None:
+        raise ValueError("missing token")
"""

BENIGN_PATCH = """@@ -1,2 +1,3 @@
 def add(a, b):
-    return a + b
+    return a + b  # comment tweak
+
"""


def test_flags_critical_path_by_filename():
    agent = ImpactAgent()
    result = agent.analyze([{"filename": "backend/config/settings.py", "patch": BENIGN_PATCH}])
    assert result["critical_hit"] is True
    assert result["severity"] >= 6


def test_flags_critical_path_by_function_name_via_ast():
    agent = ImpactAgent()
    result = agent.analyze([{"filename": "backend/utils/helpers.py", "patch": AUTH_PATCH}])
    assert result["critical_hit"] is True
    assert "validate_token" in result["records"][0]["functions_touched"]
    assert result["records"][0]["ast_parseable"] is True


def test_non_critical_change_is_low_severity():
    agent = ImpactAgent()
    result = agent.analyze([{"filename": "backend/utils/math_helpers.py", "patch": BENIGN_PATCH}])
    assert result["critical_hit"] is False
    assert result["severity"] == 1


def test_unparseable_patch_does_not_crash():
    agent = ImpactAgent()
    garbage_patch = "@@ -1,1 +1,2 @@\n+    this is not valid python at all (((\n"
    result = agent.analyze([{"filename": "backend/x.py", "patch": garbage_patch}])
    assert result["records"][0]["functions_touched"] == []


def test_deterministic_same_input_same_output():
    agent = ImpactAgent()
    files = [{"filename": "backend/auth/login.py", "patch": AUTH_PATCH}]
    r1 = agent.analyze(files)
    r2 = agent.analyze(files)
    assert r1 == r2
