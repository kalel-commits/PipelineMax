"""Demo fixture: the safe counterpart of vulnerable_sample.py.

    pipelineai check --local examples/safe_sample.py   ->  ALLOW

Same operations, done safely — no Simulation-Agent rule fires.
"""

import hashlib
import json
import subprocess


def run_report(args: list[str]):
    return subprocess.run(args, shell=False, capture_output=True, check=True)


def load_config(text: str) -> dict:
    return json.loads(text)


def get_user(db, user_id: int):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def restore_session(blob: str) -> dict:
    return json.loads(blob)


def fetch(url: str):
    import requests  # type: ignore

    return requests.get(url, timeout=10)  # verification on by default


def parse_event(payload: dict):
    return payload.get("user"), payload.get("scope")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
