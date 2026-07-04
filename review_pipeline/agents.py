"""
The three parallel reviewer agents for Reviewly.

Each agent is a LangGraph node — an async function that:
  1. Reads what it needs from PRReviewState
  2. Calls Groq (via langchain-groq) with a focused, role-specific prompt
  3. Parses the LLM's JSON response
  4. Returns {"findings": [its_finding]} to be MERGED into shared state

Why separate agents instead of one big prompt?
- Focus: a security-only prompt catches more subtle issues than a
  "check everything" prompt (LLMs get distracted by too many goals)
- Parallelism: all 3 run simultaneously, cutting total latency by ~2/3
- Debuggability: if the quality agent is wrong, you fix THAT prompt,
  not a giant monolith
- Resume story: "I designed a multi-agent system where each agent has
  a single responsibility" is a much stronger answer than "I used one LLM call"
"""

import json
import os
import re
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage

from state import PRReviewState

# ---------------------------------------------------------------------------
# Shared LLM client
# All agents share the same ChatGroq instance — no need to create three.
# Temperature=0 for deterministic, consistent review output.
# ---------------------------------------------------------------------------

def _get_llm() -> ChatGroq:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY not set. Add it to your .env file."
        )
    return ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0,
        groq_api_key=api_key,
    )


def _parse_json_response(raw: str, agent_name: str) -> dict:
    """Safely extract JSON from an LLM response.

    LLMs sometimes wrap JSON in markdown fences (```json ... ```) even
    when told not to. This handles both cases without crashing.
    """
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Fail gracefully — return a structured error finding instead of
        # crashing the whole pipeline. The aggregator handles this.
        return {
            "agent": agent_name,
            "severity": "error",
            "summary": f"Agent failed to produce valid JSON: {str(e)}",
            "issues": [],
        }


# ---------------------------------------------------------------------------
# Agent 1: Security Reviewer
# ---------------------------------------------------------------------------

SECURITY_SYSTEM_PROMPT = """You are a security-focused code reviewer.
Analyze the provided git diff for security vulnerabilities ONLY.

Look specifically for:
- Hardcoded secrets, API keys, passwords, tokens
- SQL injection vulnerabilities
- Command injection risks
- Insecure deserialization
- Missing input validation on user-controlled data
- Exposed sensitive data in logs or error messages
- Insecure use of cryptography

Respond ONLY with a JSON object in this exact schema (no markdown, no explanation):
{
  "agent": "security",
  "severity": "high" | "medium" | "low" | "none",
  "summary": "one sentence summary of security posture",
  "issues": [
    {
      "title": "short issue name",
      "description": "what the problem is and why it matters",
      "location": "filename:line or 'general' if not line-specific",
      "recommendation": "concrete fix suggestion"
    }
  ]
}

If no security issues found, return severity "none" and empty issues array.
"""


async def security_agent(state: PRReviewState) -> dict:
    """Security review node — runs in parallel with quality and test agents."""
    llm = _get_llm()
    diff = state["pr_diff"]

    if not diff or diff.startswith("Error:"):
        return {"findings": [{"agent": "security", "severity": "error",
                               "summary": "Could not fetch PR diff", "issues": []}]}

    messages = [
        SystemMessage(content=SECURITY_SYSTEM_PROMPT),
        HumanMessage(content=f"Review this diff:\n\n{diff[:8000]}"),  # cap at 8k chars
    ]

    response = await llm.ainvoke(messages)
    finding = _parse_json_response(response.content, "security")
    return {"findings": [finding]}


# ---------------------------------------------------------------------------
# Agent 2: Code Quality Reviewer
# ---------------------------------------------------------------------------

QUALITY_SYSTEM_PROMPT = """You are a code quality reviewer.
Analyze the provided git diff for code quality issues ONLY.

Look specifically for:
- Poor naming (variables, functions, classes that are unclear)
- Functions/methods that are too long or do too many things
- Code duplication or repeated logic that should be extracted
- Missing or inadequate error handling
- Dead code (unreachable code, unused variables/imports)
- Overly complex logic that could be simplified
- Missing or misleading comments on non-obvious code

Do NOT comment on security issues or test coverage — those are handled separately.

Respond ONLY with a JSON object in this exact schema (no markdown, no explanation):
{
  "agent": "quality",
  "severity": "high" | "medium" | "low" | "none",
  "summary": "one sentence summary of code quality",
  "issues": [
    {
      "title": "short issue name",
      "description": "what the problem is and why it matters",
      "location": "filename:line or 'general'",
      "recommendation": "concrete improvement suggestion"
    }
  ]
}

If no quality issues found, return severity "none" and empty issues array.
"""


async def quality_agent(state: PRReviewState) -> dict:
    """Code quality review node — runs in parallel with security and test agents."""
    llm = _get_llm()
    diff = state["pr_diff"]

    if not diff or diff.startswith("Error:"):
        return {"findings": [{"agent": "quality", "severity": "error",
                               "summary": "Could not fetch PR diff", "issues": []}]}

    messages = [
        SystemMessage(content=QUALITY_SYSTEM_PROMPT),
        HumanMessage(content=f"Review this diff:\n\n{diff[:8000]}"),
    ]

    response = await llm.ainvoke(messages)
    finding = _parse_json_response(response.content, "quality")
    return {"findings": [finding]}


# ---------------------------------------------------------------------------
# Agent 3: Test Coverage Reviewer
# ---------------------------------------------------------------------------

TEST_SYSTEM_PROMPT = """You are a test coverage reviewer.
Analyze the provided git diff to assess whether adequate tests accompany the code changes.

Look specifically for:
- New functions or classes added without corresponding tests
- Changed logic that invalidates existing tests but no test updates
- Missing edge case coverage (null inputs, empty lists, boundary values)
- Test files changed without corresponding source changes (tests without code — possible dead test)
- Hardcoded test data that should be parameterized

Do NOT comment on security or general code quality — focus only on testing.

Respond ONLY with a JSON object in this exact schema (no markdown, no explanation):
{
  "agent": "tests",
  "severity": "high" | "medium" | "low" | "none",
  "summary": "one sentence summary of test coverage",
  "issues": [
    {
      "title": "short issue name",
      "description": "what test coverage is missing and why it matters",
      "location": "filename or 'general'",
      "recommendation": "what tests should be added or changed"
    }
  ]
}

If test coverage looks adequate, return severity "none" and empty issues array.
"""


async def test_coverage_agent(state: PRReviewState) -> dict:
    """Test coverage review node — runs in parallel with security and quality agents."""
    llm = _get_llm()
    diff = state["pr_diff"]
    files = state.get("pr_files", [])

    if not diff or diff.startswith("Error:"):
        return {"findings": [{"agent": "tests", "severity": "error",
                               "summary": "Could not fetch PR diff", "issues": []}]}

    # Give the test agent extra context: which files changed, not just the diff
    file_summary = "\n".join(
        f"- {f['filename']} ({f['status']}, +{f['additions']}/-{f['deletions']})"
        for f in files
    )

    messages = [
        SystemMessage(content=TEST_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Changed files:\n{file_summary}\n\nFull diff:\n\n{diff[:8000]}"
        ),
    ]

    response = await llm.ainvoke(messages)
    finding = _parse_json_response(response.content, "tests")
    return {"findings": [finding]}
