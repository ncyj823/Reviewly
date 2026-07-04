"""
Shared GitHub API client used by all MCP tools.

Why this file exists separately:
Every tool needs to (a) authenticate, (b) make an HTTP call, (c) handle errors
the same way. Instead of repeating that in every tool function (which the MCP
best-practices guide explicitly warns against), we centralize it here.
"""

import os
import httpx

GITHUB_API_BASE = "https://api.github.com"


def _get_token() -> str:
    """Read the GitHub token from environment variables.

    We never hardcode tokens in source code. The token is loaded at runtime
    from a .env file (via python-dotenv in server.py) so it never gets
    committed to git.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN not set. Add it to your .env file: "
            "GITHUB_TOKEN=ghp_xxxxxxxxxxxx"
        )
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def github_request(method: str, path: str, **kwargs) -> dict | list | str:
    """Make an authenticated request to the GitHub API.

    Args:
        method: HTTP method, e.g. "GET", "POST"
        path: API path starting with "/", e.g. "/repos/owner/repo/pulls/1"
        **kwargs: passed through to httpx (params, json, headers override, etc.)

    Returns:
        Parsed JSON response (dict or list), or raw text for non-JSON
        responses (like diffs, which GitHub returns as plain text).

    Raises:
        httpx.HTTPStatusError: on 4xx/5xx responses, with a clear message
        attached for the caller to handle.
    """
    url = f"{GITHUB_API_BASE}{path}"
    headers = _headers()
    headers.update(kwargs.pop("headers", {}))

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(method, url, headers=headers, **kwargs)

    if response.status_code >= 400:
        # Raise so each tool's error handler can format a clear message
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()
    return response.text


def format_api_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools.

    This is the single place that turns a raw exception into something
    an LLM agent can actually act on (retry, ask for different input, etc.)
    """
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401:
            return (
                "Error: GitHub authentication failed (401). "
                "Check that GITHUB_TOKEN in your .env file is valid and not expired."
            )
        if status == 403:
            return (
                "Error: Permission denied (403). Your token may lack the "
                "'repo' scope, or you've hit GitHub's rate limit. "
                "Check response headers for X-RateLimit-Remaining."
            )
        if status == 404:
            return (
                "Error: Not found (404). Double-check the owner/repo name "
                "and that the PR/file/branch actually exists."
            )
        if status == 422:
            return (
                "Error: Unprocessable request (422). The request was "
                "understood but invalid - check parameter values (e.g. "
                "line numbers must exist in the diff for review comments)."
            )
        return f"Error: GitHub API returned status {status}: {e.response.text[:200]}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to GitHub timed out. Please try again."
    return f"Error: Unexpected error ({type(e).__name__}): {str(e)}"
