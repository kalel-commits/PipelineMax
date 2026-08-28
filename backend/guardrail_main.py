import os
import hmac
import hashlib
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from integrations.github_client import GitHubClient
from integrations.github_checks import GitHubChecksAPI
from integrations.webhook_handler import WebhookHandler
from agents.impact_agent import ImpactAgent
from agents.memory_agent import MemoryAgent
from agents.simulation_agent import SimulationAgent
from agents.risk_agent import RiskAgent
from utils.ai_remediation import AIRemediation

load_dotenv()


@asynccontextmanager
async def lifespan(_app):
    if not WEBHOOK_SECRET:
        print(
            "[Guardrail] WARNING: WEBHOOK_SECRET is not set. Incoming /webhook requests "
            "are NOT signature-verified (demo mode). Set WEBHOOK_SECRET for any "
            "internet-reachable deployment.",
            flush=True,
        )
    if not GITHUB_TOKEN:
        print(
            "[Guardrail] NOTE: GITHUB_TOKEN is not set. PR diffs can still be fetched for "
            "public repos (rate-limited), but ALLOW/BLOCK commit statuses cannot be posted.",
            flush=True,
        )
    yield
    await github_client.aclose()  # release the pooled GitHub connection on shutdown


app = FastAPI(title="PipelineAI GitHub Guardrail", version="2.0.0", lifespan=lifespan)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# OpenAI-compatible endpoint override (e.g. Gemini's OpenAI-compat API). When set,
# OPENAI_API_KEY carries that provider's key and OPENAI_MODEL names its model.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

try:
    AGENT_TIMEOUT_SECONDS = float(os.getenv("AGENT_TIMEOUT_SECONDS", "10"))
except ValueError:
    AGENT_TIMEOUT_SECONDS = 10.0

# Optional. Comma-separated list of browser origins allowed to call this API
# cross-origin (e.g. a static status page hosted elsewhere). Unset -> no CORS is
# granted (the webhook flow is server-to-server and never needs it). Never "*".
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

# Wired once at startup; reused across requests.
github_client = GitHubClient(token=GITHUB_TOKEN)
github_checks = GitHubChecksAPI(github_client)
impact_agent = ImpactAgent()
memory_agent = MemoryAgent()
simulation_agent = SimulationAgent()
remediation_ai = AIRemediation(api_key=OPENAI_API_KEY, model=OPENAI_MODEL, base_url=OPENAI_BASE_URL)
risk_agent = RiskAgent(impact_agent, memory_agent, simulation_agent, remediation_ai=remediation_ai)
webhook_handler = WebhookHandler(github_client, risk_agent, github_checks)


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not signature_header or not WEBHOOK_SECRET:
        return False
    hash_object = hmac.new(WEBHOOK_SECRET.encode("utf-8"), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    return hmac.compare_digest(expected_signature, signature_header)


async def process_guardrail(payload: dict) -> None:
    try:
        await asyncio.wait_for(webhook_handler.handle_pr_event(payload), timeout=AGENT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        pr = payload.get("pull_request", {})
        repo_data = payload.get("repository", {})
        try:
            await github_checks.publish_error(
                repo_data["owner"]["login"],
                repo_data["name"],
                pr["head"]["sha"],
                f"Guardrail analysis exceeded {AGENT_TIMEOUT_SECONDS}s timeout.",
            )
        except Exception as e:
            print(f"[Guardrail] Failed to publish timeout status: {e}")


@app.get("/")
async def root():
    return {
        "service": "PipelineAI GitHub Guardrail",
        "version": app.version,
        "endpoints": {"health": "GET /health", "webhook": "POST /webhook", "docs": "GET /docs"},
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "github_token_configured": bool(GITHUB_TOKEN),
        "webhook_secret_configured": bool(WEBHOOK_SECRET),
        "webhook_signature_enforced": bool(WEBHOOK_SECRET),
        "ai_remediation_configured": remediation_ai.configured,
    }


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(None),
):
    payload_body = await request.body()
    if WEBHOOK_SECRET and not verify_signature(payload_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from None

    if "pull_request" in payload and payload.get("action") in ["opened", "synchronize", "reopened"]:
        background_tasks.add_task(process_guardrail, payload)
        return {"status": "accepted", "message": "Webhook received. Agents deployed."}

    return {"status": "ignored", "message": "Not a relevant pull_request event"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("guardrail_main:app", host="0.0.0.0", port=port)
