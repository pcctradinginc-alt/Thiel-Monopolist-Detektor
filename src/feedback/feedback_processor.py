"""
Human feedback processor.

Reads GitHub Issue labels (confirmed/rejected/watchlist/too-early)
and writes them into the human_feedback table.
This enables the system to learn over time which patterns lead to real moats.
"""

import logging
import os
import json
from datetime import datetime, timezone
import requests

logger = logging.getLogger(__name__)

LABEL_TO_VERDICT = {
    "confirmed": "CONFIRMED",
    "rejected": "REJECTED",
    "watchlist": "WATCHLIST",
    "too-early": "TOO_EARLY",
}


def fetch_labeled_issues(repo: str, token: str) -> list[dict]:
    """Fetch all issues with feedback labels from GitHub."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    all_issues = []

    for label in LABEL_TO_VERDICT:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{repo}/issues",
                headers=headers,
                params={"labels": label, "state": "all", "per_page": 100},
                timeout=30
            )
            resp.raise_for_status()
            all_issues.extend(resp.json())
        except Exception as e:
            logger.error(f"Failed to fetch issues with label '{label}': {e}")

    return all_issues


def extract_ticker_from_issue(issue: dict) -> str:
    """Extract ticker from issue title like '[HIDDEN_WEDGE_DETECTED] AAPL — Score: 78/100'"""
    title = issue.get("title", "")
    # Pattern: ] TICKER —
    import re
    match = re.search(r'\]\s+([A-Z]+)\s+—', title)
    if match:
        return match.group(1)
    return ""


def process_feedback(conn, config: dict) -> int:
    """
    Main entry: fetch GitHub Issues with feedback labels and store in DB.
    Returns number of feedback items processed.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = config.get("alerts", {}).get("github_repo", "")

    if not token or not repo:
        logger.warning("GitHub token or repo not configured — skipping feedback processing")
        return 0

    issues = fetch_labeled_issues(repo, token)
    processed = 0
    now = datetime.now(timezone.utc).isoformat()

    for issue in issues:
        issue_number = issue.get("number")
        ticker = extract_ticker_from_issue(issue)
        if not ticker:
            continue

        # Determine verdict from labels
        labels = [l["name"] for l in issue.get("labels", [])]
        verdict = None
        for label in labels:
            if label in LABEL_TO_VERDICT:
                verdict = LABEL_TO_VERDICT[label]
                break

        if not verdict:
            continue

        # Check if already processed
        existing = conn.execute(
            "SELECT id FROM human_feedback WHERE github_issue_number = ?",
            (issue_number,)
        ).fetchone()

        if existing:
            continue

        # Find latest evaluation for this ticker
        evaluation = conn.execute(
            "SELECT id FROM evaluations WHERE ticker = ? ORDER BY evaluated_at DESC LIMIT 1",
            (ticker,)
        ).fetchone()

        evaluation_id = evaluation["id"] if evaluation else None

        # Insert feedback
        conn.execute("""
            INSERT INTO human_feedback
            (ticker, evaluation_id, feedback_date, verdict, github_issue_number)
            VALUES (?, ?, ?, ?, ?)
        """, (ticker, evaluation_id, now, verdict, issue_number))

        logger.info(f"Stored feedback for {ticker}: {verdict} (Issue #{issue_number})")
        processed += 1

    conn.commit()

    # Log feedback summary for calibration awareness
    if processed > 0:
        _log_calibration_summary(conn)

    return processed


def _log_calibration_summary(conn):
    """Log feedback statistics to help identify systematic LLM biases."""
    rows = conn.execute("""
        SELECT verdict, COUNT(*) as count
        FROM human_feedback
        GROUP BY verdict
    """).fetchall()

    summary = {row["verdict"]: row["count"] for row in rows}
    total = sum(summary.values())

    if total >= 10:
        confirmed = summary.get("CONFIRMED", 0)
        rejected = summary.get("REJECTED", 0)
        precision = confirmed / (confirmed + rejected) if (confirmed + rejected) > 0 else 0

        logger.info(f"Feedback summary: {summary}")
        logger.info(f"Precision so far: {precision:.1%} ({confirmed} confirmed / {confirmed + rejected} decided)")

        if precision < 0.3 and total >= 20:
            logger.warning(
                "Low precision detected (<30%). Consider adjusting LLM prompts. "
                "Check calibration_events table for history."
            )
