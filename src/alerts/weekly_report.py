"""
Wöchentlicher Kaufkandidaten-Report.

Nach jedem batch_collect wird ein Markdown-Report mit den Top-Kandidaten
(US + EU) erstellt — unabhängig von Alert-Schwellen. Hintergrund: Die
LLM-Scores clustern eng (58-68), sodass harte Schwellen kaum je reißen.
Das Systemziel ist aber, jede Woche konkrete NAMEN zu nennen. Der Report
geht als GitHub Issue raus und per E-Mail an den Nutzer.
"""

import json
import logging
import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# EU-Ticker tragen ein Exchange-Suffix (z.B. SAP.DE, ASML.AS)
_EU_SUFFIXES = (".DE", ".SW", ".AS", ".PA", ".ST", ".HE", ".CO", ".OL", ".BR", ".VI")


def _is_eu_ticker(ticker: str) -> bool:
    return ticker.upper().endswith(_EU_SUFFIXES)


def _fetch_top_candidates(conn, top_n: int = 15, days_back: int = 8) -> list[dict]:
    """
    Top-Kandidaten der letzten Woche aus company_status + jüngster Evaluation.
    Sortiert nach monopoly_score, bei Gleichstand nach confidence_score.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    rows = conn.execute("""
        SELECT cs.ticker, cs.monopoly_score, cs.confidence_score,
               cs.data_quality_score, cs.current_status,
               cs.consecutive_high_score_runs, cs.last_alert_type,
               (SELECT e.llm_assessment FROM evaluations e
                WHERE e.ticker = cs.ticker
                ORDER BY e.evaluated_at DESC LIMIT 1) AS llm_assessment
        FROM company_status cs
        WHERE cs.last_evaluated >= ?
          AND cs.monopoly_score IS NOT NULL
        ORDER BY cs.monopoly_score DESC, cs.confidence_score DESC
        LIMIT ?
    """, (cutoff, top_n)).fetchall()

    candidates = []
    for row in rows:
        r = dict(row)
        summary = ""
        narrow_market = ""
        try:
            assessment = json.loads(r.get("llm_assessment") or "{}")
            summary = assessment.get("evaluation_summary", "") or ""
            hyps = (assessment.get("market_hypotheses") or {}).get(
                "narrow_market_hypotheses") or []
            if hyps:
                narrow_market = hyps[0].get("narrow_market", "") or ""
        except Exception:
            pass
        candidates.append({
            "ticker": r["ticker"],
            "region": "EU" if _is_eu_ticker(r["ticker"]) else "US",
            "monopoly_score": r.get("monopoly_score") or 0,
            "confidence_score": r.get("confidence_score") or 0,
            "data_quality_score": r.get("data_quality_score") or 0,
            "status": r.get("current_status") or "NONE",
            "consecutive_runs": r.get("consecutive_high_score_runs") or 0,
            "alert_type": r.get("last_alert_type"),
            "summary": summary,
            "narrow_market": narrow_market,
        })
    return candidates


def build_report_markdown(candidates: list[dict]) -> str:
    """Markdown-Report aus der Kandidatenliste."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    eu_count = sum(1 for c in candidates if c["region"] == "EU")

    lines = [
        f"# Thiel-Detector Wochenreport — {today}",
        "",
        f"Top {len(candidates)} Monopol-Kandidaten dieser Woche "
        f"({len(candidates) - eu_count} US, {eu_count} EU), "
        "sortiert nach Monopoly-Score. Kein Kandidat ist eine Kaufempfehlung — "
        "die Liste priorisiert, was eine menschliche Tiefenanalyse verdient.",
        "",
        "| # | Ticker | Region | Mono | Conf | DQ | Runs≥65 | Status | Alert |",
        "|---|--------|--------|------|------|----|---------|--------|-------|",
    ]
    for i, c in enumerate(candidates, 1):
        alert = c["alert_type"] or "—"
        lines.append(
            f"| {i} | **{c['ticker']}** | {c['region']} | {c['monopoly_score']} "
            f"| {c['confidence_score']} | {c['data_quality_score']} "
            f"| {c['consecutive_runs']} | {c['status']} | {alert} |"
        )

    lines.append("")
    lines.append("## Kurzthesen")
    lines.append("")
    for i, c in enumerate(candidates, 1):
        thesis = c["summary"] or "Keine Zusammenfassung verfügbar."
        market = f" — *Enger Markt: {c['narrow_market']}*" if c["narrow_market"] else ""
        lines.append(f"{i}. **{c['ticker']}** ({c['region']}): {thesis}{market}")

    if eu_count == 0:
        lines.append("")
        lines.append("> ⚠️ Diese Woche wurden keine EU-Unternehmen ausgewertet — "
                      "EU-Pipeline prüfen.")

    lines.append("")
    lines.append("---")
    lines.append("*Automatisch generiert vom Thiel Monopolist Detector.*")
    return "\n".join(lines)


def _create_report_issue(markdown: str, config: dict) -> Optional[int]:
    """Report als GitHub Issue posten (Label: weekly-report)."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = config.get("alerts", {}).get("github_repo", "")
    if not token or not repo:
        logger.warning("GitHub token or repo not configured — skipping report issue")
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={
                "title": f"Wochenreport {today} — Top Monopol-Kandidaten",
                "body": markdown,
                "labels": ["weekly-report"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        number = resp.json().get("number")
        logger.info(f"Weekly report issue #{number} created")
        return number
    except Exception as e:
        logger.error(f"Weekly report issue creation failed: {e}")
        return None


def _send_report_email(markdown: str, config: dict) -> bool:
    """Report per Gmail-SMTP verschicken (einfaches HTML aus Markdown)."""
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    if not all([sender, password, recipient]):
        logger.warning("Email credentials not configured — skipping report email")
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = config.get("alerts", {}).get("email_subject_prefix", "[Thiel Detector]")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{prefix} Wochenreport {today} — Top Kandidaten"
    msg["From"] = sender
    msg["To"] = recipient
    # Markdown als <pre> — robust ohne Markdown-Renderer-Abhängigkeit
    html = (
        "<html><body style='font-family: -apple-system, sans-serif;'>"
        f"<pre style='white-space: pre-wrap; font-size: 13px;'>{markdown}</pre>"
        "</body></html>"
    )
    msg.attach(MIMEText(markdown, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        logger.info("Weekly report email sent")
        return True
    except Exception as e:
        logger.error(f"Weekly report email failed: {e}")
        return False


def post_weekly_report(conn, config: dict, top_n: int = 15,
                       dry_run: bool = False) -> dict:
    """
    Erstellt und verschickt den Wochenreport.
    Gibt Outcome-Dict zurück: {"candidates": N, "issue": #, "email_sent": bool}.
    """
    candidates = _fetch_top_candidates(conn, top_n=top_n)
    if not candidates:
        logger.warning("Weekly report: no candidates evaluated this week — skipping")
        return {"candidates": 0, "issue": None, "email_sent": False}

    markdown = build_report_markdown(candidates)

    if dry_run:
        logger.info(f"DRY RUN — weekly report with {len(candidates)} candidates:\n{markdown}")
        return {"candidates": len(candidates), "issue": None,
                "email_sent": False, "dry_run": True}

    issue = _create_report_issue(markdown, config)
    email_sent = _send_report_email(markdown, config)
    return {"candidates": len(candidates), "issue": issue, "email_sent": email_sent}
