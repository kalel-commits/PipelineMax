"""Demo fixture: every function here trips a Simulation-Agent rule.

    pipelineai check --local examples/vulnerable_sample.py   ->  BLOCK

This file is NOT imported or executed by the guardrail — it exists only so a
live demo has a deterministic BLOCK to show. See examples/safe_sample.py for the
ALLOW counterpart.
"""

import hashlib
import pickle
import subprocess

import requests  # type: ignore


def run_report(cmd):
    # shell_injection (sev 9)
    return subprocess.run(cmd, shell=True, capture_output=True)


def load_config(text):
    # eval_exec (sev 9)
    return eval(text)


def get_user(db, user_id):
    # sql_dynamic_query (sev 8) — assign-then-execute
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query).fetchone()


def restore_session(blob):
    # insecure_deserialization (sev 8)
    return pickle.loads(blob)


def fetch(url):
    # disabled_tls_verification (sev 7)
    return requests.get(url, verify=False)


def parse_event(payload):
    # unchecked_payload_access (sev 6)
    return payload["user"], payload["scope"]


def digest(password):
    # weak_crypto (sev 5)
    return hashlib.md5(password.encode()).hexdigest()
