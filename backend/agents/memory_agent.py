import json
import os
import hashlib
import time
from typing import List, Dict, Any, Optional

_DEFAULT_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "regression_lexicon.json")


def _signature(category: str, rule_id: str) -> str:
    """A stable id for 'this kind of failure, in this kind of code' so repeats
    across unrelated PRs/files still get recognized as the same regression pattern."""
    return hashlib.sha256(f"{category}:{rule_id}".encode()).hexdigest()[:16]


class MemoryAgent:
    """Institutional memory of past guardrail failures, backed by a JSON file so
    it survives process restarts (create -> save -> restart -> retrieve)."""

    def __init__(self, store_path: str = _DEFAULT_STORE_PATH):
        self.store_path = os.path.abspath(store_path)
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        if not os.path.exists(self.store_path):
            self._write({})
        # The lexicon is small and this is a single-instance service (see README),
        # so hold it in memory: every PR does a lookup, and re-reading + re-parsing
        # the JSON file on each webhook was ~15-25ms of blocking I/O per request on
        # the hot path for no benefit. Disk is still the source of truth on restart
        # and is rewritten on every stored failure.
        self._cache: Dict[str, Any] = self._read()

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.store_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        with open(self.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        self._cache = data

    def check_lexicon(self, issues: List[Dict[str, Any]]) -> Optional[str]:
        """Given this PR's detected issues, has this exact failure pattern been
        seen (and recorded as a real failure) before?"""
        if not issues:
            return None
        lexicon = self._cache
        for issue in issues:
            sig = _signature(issue.get("category", ""), issue.get("rule_id", ""))
            entry = lexicon.get(sig)
            if entry and entry.get("occurrences", 0) >= 1:
                return (
                    f"This '{issue.get('category')}' pattern ({issue.get('rule_id')}) "
                    f"has triggered the guardrail {entry['occurrences']} time(s) before, "
                    f"most recently in {entry.get('last_file', 'a previous PR')}."
                )
        return None

    def store_failure(self, pr_data: Dict[str, Any], issues: List[Dict[str, Any]]) -> None:
        """Persist this failure so future PRs with the same pattern are recognized."""
        if not issues:
            return
        lexicon = dict(self._cache)
        for issue in issues:
            sig = _signature(issue.get("category", ""), issue.get("rule_id", ""))
            entry = dict(lexicon.get(sig, {"occurrences": 0}))  # copy: don't mutate the live cache in place
            entry["occurrences"] = entry.get("occurrences", 0) + 1
            entry["last_file"] = issue.get("file")
            entry["last_pr"] = pr_data.get("id")
            entry["last_seen"] = time.time()
            entry["description"] = issue.get("description")
            lexicon[sig] = entry
        self._write(lexicon)
