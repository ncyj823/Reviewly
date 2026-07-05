<div align="center">

# 🔍 Reviewly

### MCP-powered multi-agent PR reviewer

*Catches security vulnerabilities, code quality issues, and missing test coverage automatically — the moment a PR is opened.*

![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python)
![LangGraph](https://img.shields.io/badge/LangGraph-multi--agent-green?style=flat-square)
![FastMCP](https://img.shields.io/badge/MCP-custom%20server-orange?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED?style=flat-square&logo=docker)
![Groq](https://img.shields.io/badge/Groq-llama--3.3--70b-red?style=flat-square)

</div>

---

## What it does

When a PR is opened on your repo, Reviewly automatically:

1. **Fetches** the diff and changed files via a custom GitHub MCP server
2. **Runs 3 specialized agents in parallel** — Security, Code Quality, Test Coverage
3. **Posts a structured review** back to the PR within ~6 seconds

No manual triggering. No waiting. No "check everything" prompt that catches nothing.

```
┌─ PR opened on GitHub ──────────────────────────────────────────┐
│                                                                  │
│  GitHub Webhook → FastAPI (ack < 1s) → Redis Queue              │
│                                              │                   │
│                                    ┌─────────▼──────────┐       │
│                                    │   github_mcp server │       │
│                                    │  (custom MCP tools) │       │
│                                    └─────────┬──────────┘       │
│                                              │                   │
│                          ┌───────────────────┼────────────────┐  │
│                          ▼                   ▼                ▼  │
│                    [Security]           [Quality]          [Tests]│
│                      Agent               Agent              Agent │
│                          └───────────────────┼────────────────┘  │
│                                              ▼                   │
│                                    Aggregator → PR Comment        │
└──────────────────────────────────────────────────────────────────┘
```

## Why this is different

Most "AI code review" projects do one LLM call on a diff. That breaks in production:

| Problem | How Reviewly handles it |
|---|---|
| GitHub's 10s webhook timeout | FastAPI acks immediately, queues work to Redis |
| Duplicate reviews from GitHub retries | Redis deduplication key per PR |
| Slow sequential LLM calls | 3 agents run in **parallel** — same latency as 1 |
| Shallow "check everything" prompts | Each agent has a single, focused responsibility |

## Stack

| Layer | Tech |
|---|---|
| MCP Server | Python `mcp` SDK (FastMCP), custom GitHub tools |
| Agent Orchestration | LangGraph (parallel fan-out + fan-in) |
| LLM | Groq `llama-3.3-70b-versatile` |
| Webhook Service | FastAPI + uvicorn |
| Job Queue | Redis + RQ (SimpleWorker) |
| Deployment | Docker Compose |

## Quick start

```bash
git clone https://github.com/ncyj823/Reviewly.git
cd Reviewly

# Add your keys
cp github_mcp/.env.example github_mcp/.env
# Edit .env: GITHUB_TOKEN, GROQ_API_KEY, GITHUB_WEBHOOK_SECRET

# Start everything
docker compose up --build
```

Then expose port 8000 via ngrok and register the webhook on GitHub (Settings → Webhooks → Payload URL: `https://your-ngrok-url/webhook/github`, event: Pull requests).

Open a PR — Reviewly posts a review automatically.

## Project structure

```
Reviewly/
├── github_mcp/           # Custom MCP server — 4 GitHub tools
│   ├── server.py         # Tool definitions (FastMCP)
│   ├── github_client.py  # Shared auth + error handling
│   └── .env.example
├── review_pipeline/      # LangGraph multi-agent pipeline
│   ├── state.py          # Shared PRReviewState (TypedDict)
│   ├── agents.py         # Security / Quality / TestCoverage agents
│   └── pipeline.py       # Graph definition + CLI entry point
├── webhook_service/      # FastAPI webhook receiver
│   ├── main.py           # Webhook handler + deduplication
│   └── worker.py         # RQ job entry point
├── start_worker.py       # Docker worker startup script
├── Dockerfile
└── docker-compose.yml
```

## MCP tools

The custom MCP server (`github_mcp/server.py`) exposes 4 tools:

| Tool | Description |
|---|---|
| `github_get_pr_files` | List changed files with stats |
| `github_get_pr_diff` | Fetch unified diff |
| `github_get_file_content` | Read full file at any ref |
| `github_post_review_comment` | Post review back to PR |

## Run pipeline manually

```bash
cd review_pipeline
python pipeline.py --owner ncyj823 --repo Reviewly --pr 1
```

## Environment variables

```env
GITHUB_TOKEN=ghp_...          # Personal Access Token (repo scope)
GROQ_API_KEY=gsk_...          # Groq API key (free tier works)
GITHUB_WEBHOOK_SECRET=...     # Webhook secret (any string)
```

## Safety

Reviewly never auto-approves PRs. Agents post `COMMENT` reviews only — merge decisions stay with humans, by design.

---

<div align="center">
Built by <a href="https://github.com/ncyj823">Nancy Jha</a> · KIIT University
</div>
