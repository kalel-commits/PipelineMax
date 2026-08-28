from agents.memory_agent import MemoryAgent

ISSUE = {
    "file": "backend/parser.py",
    "line": 4,
    "category": "error-handling",
    "rule_id": "bare_except",
    "description": "Bare except swallows errors.",
    "recommendation": "Catch a specific exception.",
}


def test_no_match_before_any_failure_stored(tmp_path):
    agent = MemoryAgent(store_path=str(tmp_path / "lexicon.json"))
    assert agent.check_lexicon([ISSUE]) is None


def test_store_then_check_matches(tmp_path):
    store = str(tmp_path / "lexicon.json")
    agent = MemoryAgent(store_path=store)
    agent.store_failure({"id": 1}, [ISSUE])
    assert agent.check_lexicon([ISSUE]) is not None


def test_persists_across_new_instances_simulating_restart(tmp_path):
    store = str(tmp_path / "lexicon.json")
    agent_before_restart = MemoryAgent(store_path=store)
    agent_before_restart.store_failure({"id": 1}, [ISSUE])

    # Simulate a process restart: brand-new instance, same backing file.
    agent_after_restart = MemoryAgent(store_path=store)
    match = agent_after_restart.check_lexicon([ISSUE])
    assert match is not None
    assert "1 time" in match


def test_unrelated_issue_does_not_match(tmp_path):
    store = str(tmp_path / "lexicon.json")
    agent = MemoryAgent(store_path=store)
    agent.store_failure({"id": 1}, [ISSUE])

    other_issue = {**ISSUE, "category": "security", "rule_id": "eval_exec"}
    assert agent.check_lexicon([other_issue]) is None
