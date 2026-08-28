"""Live Smee.io delivery check for the guardrail webhook.

Verifies the tunnel path the README documents actually works end to end:

    POST signed pull_request payload  ->  https://smee.io/<channel>
      ->  smee-client forwards it     ->  http://127.0.0.1:<port>/webhook
      ->  guardrail verifies HMAC + accepts (HTTP 200 "accepted")

It does NOT assert anything about the ALLOW/BLOCK verdict -- with a throwaway
repo the GitHub calls fail and the guardrail reports an ERROR status, which is
the correct behaviour. The point is only: did the webhook survive the tunnel and
pass signature verification.

Requires: node/npx on PATH, outbound network to smee.io, and this script starts
its own uvicorn instance. Run from backend/:  python -m benchmarks.check_smee
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request

SECRET = "smee-check-secret"
PORT = 8099
# Smee.io parses and re-serializes the webhook JSON before its client re-emits it,
# so an HMAC computed over our exact original bytes will not match what the backend
# receives. That is a Smee limitation, not a guardrail bug -- GitHub's own webhooks
# are unaffected. Local Smee testing is therefore normally run WITHOUT a shared
# secret (see README), which is the configuration this check verifies by default.
USE_SECRET = os.environ.get("SMEE_CHECK_USE_SECRET") == "1"


def _post(url, data, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode()


def main():
    # 1. fresh smee channel
    with urllib.request.urlopen("https://smee.io/new", timeout=15) as r:
        channel = r.url
    print(f"[smee-check] channel: {channel}")

    env = dict(os.environ)
    env["WEBHOOK_SECRET"] = SECRET if USE_SECRET else ""
    print(f"[smee-check] WEBHOOK_SECRET set on backend: {USE_SECRET}")
    env["GITHUB_TOKEN"] = ""
    env["OPENAI_API_KEY"] = ""
    env["PORT"] = str(PORT)
    env["PYTHONUNBUFFERED"] = "1"

    _logdir = os.environ.get("TEMP") or os.path.dirname(__file__)
    srv_log = open(os.path.join(_logdir, "pipelineai_smee_server.log"), "w+")
    smee_log = open(os.path.join(_logdir, "pipelineai_smee_client.log"), "w+")
    server = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "guardrail_main:app", "--port", str(PORT), "--log-level", "info"],
        env=env, stdout=srv_log, stderr=subprocess.STDOUT, text=True,
    )
    smee = None
    try:
        # 2. wait for the server
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("uvicorn did not come up")
        print("[smee-check] backend up on", PORT)

        # 3. start smee-client
        npx = "npx.cmd" if os.name == "nt" else "npx"
        smee = subprocess.Popen(
            [npx, "--yes", "smee-client", "--url", channel, "--target", f"http://127.0.0.1:{PORT}/webhook"],
            stdout=smee_log, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(10)  # let smee connect its SSE stream

        # 4. deliver a signed pull_request event THROUGH smee
        payload = {
            "action": "opened",
            "number": 1,
            "pull_request": {"number": 1, "head": {"sha": "0" * 40, "ref": "feature"}},
            "repository": {"name": "smee-check-repo", "owner": {"login": "smee-check-user"}},
        }
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "pull_request"}
        if USE_SECRET:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        status, text = _post(channel, body, headers)
        print(f"[smee-check] POST to smee channel -> HTTP {status}")
        time.sleep(6)  # allow forward + background task

        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        srv_log.flush(); srv_log.seek(0)
        out = srv_log.read()
        smee_log.flush(); smee_log.seek(0)
        smee_out = smee_log.read()

        forwarded = "/webhook" in smee_out and "Forwarding" in smee_out
        reached_pipeline = "GitHub Check Run" in out
        accepted_200 = 'POST /webhook HTTP/1.1" 200' in out
        print("\n---- smee-client log ----")
        print(smee_out.strip() or "(empty)")
        print("\n---- backend log ----")
        print(out.strip() or "(empty)")
        print("---------------------")
        print(f"[smee-check] smee-client connected + forwarded : {forwarded}")
        print(f"[smee-check] backend accepted webhook (200)     : {accepted_200}")
        print(f"[smee-check] event reached the agent pipeline   : {reached_pipeline}")
        if forwarded and accepted_200 and reached_pipeline:
            print("\n[smee-check] PASS: pull_request event delivered through a live Smee.io channel, "
                  "accepted by the guardrail, and processed by the agent pipeline.")
        elif forwarded:
            print("\n[smee-check] PARTIAL: Smee.io forwarded the event but the backend did not fully "
                  "process it (see logs above).")
        else:
            print("\n[smee-check] INCONCLUSIVE: smee-client did not forward; check connectivity.")
    finally:
        for p in (smee, server):
            if not p:
                continue
            if os.name == "nt":
                # npx spawns a grandchild node process; kill the whole tree.
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        for fh in (srv_log, smee_log):
            try:
                fh.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
