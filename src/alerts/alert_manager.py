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


def format_email_html(ticker: str, assessment: dict, filing_data: dict, hypotheses: dict,
                      issue_number: int = None) -> str:
    """Format alert email in clean Apple-style design."""
    scores      = assessment.get("scores", {})
    alert_type  = assessment.get("alert_type", "UNKNOWN")
    alert_desc  = ALERT_DESCRIPTIONS.get(alert_type, "")
    criteria    = assessment.get("criteria", {})
    summary     = assessment.get("evaluation_summary", "")
    next_steps  = assessment.get("next_verification_steps", [])
    status      = assessment.get("status", "")

    monopoly_score   = scores.get("monopoly_score", 0)
    confidence_score = scores.get("confidence_score", 0)
    data_quality     = scores.get("data_quality_score", 0)

    narrow_markets    = hypotheses.get("narrow_market_hypotheses", [])
    primary           = narrow_markets[0] if narrow_markets else {}
    claimed_market    = hypotheses.get("company_claimed_market", "—")
    filing_date       = filing_data.get("filing_date", "—")

    # Feedback buttons (only if issue_number and worker URL are available)
    from alerts.feedback_links import make_feedback_links
    feedback_html = ""
    if issue_number:
        links = make_feedback_links(issue_number)
        if links:
            repo = os.environ.get("GITHUB_REPO",
                "pcctradinginc-alt/Thiel-Monopolist-Detektor")
            issue_url = f"https://github.com/{repo}/issues/{issue_number}"
            feedback_html = f"""
    <tr><td style="height:12px;"></td></tr>
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 16px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Deine Einschätzung</p>
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="padding:0 4px 0 0;">
            <a href="{links['confirmed']}" style="display:block; text-align:center;
               background:#34c759; color:white; text-decoration:none; font-size:13px;
               font-weight:600; padding:10px 0; border-radius:8px;">✓ Bestätigt</a>
          </td>
          <td style="padding:0 4px;">
            <a href="{links['rejected']}" style="display:block; text-align:center;
               background:#ff3b30; color:white; text-decoration:none; font-size:13px;
               font-weight:600; padding:10px 0; border-radius:8px;">✗ Abgelehnt</a>
          </td>
          <td style="padding:0 4px;">
            <a href="{links['watchlist']}" style="display:block; text-align:center;
               background:#0071e3; color:white; text-decoration:none; font-size:13px;
               font-weight:600; padding:10px 0; border-radius:8px;">👁 Watchlist</a>
          </td>
          <td style="padding:0 0 0 4px;">
            <a href="{links['too-early']}" style="display:block; text-align:center;
               background:#f0a500; color:white; text-decoration:none; font-size:13px;
               font-weight:600; padding:10px 0; border-radius:8px;">⏳ Zu früh</a>
          </td>
        </tr></table>
        <p style="margin:12px 0 0 0; font-size:11px; color:#86868b; text-align:center;">
          <a href="{issue_url}" style="color:#86868b;">Issue #{issue_number} auf GitHub öffnen →</a>
        </p>
      </td>
    </tr>"""

    # Score ring colour: blue ≥75, amber 55–74, gray <55
    def ring_color(s):
        return "#0071e3" if s >= 75 else "#f0a500" if s >= 55 else "#86868b"

    def score_ring(score, label):
        color = ring_color(score)
        return f"""
        <td style="text-align:center; padding: 0 20px;">
          <div style="display:inline-block; width:64px; height:64px; border-radius:50%;
                      border: 3px solid {color}; line-height:58px; text-align:center;">
            <span style="font-size:20px; font-weight:600; color:{color};">{score}</span>
          </div>
          <div style="margin-top:6px; font-size:11px; color:#86868b;
                      letter-spacing:0.04em; text-transform:uppercase;">{label}</div>
        </td>"""

    def criteria_row(key, data):
        name   = key.replace("_", " ").title()
        s      = data.get("score", 0)
        color  = ring_color(s)
        ev     = data.get("evidence", [])[:2]
        ce     = data.get("counter_evidence", [])[:2]
        bar_w  = s  # percent
        ev_html = "".join(f'<li style="margin:3px 0;">{e}</li>' for e in ev) or "<li style='color:#86868b'>—</li>"
        ce_html = "".join(f'<li style="margin:3px 0;">{e}</li>' for e in ce) or "<li style='color:#86868b'>—</li>"
        return f"""
        <tr>
          <td colspan="2" style="padding: 0 0 1px 0;">
            <div style="background:#ffffff; border:1px solid #d2d2d7; border-radius:12px;
                        padding:16px 20px; margin-bottom:8px;">
              <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                <span style="font-size:13px; font-weight:600; color:#1d1d1f;">{name}</span>
                <span style="font-size:13px; font-weight:600; color:{color};">{s}/100</span>
              </div>
              <div style="height:4px; background:#f5f5f7; border-radius:2px; margin-bottom:12px;">
                <div style="height:4px; width:{bar_w}%; background:{color}; border-radius:2px;"></div>
              </div>
              <table width="100%" style="border-collapse:collapse;">
                <tr>
                  <td width="50%" style="vertical-align:top; padding-right:12px;">
                    <div style="font-size:11px; font-weight:600; color:#34c759;
                                text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;">For</div>
                    <ul style="margin:0; padding-left:16px; font-size:12px; color:#3a3a3c;">{ev_html}</ul>
                  </td>
                  <td width="50%" style="vertical-align:top;">
                    <div style="font-size:11px; font-weight:600; color:#ff3b30;
                                text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px;">Against</div>
                    <ul style="margin:0; padding-left:16px; font-size:12px; color:#3a3a3c;">{ce_html}</ul>
                  </td>
                </tr>
              </table>
            </div>
          </td>
        </tr>"""

    criteria_rows = "".join(criteria_row(k, v) for k, v in criteria.items())
    steps_html    = "".join(
        f'<li style="margin:6px 0; font-size:13px; color:#3a3a3c;">{s}</li>'
        for s in next_steps[:4]
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0; padding:0; background:#f5f5f7;
             font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7; padding:32px 0;">
  <tr><td align="center">
  <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%;">

    <!-- Header -->
    <tr>
      <td style="padding-bottom:8px;">
        <p style="margin:0; font-size:12px; color:#86868b; letter-spacing:0.06em;
                  text-transform:uppercase;">Thiel Monopolist Detector</p>
      </td>
    </tr>

    <!-- Ticker card -->
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:28px 28px 24px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:12px;">
        <p style="margin:0 0 4px 0; font-size:12px; font-weight:600; color:#0071e3;
                  letter-spacing:0.08em; text-transform:uppercase;">{alert_type.replace("_"," ")}</p>
        <h1 style="margin:0 0 6px 0; font-size:36px; font-weight:700;
                   letter-spacing:-0.02em; color:#1d1d1f;">{ticker}</h1>
        <p style="margin:0 0 16px 0; font-size:14px; color:#86868b;">{alert_desc}</p>
        <hr style="border:none; border-top:1px solid #d2d2d7; margin:0 0 20px 0;">
        <!-- Score rings -->
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            {score_ring(monopoly_score,   "Monopoly")}
            {score_ring(confidence_score, "Confidence")}
            {score_ring(data_quality,     "Data Quality")}
          </tr>
        </table>
      </td>
    </tr>

    <tr><td style="height:12px;"></td></tr>

    <!-- Summary card -->
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 8px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Summary</p>
        <p style="margin:0; font-size:14px; line-height:1.6; color:#1d1d1f;">{summary}</p>
      </td>
    </tr>

    <tr><td style="height:12px;"></td></tr>

    <!-- Hypothesis card -->
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 16px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Market Hypothesis</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="50%" style="vertical-align:top; padding-right:16px;">
              <p style="margin:0 0 4px 0; font-size:11px; color:#86868b;">Claimed market</p>
              <p style="margin:0; font-size:13px; color:#3a3a3c;">{claimed_market}</p>
            </td>
            <td width="50%" style="vertical-align:top;
                border-left:1px solid #d2d2d7; padding-left:16px;">
              <p style="margin:0 0 4px 0; font-size:11px; color:#86868b;">Actual narrow market</p>
              <p style="margin:0; font-size:13px; font-weight:600; color:#1d1d1f;">
                {primary.get("narrow_market","—")}</p>
              <p style="margin:6px 0 0 0; font-size:12px; color:#86868b; line-height:1.5;">
                {primary.get("why_narrow","")}</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <tr><td style="height:12px;"></td></tr>

    <!-- Criteria cards -->
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 16px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Thiel Criteria</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          {criteria_rows}
        </table>
      </td>
    </tr>

    <tr><td style="height:12px;"></td></tr>

    <!-- Next steps card -->
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 12px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Next Steps</p>
        <ul style="margin:0; padding-left:18px;">{steps_html}</ul>
      </td>
    </tr>

    {feedback_html}

    <tr><td style="height:12px;"></td></tr>

    <!-- Footer -->
    <tr>
      <td style="padding:4px 4px 32px;">
        <p style="margin:0; font-size:11px; color:#86868b; line-height:1.6;">
          Automated screening — human analysis required before any investment decision.<br>
          Filing: {filing_date} &nbsp;·&nbsp; Status: {status}
        </p>
      </td>
    </tr>

  </table>
  </td></tr>
  </table>

</body>
</html>"""


def send_email_alert(
    ticker: str,
    assessment: dict,
    filing_data: dict,
    hypotheses: dict,
    config: dict,
    issue_number: int = None,
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

    html_content = format_email_html(ticker, assessment, filing_data, hypotheses, issue_number)
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

    # Create GitHub Issue first (so we have the issue number for email buttons)
    issue_number = None
    if config.get("alerts", {}).get("create_github_issues", True):
        issue_number = create_github_issue(ticker, assessment, filing_data, hypotheses, config)
        outcome["github_issue"] = issue_number

    # Send email with feedback buttons linked to the issue
    outcome["email_sent"] = send_email_alert(
        ticker, assessment, filing_data, hypotheses, config, issue_number=issue_number
    )

    return outcome
