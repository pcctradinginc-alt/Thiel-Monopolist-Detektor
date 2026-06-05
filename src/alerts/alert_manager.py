"""
Alert system — email notifications + GitHub Issues for human feedback.

Alert philosophy:
  - Alert on NEW evidence, not just high scores
  - Hysteresis: no repeat alert within cooldown period
  - Requires confirmation across multiple runs for STRONG status
  - GitHub Issues enable structured human feedback via labels
"""

import json
import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
import requests

logger = logging.getLogger(__name__)

ALERT_DESCRIPTIONS = {
    "HIDDEN_WEDGE_DETECTED": "Narrow market dominance detected beneath broad market claims",
    "SUBSTITUTE_GAP_DETECTED": "No close substitute found — customers likely locked in",
    "LOCK_IN_STRENGTHENING": "Evidence of increasing customer lock-in over time",
    "SCALE_INFLECTION": "Cost structure scaling better than revenue — emerging leverage",
    "CUSTOMER_EXPANSION_SIGNAL": "Existing customers expanding usage without proportional S&M spend",
    "IPO_WITH_NARROW_DOMINANCE": "Recent IPO with unusually focused market position",
    "MOAT_EVIDENCE_IMPROVED": "Moat evidence stronger than previous evaluation",
    "MOAT_RISK_DETECTED": "Previously strong moat showing signs of weakening",
}


def should_send_alert(
    ticker: str,
    assessment: dict,
    previous_status: Optional[dict],
    config: dict
) -> tuple[bool, str]:
    """
    Hysteresis check: should we send an alert for this evaluation?

    Returns (should_alert, reason)
    """
    scores = assessment.get("scores", {})
    monopoly_score = scores.get("monopoly_score", 0)
    confidence_score = scores.get("confidence_score", 0)
    data_quality_score = scores.get("data_quality_score", 0)
    alert_type = assessment.get("alert_type")
    status = assessment.get("status", "NONE")

    thresholds = config.get("screening", {})
    min_monopoly = thresholds.get("min_monopoly_score_alert", 65)
    min_confidence = thresholds.get("min_confidence_score_alert", 60)
    min_data_quality = thresholds.get("min_data_quality_score_alert", 55)
    cooldown_days = config.get("alerts", {}).get("alert_cooldown_days", 21)
    min_delta = config.get("alerts", {}).get("min_score_delta_for_alert", 8)

    # Must have alert type
    if not alert_type:
        return False, "No alert type assigned"

    # Score thresholds
    if monopoly_score < min_monopoly:
        return False, f"monopoly_score {monopoly_score} < threshold {min_monopoly}"
    if confidence_score < min_confidence:
        return False, f"confidence_score {confidence_score} < threshold {min_confidence}"
    if data_quality_score < min_data_quality:
        return False, f"data_quality_score {data_quality_score} < threshold {min_data_quality}"

    # Cooldown check
    if previous_status:
        last_alert_date = previous_status.get("last_alert_date")
        if last_alert_date:
            last_alert = datetime.fromisoformat(last_alert_date)
            cooldown_end = last_alert + timedelta(days=cooldown_days)
            if datetime.now(timezone.utc) < cooldown_end:
                # Only override cooldown if MOAT_RISK_DETECTED or big score jump
                if alert_type != "MOAT_RISK_DETECTED":
                    prev_monopoly = previous_status.get("monopoly_score", 0)
                    if monopoly_score - prev_monopoly < min_delta:
                        return False, f"Within cooldown period and score delta < {min_delta}"

    return True, f"All thresholds met — alert type: {alert_type}"


def format_email_html(ticker: str, assessment: dict, filing_data: dict, hypotheses: dict) -> str:
    """Format a rich HTML alert email."""
    scores = assessment.get("scores", {})
    alert_type = assessment.get("alert_type", "UNKNOWN")
    alert_desc = ALERT_DESCRIPTIONS.get(alert_type, "")
    criteria = assessment.get("criteria", {})
    summary = assessment.get("evaluation_summary", "")
    next_steps = assessment.get("next_verification_steps", [])
    status = assessment.get("status", "")

    # Color coding
    monopoly_score = scores.get("monopoly_score", 0)
    score_color = "#2d7a2d" if monopoly_score >= 75 else "#b07a00" if monopoly_score >= 55 else "#888"

    narrow_markets = hypotheses.get("narrow_market_hypotheses", [])
    primary_hypothesis = narrow_markets[0] if narrow_markets else {}

    def score_bar(score):
        filled = int(score / 10)
        return "█" * filled + "░" * (10 - filled)

    def format_list(items, limit=4):
        if not items:
            return "<li><em>None identified</em></li>"
        return "".join(f"<li>{item}</li>" for item in items[:limit])

    criteria_html = ""
    for crit_key, crit_data in criteria.items():
        crit_name = crit_key.replace("_", " ").title()
        crit_score = crit_data.get("score", 0)
        evidence = crit_data.get("evidence", [])
        counter = crit_data.get("counter_evidence", [])
        criteria_html += f"""
        <div style="margin: 16px 0; padding: 12px; background: #f8f8f8; border-radius: 6px;">
          <strong>{crit_name}</strong> — Score: {crit_score}/100
          <div style="font-family: monospace; color: {score_color};">{score_bar(crit_score)}</div>
          <div style="margin-top: 8px;">
            <span style="color: #2d7a2d; font-weight: bold;">✓ Evidence:</span>
            <ul style="margin: 4px 0;">{"".join(f"<li>{e}</li>" for e in evidence[:3])}</ul>
          </div>
          <div style="margin-top: 8px;">
            <span style="color: #c0392b; font-weight: bold;">✗ Counter-evidence:</span>
            <ul style="margin: 4px 0;">{"".join(f"<li>{c}</li>" for c in counter[:3])}</ul>
          </div>
        </div>"""

    return f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           font-size: 14px; color: #222; max-width: 680px; margin: 0 auto; padding: 20px; }}
    .header {{ background: #1a1a2e; color: white; padding: 20px; border-radius: 8px; }}
    .alert-badge {{ display: inline-block; background: {score_color}; color: white;
                   padding: 4px 12px; border-radius: 4px; font-size: 12px;
                   font-weight: bold; letter-spacing: 0.05em; }}
    .scores {{ display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }}
    .score-box {{ padding: 12px 16px; background: #f0f4f8; border-radius: 6px; text-align: center; }}
    .score-number {{ font-size: 24px; font-weight: bold; color: {score_color}; }}
    .section {{ margin: 20px 0; }}
    h3 {{ color: #333; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
    .hypothesis-box {{ background: #e8f4e8; border-left: 3px solid #2d7a2d;
                      padding: 12px; border-radius: 0 6px 6px 0; margin: 12px 0; }}
    .next-steps {{ background: #fff3cd; padding: 12px; border-radius: 6px; }}
    .footer {{ font-size: 11px; color: #888; margin-top: 24px; border-top: 1px solid #eee; padding-top: 12px; }}
  </style>
</head>
<body>
  <div class="header">
    <h2 style="margin: 0 0 8px 0;">🔍 Thiel Monopolist Detector</h2>
    <div class="alert-badge">{alert_type}</div>
    <span style="margin-left: 12px; font-size: 13px; color: #ccc;">{alert_desc}</span>
    <h1 style="margin: 12px 0 0 0; font-size: 28px;">{ticker}</h1>
    <p style="margin: 4px 0 0 0; color: #aaa; font-size: 13px;">Status: {status}</p>
  </div>

  <div class="scores">
    <div class="score-box">
      <div class="score-number">{scores.get("monopoly_score", 0)}</div>
      <div style="font-size: 11px; color: #666;">Monopoly Score</div>
    </div>
    <div class="score-box">
      <div class="score-number">{scores.get("confidence_score", 0)}</div>
      <div style="font-size: 11px; color: #666;">Confidence</div>
    </div>
    <div class="score-box">
      <div class="score-number">{scores.get("data_quality_score", 0)}</div>
      <div style="font-size: 11px; color: #666;">Data Quality</div>
    </div>
  </div>

  <div class="section">
    <h3>Summary</h3>
    <p>{summary}</p>
  </div>

  <div class="section">
    <h3>Primary Market Hypothesis</h3>
    <div class="hypothesis-box">
      <strong>Claimed market:</strong> {hypotheses.get("company_claimed_market", "Unknown")}<br><br>
      <strong>Actual narrow market:</strong> {primary_hypothesis.get("narrow_market", "N/A")}<br>
      <em>{primary_hypothesis.get("why_narrow", "")}</em>
    </div>
  </div>

  <div class="section">
    <h3>Thiel Criteria Analysis</h3>
    {criteria_html}
  </div>

  <div class="next-steps section">
    <h3>⚡ Next Verification Steps</h3>
    <ul>
      {"".join(f"<li>{step}</li>" for step in next_steps[:5])}
    </ul>
    <p style="font-size: 12px; color: #666;">
      This is a candidate flagged by an automated system. Human analysis required before any investment decision.
    </p>
  </div>

  <div class="footer">
    <p>Thiel Monopolist Detector | Automated screening system<br>
    Filing date: {filing_data.get("filing_date", "unknown")} |
    Data sources: {"10-K" if filing_data.get("has_10k") else ""} {"S-1" if filing_data.get("has_s1") else ""} yfinance
    </p>
    <p>Reply to this GitHub Issue with labels: <strong>confirmed</strong> / <strong>rejected</strong> /
    <strong>watchlist</strong> / <strong>too-early</strong></p>
  </div>
</body>
</html>"""


def send_email_alert(
    ticker: str,
    assessment: dict,
    filing_data: dict,
    hypotheses: dict,
    config: dict
) -> bool:
    """Send HTML email alert via Gmail SMTP."""
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT")

    if not all([sender, password, recipient]):
        logger.warning("Email credentials not configured — skipping email")
        return False

    alert_type = assessment.get("alert_type", "ALERT")
    monopoly_score = assessment.get("scores", {}).get("monopoly_score", 0)
    subject_prefix = config.get("alerts", {}).get("email_subject_prefix", "[Thiel Detector]")
    subject = f"{subject_prefix} {ticker} — {alert_type} (Score: {monopoly_score})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    html_content = format_email_html(ticker, assessment, filing_data, hypotheses)
    msg.attach(MIMEText(html_content, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        logger.info(f"Email sent for {ticker}: {alert_type}")
        return True
    except Exception as e:
        logger.error(f"Email send failed for {ticker}: {e}")
        return False


def create_github_issue(
    ticker: str,
    assessment: dict,
    filing_data: dict,
    hypotheses: dict,
    config: dict
) -> Optional[int]:
    """Create a GitHub Issue for human feedback via label-based workflow."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = config.get("alerts", {}).get("github_repo", "")

    if not token or not repo:
        logger.warning("GitHub token or repo not configured — skipping issue creation")
        return None

    scores = assessment.get("scores", {})
    alert_type = assessment.get("alert_type", "UNKNOWN")
    summary = assessment.get("evaluation_summary", "")
    status = assessment.get("status", "")

    narrow_markets = hypotheses.get("narrow_market_hypotheses", [])
    primary = narrow_markets[0] if narrow_markets else {}

    criteria = assessment.get("criteria", {})
    criteria_md = ""
    for crit_key, crit_data in criteria.items():
        crit_name = crit_key.replace("_", " ").title()
        crit_score = crit_data.get("score", 0)
        evidence = crit_data.get("evidence", [])[:2]
        counter = crit_data.get("counter_evidence", [])[:2]
        criteria_md += f"""
### {crit_name} — {crit_score}/100

**Evidence:** {" | ".join(evidence) if evidence else "None"}
**Counter-evidence:** {" | ".join(counter) if counter else "None"}
"""

    next_steps = assessment.get("next_verification_steps", [])

    issue_body = f"""## {ticker} — {alert_type}

**Status:** `{status}` | **Monopoly Score:** {scores.get("monopoly_score", 0)}/100 | **Confidence:** {scores.get("confidence_score", 0)}/100 | **Data Quality:** {scores.get("data_quality_score", 0)}/100

---

### Summary
{summary}

### Primary Market Hypothesis
**Claimed market:** {hypotheses.get("company_claimed_market", "Unknown")}
**Actual narrow market:** {primary.get("narrow_market", "N/A")}
{primary.get("why_narrow", "")}

### Thiel Criteria
{criteria_md}

### Next Verification Steps
{chr(10).join(f"- [ ] {step}" for step in next_steps[:5])}

---

*Automated alert by Thiel Monopolist Detector*
*Filing date: {filing_data.get("filing_date", "unknown")}*

**Please label this issue:**
- `confirmed` — Genuine Thiel monopoly candidate
- `rejected` — False positive
- `watchlist` — Interesting but needs more time
- `too-early` — Re-evaluate in 6 months
"""

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "title": f"[{alert_type}] {ticker} — Score: {scores.get('monopoly_score', 0)}/100",
        "body": issue_body,
        "labels": ["needs-review", alert_type.lower().replace("_", "-")]
    }

    try:
        resp = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers=headers,
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        issue_number = resp.json().get("number")
        logger.info(f"GitHub Issue #{issue_number} created for {ticker}")
        return issue_number
    except Exception as e:
        logger.error(f"GitHub Issue creation failed for {ticker}: {e}")
        return None


def process_alerts(
    ticker: str,
    analysis_result: dict,
    filing_data: dict,
    previous_status: Optional[dict],
    config: dict,
    conn,
    dry_run: bool = False
) -> dict:
    """
    Main alert processor. Checks hysteresis, sends email, creates GitHub Issue.
    Returns dict with alert outcome.
    """
    assessment = analysis_result.get("assessment", {})
    hypotheses = analysis_result.get("market_hypotheses", {})

    should_alert, reason = should_send_alert(ticker, assessment, previous_status, config)

    outcome = {
        "ticker": ticker,
        "should_alert": should_alert,
        "reason": reason,
        "email_sent": False,
        "github_issue": None
    }

    if not should_alert:
        logger.info(f"{ticker}: No alert — {reason}")
        return outcome

    if dry_run:
        logger.info(f"{ticker}: DRY RUN — would alert: {assessment.get('alert_type')}")
        outcome["dry_run"] = True
        return outcome

    # Send email
    outcome["email_sent"] = send_email_alert(ticker, assessment, filing_data, hypotheses, config)

    # Create GitHub Issue
    if config.get("alerts", {}).get("create_github_issues", True):
        outcome["github_issue"] = create_github_issue(
            ticker, assessment, filing_data, hypotheses, config
        )

    return outcome
