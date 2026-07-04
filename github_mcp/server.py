"""
github_mcp - A custom MCP server exposing GitHub PR-review capabilities.

This server is consumed by our LangGraph review pipeline (built separately).
It deliberately exposes only 4 focused tools rather than wrapping GitHub's
entire API - see the quality checklist in mcp-builder skill for why.

Run locally for testing with:
    python server.py

Or inspect interactively with:
    npx @modelcontextprotocol/inspector python server.py
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict
from dotenv import load_dotenv

from github_client import github_request, format_api_error

load_dotenv()  # reads .env into os.environ - this is where GITHUB_TOKEN comes from

# Server name follows the {service}_mcp convention from the MCP best-practices guide
mcp = FastMCP("github_mcp")


# ---------------------------------------------------------------------------
# Tool 1: github_get_pr_files
# ---------------------------------------------------------------------------

class GetPRFilesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    owner: str = Field(..., description="Repository owner/org, e.g. 'ncyj823'", min_length=1)
    repo: str = Field(..., description="Repository name, e.g. 'pr-review-test'", min_length=1)
    pr_number: int = Field(..., description="Pull request number", ge=1)


@mcp.tool(
    name="github_get_pr_files",
    annotations={
        "title": "List files changed in a PR",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def github_get_pr_files(params: GetPRFilesInput) -> str:
    """List the files changed in a pull request, with change stats.

    Call this FIRST before fetching diffs or file content - it tells the
    agent the scope of the PR (how many files, which ones, how much churn)
    so it can decide what to look at in depth.

    Args:
        params (GetPRFilesInput): owner, repo, pr_number

    Returns:
        str: JSON list of changed files, each with:
            - filename (str)
            - status (str): "added" | "modified" | "removed" | "renamed"
            - additions (int), deletions (int), changes (int)
    """
    try:
        path = f"/repos/{params.owner}/{params.repo}/pulls/{params.pr_number}/files"
        files = await github_request("GET", path, params={"per_page": 100})
        summary = [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "changes": f["changes"],
            }
            for f in files
        ]
        return json.dumps({"pr_number": params.pr_number, "files": summary}, indent=2)
    except Exception as e:
        return format_api_error(e)


# ---------------------------------------------------------------------------
# Tool 2: github_get_pr_diff
# ---------------------------------------------------------------------------

class GetPRDiffInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    owner: str = Field(..., description="Repository owner/org", min_length=1)
    repo: str = Field(..., description="Repository name", min_length=1)
    pr_number: int = Field(..., description="Pull request number", ge=1)


@mcp.tool(
    name="github_get_pr_diff",
    annotations={
        "title": "Get the full unified diff for a PR",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def github_get_pr_diff(params: GetPRDiffInput) -> str:
    """Fetch the full unified diff for a pull request.

    This is the primary input for code review - it shows exactly what
    lines were added/removed. For large PRs, prefer calling
    github_get_pr_files first to scope down which files matter before
    relying on this full diff.

    Args:
        params (GetPRDiffInput): owner, repo, pr_number

    Returns:
        str: The raw unified diff text (standard git diff format), or
            an error message string starting with "Error:" on failure.
    """
    try:
        path = f"/repos/{params.owner}/{params.repo}/pulls/{params.pr_number}"
        # GitHub returns the diff as plain text when we ask for this media type
        diff_text = await github_request(
            "GET", path, headers={"Accept": "application/vnd.github.v3.diff"}
        )
        if not diff_text or not isinstance(diff_text, str):
            return "Error: Received an unexpected (non-text) response for the diff."
        return diff_text
    except Exception as e:
        return format_api_error(e)


# ---------------------------------------------------------------------------
# Tool 3: github_get_file_content
# ---------------------------------------------------------------------------

class GetFileContentInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    owner: str = Field(..., description="Repository owner/org", min_length=1)
    repo: str = Field(..., description="Repository name", min_length=1)
    path: str = Field(..., description="File path within the repo, e.g. 'src/app.py'", min_length=1)
    ref: Optional[str] = Field(
        default=None,
        description="Branch, tag, or commit SHA to read from. Defaults to the repo's default branch if omitted.",
    )


@mcp.tool(
    name="github_get_file_content",
    annotations={
        "title": "Get full content of a file at a given ref",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def github_get_file_content(params: GetFileContentInput) -> str:
    """Fetch the full content of a single file at a specific branch/commit.

    Use this when the diff alone isn't enough context - e.g. to check
    whether a function being modified is used elsewhere in the same file,
    or to see the full surrounding code around a changed line.

    Args:
        params (GetFileContentInput): owner, repo, path, optional ref

    Returns:
        str: The decoded file content as plain text, or an error message
            string starting with "Error:" on failure (e.g. file too large,
            file is binary, or file not found at that ref).
    """
    try:
        import base64

        api_path = f"/repos/{params.owner}/{params.repo}/contents/{params.path}"
        query_params = {"ref": params.ref} if params.ref else {}
        result = await github_request("GET", api_path, params=query_params)

        if isinstance(result, list):
            return f"Error: '{params.path}' is a directory, not a file."

        if result.get("encoding") != "base64":
            return f"Error: Unexpected encoding '{result.get('encoding')}' - file may be too large or binary."

        content = base64.b64decode(result["content"]).decode("utf-8", errors="replace")
        return content
    except Exception as e:
        return format_api_error(e)


# ---------------------------------------------------------------------------
# Tool 4: github_post_review_comment
# ---------------------------------------------------------------------------

class PostReviewCommentInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    owner: str = Field(..., description="Repository owner/org", min_length=1)
    repo: str = Field(..., description="Repository name", min_length=1)
    pr_number: int = Field(..., description="Pull request number", ge=1)
    body: str = Field(
        ..., description="The review comment text (markdown supported)", min_length=1, max_length=10000
    )
    event: str = Field(
        default="COMMENT",
        description="Review verdict: 'COMMENT' (neutral, default), 'REQUEST_CHANGES', or 'APPROVE'. "
        "Agents should default to COMMENT or REQUEST_CHANGES - never auto-APPROVE without human oversight.",
    )


@mcp.tool(
    name="github_post_review_comment",
    annotations={
        "title": "Post a review comment on a PR",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def github_post_review_comment(params: PostReviewCommentInput) -> str:
    """Post a top-level review comment (and verdict) on a pull request.

    SAFETY NOTE: This tool can submit 'event': 'APPROVE'. In our pipeline,
    the agent is instructed to NEVER use APPROVE - only COMMENT or
    REQUEST_CHANGES. Final merge approval should always stay with a human.

    Args:
        params (PostReviewCommentInput): owner, repo, pr_number, body, event

    Returns:
        str: JSON confirmation with the created review's id and html_url,
            or an error message string starting with "Error:" on failure.
    """
    try:
        if params.event not in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
            return "Error: event must be one of 'COMMENT', 'REQUEST_CHANGES', 'APPROVE'."

        path = f"/repos/{params.owner}/{params.repo}/pulls/{params.pr_number}/reviews"
        result = await github_request(
            "POST", path, json={"body": params.body, "event": params.event}
        )
        return json.dumps(
            {
                "success": True,
                "review_id": result.get("id"),
                "html_url": result.get("html_url"),
                "state": result.get("state"),
            },
            indent=2,
        )
    except Exception as e:
        return format_api_error(e)


if __name__ == "__main__":
    # stdio transport - this server is meant to be launched as a subprocess
    # by an MCP client (e.g. our LangGraph pipeline), same pattern as the
    # `npx @playwright/mcp@latest` server we studied in Week 1.
    mcp.run()
