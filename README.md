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
   Simulates chaos engineering by injecting malformed, adversarial payloads into a sandbox to guarantee the code will fail gracefully in production.
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
Create a `.env` file in the `backend/` directory:
```env
GITHUB_TOKEN=your_personal_access_token
WEBHOOK_SECRET=your_webhook_secret (optional)
```

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
