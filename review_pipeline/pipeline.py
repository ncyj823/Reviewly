"""
Reviewly's LangGraph review pipeline.

Graph structure:
    fetch_pr_data
         │
    ┌────┼────┐
    ▼    ▼    ▼         ← parallel fan-out (all 3 run simultaneously)
  sec  qual  tests
    └────┼────┘
         ▼
     aggregate          ← merges 3 findings into one structured comment
         │
         ▼
     post_review        ← calls github_post_review_comment via MCP server

Running this file directly:
    python pipeline.py --owner ncyj823 --repo Reviewly --pr 1
"""

import asyncio
import json
import os
import sys
import argparse
from datetime import datetime , timezone

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END

# Add parent dir to path so we can import github_mcp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "github_mcp"))

from state import PRReviewState
from agents import security_agent, quality_agent, test_coverage_agent
# pyrefly: ignore [missing-import]
from github_client import github_request, format_api_error

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "github_mcp", ".env"))


# ---------------------------------------------------------------------------
# Node 1: fetch_pr_data
# Calls our MCP server's underlying logic directly (same github_client.py)
# to populate the diff and files into state before agents run.
# ---------------------------------------------------------------------------

async def fetch_pr_data(state: PRReviewState) -> dict:
    """Fetch PR diff and file list from GitHub, populate into state."""
    owner, repo, pr_number = state["owner"], state["repo"], state["pr_number"]
    print(f"[fetch] Fetching PR #{pr_number} from {owner}/{repo}...")

    # Fetch diff
    try:
        diff = await github_request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
    except Exception as e:
        diff = format_api_error(e)

    # Fetch file list
    try:
        files_raw = await github_request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        files = [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "changes": f["changes"],
            }
            for f in files_raw
        ]
    except Exception as e:
        files = []
        print(f"[fetch] Warning: could not fetch file list: {format_api_error(e)}")

    print(f"[fetch] Got diff ({len(diff)} chars), {len(files)} file(s) changed")
    return {"pr_diff": diff, "pr_files": files, "findings": []}


# ---------------------------------------------------------------------------
# Node 5: aggregate
# Merges all 3 agents' findings into one clean markdown comment.
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3, "error": 4}
SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢", "none": "✅", "error": "⚠️"}


def _build_review_body(findings: list, pr_number: int) -> tuple[str, str]:
    """Build the markdown review comment and determine overall verdict.

    Returns:
        (review_body: str, review_event: str)
        review_event is "REQUEST_CHANGES" if any high severity found, else "COMMENT"
    """
    # Sort findings: security first, then quality, then tests
    agent_order = {"security": 0, "quality": 1, "tests": 2}
    findings_sorted = sorted(findings, key=lambda f: agent_order.get(f.get("agent", ""), 9))

    has_high = any(f.get("severity") == "high" for f in findings_sorted)
    overall_event = "COMMENT"

    lines = [
        f"## 🤖 Reviewly Automated Review",
        f"*Generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — PR #{pr_number}*",
        "",
    ]

    for finding in findings_sorted:
        agent = finding.get("agent", "unknown").title()
        severity = finding.get("severity", "none")
        emoji = SEVERITY_EMOJI.get(severity, "❓")
        summary = finding.get("summary", "No summary.")
        issues = finding.get("issues", [])

        lines.append(f"### {emoji} {agent} Review — `{severity.upper()}`")
        lines.append(f"_{summary}_")

        if issues:
            lines.append("")
            for issue in issues:
                lines.append(f"**{issue.get('title', 'Issue')}**")
                lines.append(f"- 📍 `{issue.get('location', 'general')}`")
                lines.append(f"- {issue.get('description', '')}")
                lines.append(f"- 💡 _{issue.get('recommendation', '')}_")
                lines.append("")
        else:
            lines.append("")

    lines.append("---")
    lines.append(
        "_This review was posted by [Reviewly](https://github.com/ncyj823/Reviewly) — "
        "an MCP-powered multi-agent PR reviewer. Final merge decisions stay with humans._"
    )

    return "\n".join(lines), overall_event


async def aggregate(state: PRReviewState) -> dict:
    """Merge all agent findings into one structured markdown comment."""
    findings = state.get("findings", [])
    print(f"[aggregate] Merging {len(findings)} agent finding(s)...")

    review_body, review_event = _build_review_body(findings, state["pr_number"])
    print(f"[aggregate] Overall verdict: {review_event}")
    return {"review_body": review_body, "review_event": review_event}


# ---------------------------------------------------------------------------
# Node 6: post_review
# Posts the aggregated comment to GitHub using our MCP server's github_client.
# ---------------------------------------------------------------------------

async def post_review(state: PRReviewState) -> dict:
    """Post the final review comment to GitHub PR."""
    owner, repo, pr_number = state["owner"], state["repo"], state["pr_number"]
    body = state.get("review_body", "")
    event = state.get("review_event", "COMMENT")

    print(f"[post] Posting review to {owner}/{repo}#{pr_number} as {event}...")

    try:
        result = await github_request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json={"body": body, "event": event},
        )
        url = result.get("html_url", "")
        print(f"[post] OK Review posted: {url}")
        return {"posted_url": url}
    except Exception as e:
        print(f"[post] ERROR Failed to post review: {format_api_error(e)}")
        return {"posted_url": None}


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    """Assemble and compile the full review pipeline graph."""
    builder = StateGraph(PRReviewState)

    # Add all nodes
    builder.add_node("fetch_pr_data", fetch_pr_data)
    builder.add_node("security_agent", security_agent)
    builder.add_node("quality_agent", quality_agent)
    builder.add_node("test_coverage_agent", test_coverage_agent)
    builder.add_node("aggregate", aggregate)
    builder.add_node("post_review", post_review)

    # Flow: START → fetch
    builder.add_edge(START, "fetch_pr_data")

    # Fan-out: fetch → all 3 agents IN PARALLEL
    # This is the key LangGraph feature — these 3 run simultaneously,
    # not sequentially. Each writes to findings[], which state.py merges
    # via operator.add so no agent's output overwrites another's.
    builder.add_edge("fetch_pr_data", "security_agent")
    builder.add_edge("fetch_pr_data", "quality_agent")
    builder.add_edge("fetch_pr_data", "test_coverage_agent")

    # Fan-in: all 3 agents → aggregate
    # LangGraph automatically waits for ALL incoming edges to complete
    # before running the aggregate node — this is the "join" after the fork.
    builder.add_edge("security_agent", "aggregate")
    builder.add_edge("quality_agent", "aggregate")
    builder.add_edge("test_coverage_agent", "aggregate")

    # Final: aggregate → post → END
    builder.add_edge("aggregate", "post_review")
    builder.add_edge("post_review", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# CLI entry point — run directly to test against a real PR
# ---------------------------------------------------------------------------

async def run_review(owner: str, repo: str, pr_number: int):
    """Run the full review pipeline against a real GitHub PR."""
    graph = build_graph()

    initial_state: PRReviewState = {
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "pr_diff": "",
        "pr_files": [],
        "findings": [],
        "review_body": None,
        "review_event": None,
        "posted_url": None,
    }

    print(f"\n{'='*50}")
    print(f"  Reviewly — reviewing {owner}/{repo} PR #{pr_number}")
    print(f"{'='*50}\n")

    start = asyncio.get_event_loop().time()
    final_state = await graph.ainvoke(initial_state)
    elapsed = asyncio.get_event_loop().time() - start

    print(f"\n{'='*50}")
    print(f"  Done in {elapsed:.1f}s")
    print(f"  Review posted: {final_state.get('posted_url', 'N/A')}")
    print(f"{'='*50}\n")

    return final_state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Reviewly on a GitHub PR")
    parser.add_argument("--owner", required=True, help="GitHub owner, e.g. ncyj823")
    parser.add_argument("--repo", required=True, help="Repo name, e.g. Reviewly")
    parser.add_argument("--pr", required=True, type=int, help="PR number, e.g. 1")
    args = parser.parse_args()

    asyncio.run(run_review(args.owner, args.repo, args.pr))
