"""
worker.py — the actual job that runs in the background via RQ.

Why a separate file?
RQ (Redis Queue) workers import this file in a separate process.
It must be importable on its own without starting FastAPI.

Flow:
    GitHub webhook → FastAPI (acks in <1s) → Redis queue → THIS FILE runs
"""

import sys
import os
import asyncio

# Make github_mcp importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "github_mcp"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "review_pipeline"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "github_mcp", ".env"))


def run_review_job(owner: str, repo: str, pr_number: int):
    """Entry point called by RQ worker for each PR review job.

    RQ calls regular (sync) functions, so we wrap our async pipeline
    with asyncio.run() here.
    """
    print(f"[worker] Starting review job: {owner}/{repo}#{pr_number}")
    try:
        # pyrefly: ignore [missing-import]
        from pipeline import run_review
        asyncio.run(run_review(owner, repo, pr_number))
        print(f"[worker] [OK] Review complete: {owner}/{repo}#{pr_number}")
    except Exception as e:
        print(f"[worker] [Error] Review failed: {e}")
        raise  # Re-raise so RQ marks job as failed (visible in Redis)
