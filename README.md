# 🛡️ PipelineAI: Multi-Agent CI/CD Guardrail

**A predictive, autonomous gatekeeper for software engineering pipelines.**

PipelineAI intercepts GitHub Pull Requests in real-time, utilizing a deterministic multi-agent engine to analyze code changes for semantic risk, historical regressions, and adversarial failure before they ever reach the CI/CD runner.

---

## 📖 About the Project

In modern software engineering, traditional CI/CD pipelines are reactive and slow. Developers push code and wait upwards of 30 minutes for integration tests to finish, only to discover a catastrophic failure. Worse, logic regressions and security vulnerabilities often slip past human code reviewers, causing massive production outages.

**PipelineAI solves this by shifting risk assessment entirely to the left.**

Acting as an autonomous security gatekeeper, PipelineAI intercepts Pull Requests the exact millisecond they are opened. It mathematically guarantees that dangerous code is physically blocked from being merged, saving organizations thousands of hours in compute time and preventing devastating regressions.

---

## 🧠 The Multi-Agent Engine

PipelineAI is powered by a high-performance, asynchronous FastAPI backend that delegates tasks to three core deterministic agents:

1. **🗺️ The Impact Agent (Semantic Blast Radius):** 
   Scans the Abstract Syntax Tree (AST) and modified file paths to detect critical infrastructure changes (e.g., `auth`, `parser`, `config`).
2. **📚 The Memory Agent (Historical Lexicon):** 
   Acts as the institutional memory of the engineering team, cross-referencing incoming code against historical outage patterns to prevent known regressions.
3. **🔬 The Simulation Agent (Adversarial Chaos):** 
   Deterministically scans the diff for code shapes known to fail under malformed/adversarial input (bare `except:`, `eval`/`exec`, shell/SQL injection, unchecked payload indexing). No PR code is executed — see [backend/README.md](backend/README.md#what-adversarial-testing-means-here) for why.
4. **⚖️ The Risk Synthesis Agent (The Judge):** 
   Aggregates the heuristics from the other three agents to issue a final, deterministic verdict (`ALLOW` or `BLOCK`).

---

## 🏗️ Technical Architecture

PipelineAI uses a hybrid cloud-local deployment strategy for absolute security and speed:

*   **Trigger:** GitHub Webhooks (Payload triggered on `pull_request` events).
*   **Security:** Cryptographic Webhook Validation via `X-Hub-Signature-256` HMAC SHA-256.
*   **Tunneling:** `Smee.io` reverse proxy allows the local backend to securely receive cloud webhooks through corporate firewalls.
*   **Backend:** Stateless, event-driven `FastAPI` server utilizing asynchronous Background Tasks to guarantee instant `200 OK` responses to GitHub.
*   **Action:** GitHub Commit Status REST API physically injects UI feedback (`✅ ALLOW` or `❌ BLOCK`) and locks the target repository's merge button.

---

## 🚀 Quick Start (Demo Mode)

To run the PipelineAI Guardrail locally:

**1. Configure Environment**
Create a `.env` file in the `backend/` directory (see `backend/.env.example`):
```env
GITHUB_TOKEN=your_personal_access_token
WEBHOOK_SECRET=your_webhook_secret
OPENAI_API_KEY=your_openai_key   # optional — enables AI-generated remediation suggestions
```
Without `WEBHOOK_SECRET`, incoming webhooks are accepted unverified — fine for local testing, not for anything internet-reachable. Without `OPENAI_API_KEY`, BLOCK verdicts still fire correctly; suggestions fall back to the static analyzers' own recommendation text instead of an LLM-authored one.

**2. Start the Backend Server**
```bash
cd backend
python guardrail_main.py
```

**3. Start the Secure Tunnel**
In a new terminal, open your Smee.io tunnel:
```bash
npx smee-client --url https://smee.io/YOUR_WEBHOOK_URL --target http://127.0.0.1:8000/webhook
```

Open a Pull Request on your connected GitHub repository and watch the terminal agents execute in real-time!

> **No frontend.** PipelineAI is a headless webhook service (`GET /`, `GET /health`,
> `POST /webhook`). There is no dashboard, no login, no user accounts — the only
> credential is the server-side `GITHUB_TOKEN` for the GitHub REST API.

---

## ☁️ Deployment (Render)

The backend ships a Docker image that binds to `$PORT` and runs as non-root.

1. **Render** → New → **Blueprint** → select this repo (uses [`render.yaml`](render.yaml)).
   Or: New → Web Service → Runtime **Docker**, Root Directory **`backend`**,
   Health Check Path **`/health`**.
2. Set env vars in the service's **Environment** tab:
   `WEBHOOK_SECRET` (required), `GITHUB_TOKEN` (required to post statuses),
   and optionally `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`.
   **Do not set `PORT`** — Render injects it.
3. In your GitHub repo → Settings → Webhooks → add
   `https://<service>.onrender.com/webhook`, content type `application/json`,
   secret = your `WEBHOOK_SECRET`, events = **Pull requests**.

Full env-var table and behavior-if-missing: [backend/README.md](backend/README.md#environment-variables).

---

## ✅ Testing

```bash
cd backend
pip install -r requirements-dev.txt
pytest -v
```

37 tests cover signature verification, the AST-based impact analysis, the
deterministic adversarial pattern scanner, the persistent regression memory
(including a simulated-restart persistence check), the risk-aggregation logic,
and the OpenAI remediation client's error handling (missing key, timeout,
malformed response). GitHub and OpenAI are mocked at the network boundary;
everything else runs against real code paths with real assertions.

See [backend/README.md](backend/README.md) for the full architecture, setup,
and known limitations.
