"""
webhook_service/main.py — FastAPI webhook receiver for GitHub PR events.

The single most important design decision here:
    GitHub requires a response within 10 seconds or it marks the
    delivery as FAILED and retries. LLM calls take 6-10s minimum.
    So we NEVER run the pipeline inside the webhook handler.
    Instead: receive → validate → queue → ack immediately.

GitHub will retry failed deliveries up to 3 times, which would cause
duplicate reviews. Redis deduplication prevents this.

Run locally:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Then expose via ngrok for GitHub webhook delivery:
    ngrok http 8000
"""

import hashlib
import hmac
import json
import os
import sys

import redis
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from rq import Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "github_mcp"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "github_mcp", ".env"))

from worker import run_review_job

app = FastAPI(title="Reviewly Webhook Service")

# Redis connection — used for both job queuing and deduplication
redis_conn = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=6379,
    decode_responses=True
)
review_queue = Queue("reviews", connection=redis_conn)

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def _verify_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub's HMAC-SHA256 webhook signature.

    GitHub signs every webhook delivery with your webhook secret.
    We verify this to ensure requests are genuinely from GitHub,
    not from someone who discovered our endpoint URL.

    If no secret is configured (dev mode), we skip verification.
    In production, always set GITHUB_WEBHOOK_SECRET.
    """
    if not WEBHOOK_SECRET:
        return True  # Dev mode — skip verification
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/health")
async def health():
    """Health check endpoint — useful for deployment monitoring."""
    try:
        redis_conn.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    """Receive GitHub webhook events for pull requests.

    This handler must return within GitHub's 10-second timeout.
    All heavy work (LLM calls, GitHub API calls) is queued to Redis.
    """
    payload_bytes = await request.body()

    # Step 1: Verify this is actually from GitHub
    if not _verify_signature(payload_bytes, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Step 2: Only handle pull_request events, ignore everything else
    if x_github_event != "pull_request":
        return {"status": "ignored", "event": x_github_event}

    payload = json.loads(payload_bytes)
    action = payload.get("action", "")

    # Step 3: Only trigger on PR opened or new commits pushed (synchronize)
    if action not in {"opened", "synchronize"}:
        return {"status": "ignored", "action": action}

    # Step 4: Extract PR details
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    owner = repo.get("owner", {}).get("login", "")
    repo_name = repo.get("name", "")
    pr_number = pr.get("number")

    if not all([owner, repo_name, pr_number]):
        raise HTTPException(status_code=400, detail="Missing required PR fields")

    # Step 5: Deduplicate — don't queue if already queued/running
    # Redis key expires after 10 minutes (covers max pipeline runtime)
    dedup_key = f"reviewly:queued:{owner}:{repo_name}:{pr_number}"
    if redis_conn.get(dedup_key):
        print(f"[webhook] Duplicate delivery ignored: {owner}/{repo_name}#{pr_number}")
        return {"status": "duplicate", "pr": pr_number}

    # Step 6: Mark as queued BEFORE enqueueing (prevents race condition)
    redis_conn.setex(dedup_key, 600, "queued")  # expires in 10 minutes

    # Step 7: Enqueue the review job — this returns immediately
    job = review_queue.enqueue(
        run_review_job,
        owner,
        repo_name,
        pr_number,
        job_timeout=300,  # 5 min max per job
    )

    print(f"[webhook] Queued review job {job.id}: {owner}/{repo_name}#{pr_number}")

    # Step 8: Ack to GitHub immediately — well within the 10s window
    return {
        "status": "queued",
        "job_id": job.id,
        "pr": pr_number,
        "repo": f"{owner}/{repo_name}",
    }


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    """Check the status of a queued review job."""
    from rq.job import Job
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        return {
            "job_id": job_id,
            "status": job.get_status(),
            "created_at": str(job.created_at),
            "ended_at": str(job.ended_at) if job.ended_at else None,
        }
    except Exception:
        raise HTTPException(status_code=404, detail="Job not found")
