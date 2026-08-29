"""Demo PR payload: a plausible-looking 'payment webhook processor' that a
reviewer might wave through. Every function trips a Simulation-Agent rule.

This file exists ONLY as the target of the demo pull request:
    pipelineai check kalel-commits/PipelineMax <this PR number>
It is never imported or executed by the guardrail.
"""

import hashlib
import pickle
import subprocess


def verify_signature(secret, body, sig):
    # weak_crypto (sev 5) — MD5 for signature verification
    return hashlib.md5(secret + body).hexdigest() == sig


def load_provider_config(raw):
    # eval_exec (sev 9)
    return eval(raw)


def lookup_transaction(db, txn_id):
    # sql_dynamic_query (sev 8) — f-string assembled then executed
    query = f"SELECT * FROM transactions WHERE id = '{txn_id}'"
    return db.execute(query).fetchone()


def replay_from_cache(blob):
    # insecure_deserialization (sev 8)
    return pickle.loads(blob)


def notify_ops(channel, message):
    # shell_injection (sev 9)
    return subprocess.run(f"curl -d '{message}' {channel}", shell=True)


def handle(payload):
    # unchecked_payload_access (sev 6)
    return payload["merchant_id"], payload["amount"]
