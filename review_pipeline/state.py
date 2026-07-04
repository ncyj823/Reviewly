"""
Shared state definition for the Reviewly pipeline.

Every node in our LangGraph graph reads from and writes to this state.
Think of it as the "conversation memory" for one PR review run.

Why TypedDict?
LangGraph requires state to be a TypedDict (or dataclass). It uses the
field annotations to know what keys exist and how to merge updates from
parallel nodes. The Annotated[list, operator.add] pattern is the key
part for parallel execution — when Security, Quality, and Test agents
all write to `findings` simultaneously, LangGraph uses operator.add to
MERGE their lists instead of one overwriting the other.
"""

import operator
from typing import Annotated, Optional
from typing_extensions import TypedDict


class PRReviewState(TypedDict):
    # ── Input fields (set once by fetch_pr_data node) ──────────────────
    owner: str               # GitHub repo owner, e.g. "ncyj823"
    repo: str                # GitHub repo name, e.g. "Reviewly"
    pr_number: int           # PR number, e.g. 1

    pr_diff: str             # Raw unified diff text from github_get_pr_diff
    pr_files: list           # List of changed files from github_get_pr_files

    # ── Output fields (written by parallel agent nodes) ─────────────────
    # Annotated with operator.add so parallel writes MERGE, not overwrite.
    # Each agent appends its own FindingDict to this list.
    findings: Annotated[list, operator.add]

    # ── Final output (set by aggregator, consumed by post_review) ───────
    review_body: Optional[str]    # Final markdown comment to post to GitHub
    review_event: Optional[str]   # "COMMENT" or "REQUEST_CHANGES"
    posted_url: Optional[str]     # html_url of the posted review, for confirmation
