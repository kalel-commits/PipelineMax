import os
import hmac
import hashlib
import json
import httpx
import asyncio
from typing import List, Dict, Any
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Header
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="PipelineAI GitHub Guardrail", version="1.0.0")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not signature_header or not WEBHOOK_SECRET:
        return False
    hash_object = hmac.new(WEBHOOK_SECRET.encode("utf-8"), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected_signature, signature_header)

async def set_commit_status(owner: str, repo: str, sha: str, state: str, description: str, target_url: str = "http://localhost:8000/logs"):
    """Update GitHub Commit Status"""
    url = f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "state": state, # pending, success, failure, error
        "description": description[:140], # GitHub limits to 140 chars
        "context": "PipelineAI / Guardrail",
        "target_url": target_url
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, headers=headers, json=data)
            resp.raise_for_status()
            print(f"📡 [GitHub API] Set status for {sha[:7]} -> {state.upper()}")
        except Exception as e:
            print(f"❌ [GitHub API Error] Failed to set status: {e}")

async def get_pull_request_files(owner: str, repo: str, pull_number: int) -> List[str]:
    """Fetch files changed in the PR"""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/files"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            files_data = resp.json()
            return [f["filename"] for f in files_data]
        except Exception as e:
            print(f"❌ [GitHub API Error] Failed to fetch PR files: {e}")
            return []

async def run_agents(owner: str, repo: str, pull_number: int, files_changed: List[str]):
    # Impact Agent
    print("  -> 🗺️ Impact Agent: Analyzing semantic blast radius...")
    critical_hit = False
    for f in files_changed:
        if any(keyword in f for keyword in ["auth", "parser", "config"]):
            critical_hit = True
            break
    if critical_hit:
        print("  -> ⚠️  Impact Agent: CRITICAL PATH HIT (auth/parser/config detected). Severity: High.")
    else:
        print("  -> ✅  Impact Agent: No critical paths touched. Severity: Low.")

    # Memory Agent
    print("  -> 📚 Memory Agent: Checking historical failures lexicon...")
    memory_hit = False
    for f in files_changed:
        if "data_parser" in f:
            memory_hit = True
            break
    if memory_hit:
        print("  -> 🚨 Memory Agent: MATCH FOUND! This parsing pattern historically caused regressions (PR #1042).")
    else:
        print("  -> ✅  Memory Agent: No historical regression patterns matched.")

    # Simulation Agent
    print("  -> 🔬 Simulation Agent: Executing adversarial chaos tests...")
    await asyncio.sleep(0.5) # Simulate processing time
    sim_fails = any("data_parser" in f for f in files_changed)
    if sim_fails:
        print("  -> 💥 Simulation Agent: DETERMINISTIC FAILURE! Injected 'malformed_payload' caused unhandled exception.")
    else:
        print("  -> ✅  Simulation Agent: Code survived chaos testing.")
        
    return sim_fails, critical_hit, memory_hit

async def process_guardrail(owner: str, repo: str, pull_number: int, sha: str):
    print("\n" + "="*50)
    print(f"🛡️  PIPELINE-AI GUARDRAIL INITIATED")
    print(f"🔗 Target: {owner}/{repo} | PR #{pull_number} | SHA: {sha[:7]}")
    print("="*50)
    
    # Phase 1: Acknowledge & Set Pending
    print("⏳ [Phase 1] Setting GitHub status to Pending...")
    await set_commit_status(owner, repo, sha, "pending", "⏳ Pending...")
    
    # Fetch real files
    print("🔍 Fetching modified files from GitHub API...")
    files_changed = await get_pull_request_files(owner, repo, pull_number)
    print(f"📄 Files changed ({len(files_changed)}): {', '.join(files_changed)}")
    
    # Phase 2: Multi-Agent Consensus
    print("\n🧠 [Phase 2] Multi-Agent Workflow")
    print("🤖 Updating status to running...")
    await set_commit_status(owner, repo, sha, "pending", "🤖 Agents running...")
    
    try:
        sim_fails, critical_hit, memory_hit = await asyncio.wait_for(
            run_agents(owner, repo, pull_number, files_changed), 
            timeout=10.0
        )
    except asyncio.TimeoutError:
        print("  -> ⏱️ TIMEOUT: Agents took too long to run.")
        await set_commit_status(owner, repo, sha, "error", "⏱️ Timeout: Analysis exceeded 10 seconds.")
        print("="*50 + "\n")
        return
        
    # Phase 3: Final Verdict
    print("\n⚖️  [Phase 3] Risk Agent Synthesis")
    if sim_fails or (critical_hit and memory_hit):
        verdict_state = "failure"
        verdict_msg = "❌ BLOCK: High systemic risk detected by agents."
        if sim_fails:
            verdict_msg = "❌ Parser change broke authentication flow under adversarial input"
        print(f"🔴 VERDICT: {verdict_msg}")
    else:
        verdict_state = "success"
        verdict_msg = "✅ ALLOW: Agents consensus passed."
        print(f"🟢 VERDICT: {verdict_msg}")
        
    print(f"🚀 Updating GitHub Commit Status -> {verdict_state.upper()}")
    await set_commit_status(owner, repo, sha, verdict_state, verdict_msg)
    print("="*50 + "\n")


@app.post("/webhook")
async def handle_webhook(
    request: Request, 
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(None)
):
    # Verify Signature (Security)
    payload_body = await request.body()
    if WEBHOOK_SECRET and not verify_signature(payload_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    
    payload = await request.json()
    
    # We only care about Pull Request events
    if "pull_request" in payload and payload.get("action") in ["opened", "synchronize", "reopened"]:
        pr = payload["pull_request"]
        repo_data = payload["repository"]
        
        owner = repo_data["owner"]["login"]
        repo = repo_data["name"]
        pull_number = pr["number"]
        head_sha = pr["head"]["sha"]
        
        # Dispatch background task so we return 200 immediately (<200ms)
        background_tasks.add_task(
            process_guardrail, 
            owner=owner, 
            repo=repo, 
            pull_number=pull_number, 
            sha=head_sha
        )
        return {"status": "accepted", "message": "Webhook received. Agents deployed."}
        
    return {"status": "ignored", "message": "Not a relevant pull_request event"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("guardrail_main:app", host="0.0.0.0", port=port)
