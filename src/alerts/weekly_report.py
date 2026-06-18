"""
Wöchentlicher Kaufkandidaten-Report.

Nach jedem batch_collect wird ein Markdown-Report mit den Top-Kandidaten
(US + EU) erstellt — unabhängig von Alert-Schwellen. Hintergrund: Die
LLM-Scores clustern eng (58-68), sodass harte Schwellen kaum je reißen.
Das Systemziel ist aber, jede Woche konkrete NAMEN zu nennen. Der Report
geht als GitHub Issue raus und per E-Mail an den Nutzer.
"""

import html
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


def _enrich_with_entry_signals(candidates: list[dict]) -> None:
    """Einstiegssignal (Preis, Drawdown, P/S, Einstufung) pro Kandidat ergänzen."""
    from analysis.entry_signal import compute_entry_signal
    for c in candidates:
        try:
            c["entry"] = compute_entry_signal(
                c["ticker"], c["monopoly_score"], c["consecutive_runs"])
        except Exception as e:
            logger.warning(f"{c['ticker']}: entry signal failed: {e}")
            c["entry"] = {"classification": "WATCH", "reason": "keine Marktdaten"}


def _fetch_open_signals_performance(conn) -> list[dict]:
    """
    Offene Signale mit Performance seit Signalzeitpunkt.
    Feedback-Loop: Hätten die bisherigen Signale Geld verdient?
    """
    from analysis.entry_signal import fetch_current_price
    try:
        rows = conn.execute("""
            SELECT ticker, signal_date, alert_type, price_at_signal, decision_status
            FROM signals
            WHERE decision_status IN ('WATCH', 'CANDIDATE', 'BOUGHT')
              AND price_at_signal IS NOT NULL
            ORDER BY signal_date DESC
            LIMIT 20
        """).fetchall()
    except Exception as e:
        logger.warning(f"Open signals query failed: {e}")
        return []

    results = []
    for row in rows:
        r = dict(row)
        current = fetch_current_price(r["ticker"])
        perf = None
        if current and r["price_at_signal"]:
            perf = round((current / r["price_at_signal"] - 1) * 100, 1)
        results.append({**r, "current_price": current, "performance_pct": perf})
    return results


def build_report_markdown(candidates: list[dict],
                          open_signals: list[dict] = None) -> str:
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
        "| # | Ticker | Region | Mono | Runs≥65 | Status | Preis | Δ52wH | P/S | Einstufung |",
        "|---|--------|--------|------|---------|--------|-------|-------|-----|------------|",
    ]
    for i, c in enumerate(candidates, 1):
        e = c.get("entry", {})
        price = e.get("price")
        dd = e.get("drawdown_pct")
        ps = e.get("ps_ratio")
        lines.append(
            f"| {i} | **{c['ticker']}** | {c['region']} | {c['monopoly_score']} "
            f"| {c['consecutive_runs']} | {c['status']} "
            f"| {price if price is not None else '—'} "
            f"| {f'-{dd}%' if dd is not None else '—'} "
            f"| {ps if ps is not None else '—'} "
            f"| **{e.get('classification', 'WATCH')}** |"
        )

    # Kauffenster prominent herausstellen
    buy_windows = [c for c in candidates
                   if c.get("entry", {}).get("classification") == "KAUFFENSTER"]
    if buy_windows:
        lines.append("")
        lines.append("## 🎯 Kauffenster (bestätigter Moat + Preis)")
        lines.append("")
        for c in buy_windows:
            lines.append(f"- **{c['ticker']}** ({c['region']}): {c['entry']['reason']}")
    else:
        lines.append("")
        lines.append("> Kein Kandidat erfüllt aktuell beides: bestätigter Moat "
                      "(≥2 Runs ≥65) **und** vernünftiger Einstiegspreis.")

    lines.append("")
    lines.append("## Kurzthesen")
    lines.append("")
    for i, c in enumerate(candidates, 1):
        thesis = c["summary"] or "Keine Zusammenfassung verfügbar."
        market = f" — *Enger Markt: {c['narrow_market']}*" if c["narrow_market"] else ""
        entry_reason = c.get("entry", {}).get("reason", "")
        entry_note = f" — *Einstieg: {entry_reason}*" if entry_reason else ""
        lines.append(f"{i}. **{c['ticker']}** ({c['region']}): {thesis}{market}{entry_note}")

    # Performance-Tracking: Hätten die bisherigen Signale Geld verdient?
    if open_signals:
        lines.append("")
        lines.append("## Offene Signale — Performance seit Signal")
        lines.append("")
        lines.append("| Ticker | Signal | Datum | Kurs damals | Kurs jetzt | Performance |")
        lines.append("|--------|--------|-------|-------------|------------|-------------|")
        for s in open_signals:
            perf = s.get("performance_pct")
            perf_str = f"{'+' if perf and perf > 0 else ''}{perf}%" if perf is not None else "—"
            lines.append(
                f"| {s['ticker']} | {s.get('alert_type') or '—'} "
                f"| {(s.get('signal_date') or '')[:10]} "
                f"| {s.get('price_at_signal') or '—'} "
                f"| {s.get('current_price') or '—'} | {perf_str} |"
            )

    if eu_count == 0:
        lines.append("")
        lines.append("> ⚠️ Diese Woche wurden keine EU-Unternehmen ausgewertet — "
                      "EU-Pipeline prüfen.")

    lines.append("")
    lines.append("---")
    lines.append("*Automatisch generiert vom Thiel Monopolist Detector. "
                  "Einstufungen sind regelbasierte Priorisierung, keine Anlageberatung.*")
    return "\n".join(lines)


def _score_color(s: int) -> str:
    """Score-Farbe wie in der Alert-Mail: blau ≥75, amber 55–74, grau <55."""
    return "#0071e3" if s >= 75 else "#f0a500" if s >= 55 else "#86868b"


_CLASS_COLORS = {
    "KAUFFENSTER":     "#34c759",
    "QUALITAET_TEUER": "#f0a500",
    "THESE_PRUEFEN":   "#ff9500",
    "WATCH":           "#86868b",
}


def _esc(value) -> str:
    """HTML-sicher escapen (LLM-Texte können <, >, & enthalten)."""
    return html.escape(str(value)) if value is not None else "—"


def build_report_html(candidates: list[dict],
                      open_signals: list[dict] = None) -> str:
    """
    Apple-Style HTML-Report aus der Kandidatenliste — gleiches Karten-Design
    wie die Alert-Mail (format_email_html). Inhaltlich identisch zum Markdown.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    eu_count = sum(1 for c in candidates if c["region"] == "EU")

    # ── Kandidaten-Tabelle ────────────────────────────────────────────────────
    rows = ""
    for i, c in enumerate(candidates, 1):
        e = c.get("entry", {})
        price = e.get("price")
        dd    = e.get("drawdown_pct")
        ps    = e.get("ps_ratio")
        cls   = e.get("classification", "WATCH")
        cls_color = _CLASS_COLORS.get(cls, "#86868b")
        mono  = c["monopoly_score"]
        rows += f"""
          <tr style="border-top:1px solid #f0f0f2;">
            <td style="padding:8px 6px; font-size:12px; color:#86868b;">{i}</td>
            <td style="padding:8px 6px; font-size:13px; font-weight:600; color:#1d1d1f;">{_esc(c['ticker'])}</td>
            <td style="padding:8px 6px; font-size:12px; color:#86868b;">{_esc(c['region'])}</td>
            <td style="padding:8px 6px; font-size:13px; font-weight:600; color:{_score_color(mono)};">{mono}</td>
            <td style="padding:8px 6px; font-size:12px; color:#3a3a3c; text-align:center;">{c['consecutive_runs']}</td>
            <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{_esc(c['status'])}</td>
            <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{price if price is not None else '—'}</td>
            <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{f'-{dd}%' if dd is not None else '—'}</td>
            <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{ps if ps is not None else '—'}</td>
            <td style="padding:8px 6px;">
              <span style="display:inline-block; font-size:11px; font-weight:600; color:#ffffff;
                           background:{cls_color}; border-radius:6px; padding:3px 8px;">{_esc(cls)}</span>
            </td>
          </tr>"""

    table_card = f"""
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:20px 24px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 12px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Top-Kandidaten</p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr style="text-align:left;">
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">#</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Ticker</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Region</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Mono</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em; text-align:center;">Runs≥65</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Status</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Preis</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Δ52wH</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">P/S</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Einstufung</th>
          </tr>
          {rows}
        </table>
      </td>
    </tr>
    <tr><td style="height:12px;"></td></tr>"""

    # ── Kauffenster ───────────────────────────────────────────────────────────
    buy_windows = [c for c in candidates
                   if c.get("entry", {}).get("classification") == "KAUFFENSTER"]
    if buy_windows:
        items = "".join(
            f'<li style="margin:6px 0; font-size:13px; color:#1d1d1f;">'
            f'<strong>{_esc(c["ticker"])}</strong> ({_esc(c["region"])}): '
            f'{_esc(c["entry"]["reason"])}</li>'
            for c in buy_windows
        )
        buy_card = f"""
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08); border-left:4px solid #34c759;">
        <p style="margin:0 0 12px 0; font-size:11px; font-weight:600; color:#34c759;
                  text-transform:uppercase; letter-spacing:0.06em;">🎯 Kauffenster (bestätigter Moat + Preis)</p>
        <ul style="margin:0; padding-left:18px;">{items}</ul>
      </td>
    </tr>
    <tr><td style="height:12px;"></td></tr>"""
    else:
        buy_card = """
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:20px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0; font-size:13px; color:#86868b; line-height:1.5;">
          Kein Kandidat erfüllt aktuell beides: bestätigter Moat (≥2 Runs ≥65)
          <strong>und</strong> vernünftiger Einstiegspreis.</p>
      </td>
    </tr>
    <tr><td style="height:12px;"></td></tr>"""

    # ── Kurzthesen ────────────────────────────────────────────────────────────
    theses = ""
    for i, c in enumerate(candidates, 1):
        thesis = _esc(c["summary"] or "Keine Zusammenfassung verfügbar.")
        market = (f' <span style="color:#86868b; font-style:italic;">— Enger Markt: '
                  f'{_esc(c["narrow_market"])}</span>') if c["narrow_market"] else ""
        entry_reason = c.get("entry", {}).get("reason", "")
        entry_note = (f' <span style="color:#86868b; font-style:italic;">— Einstieg: '
                      f'{_esc(entry_reason)}</span>') if entry_reason else ""
        theses += f"""
          <li style="margin:10px 0; font-size:13px; line-height:1.5; color:#1d1d1f;">
            <strong style="color:#0071e3;">{_esc(c['ticker'])}</strong>
            ({_esc(c['region'])}): {thesis}{market}{entry_note}
          </li>"""

    theses_card = f"""
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:24px 28px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 12px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Kurzthesen</p>
        <ol style="margin:0; padding-left:18px;">{theses}</ol>
      </td>
    </tr>
    <tr><td style="height:12px;"></td></tr>"""

    # ── Offene Signale — Performance ──────────────────────────────────────────
    signals_card = ""
    if open_signals:
        sig_rows = ""
        for s in open_signals:
            perf = s.get("performance_pct")
            if perf is None:
                perf_html = '<span style="color:#86868b;">—</span>'
            else:
                color = "#34c759" if perf > 0 else "#ff3b30" if perf < 0 else "#86868b"
                perf_html = (f'<span style="color:{color}; font-weight:600;">'
                             f'{"+" if perf > 0 else ""}{perf}%</span>')
            sig_rows += f"""
              <tr style="border-top:1px solid #f0f0f2;">
                <td style="padding:8px 6px; font-size:13px; font-weight:600; color:#1d1d1f;">{_esc(s['ticker'])}</td>
                <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{_esc(s.get('alert_type') or '—')}</td>
                <td style="padding:8px 6px; font-size:12px; color:#86868b;">{_esc((s.get('signal_date') or '')[:10])}</td>
                <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{s.get('price_at_signal') or '—'}</td>
                <td style="padding:8px 6px; font-size:12px; color:#3a3a3c;">{s.get('current_price') or '—'}</td>
                <td style="padding:8px 6px; font-size:12px;">{perf_html}</td>
              </tr>"""
        signals_card = f"""
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:20px 24px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 12px 0; font-size:11px; font-weight:600; color:#86868b;
                  text-transform:uppercase; letter-spacing:0.06em;">Offene Signale — Performance seit Signal</p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr style="text-align:left;">
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Ticker</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Signal</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Datum</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Kurs damals</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Kurs jetzt</th>
            <th style="padding:0 6px 8px; font-size:10px; font-weight:600; color:#86868b;
                       text-transform:uppercase; letter-spacing:0.04em;">Performance</th>
          </tr>
          {sig_rows}
        </table>
      </td>
    </tr>
    <tr><td style="height:12px;"></td></tr>"""

    # ── EU-Warnung ────────────────────────────────────────────────────────────
    eu_warning = ""
    if eu_count == 0:
        eu_warning = """
    <tr>
      <td style="background:#fff8e6; border-radius:16px; padding:16px 24px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0; font-size:13px; color:#8a6d00; line-height:1.5;">
          ⚠️ Diese Woche wurden keine EU-Unternehmen ausgewertet — EU-Pipeline prüfen.</p>
      </td>
    </tr>
    <tr><td style="height:12px;"></td></tr>"""

    intro = (f"Top {len(candidates)} Monopol-Kandidaten dieser Woche "
             f"({len(candidates) - eu_count} US, {eu_count} EU), sortiert nach "
             "Monopoly-Score. Kein Kandidat ist eine Kaufempfehlung — die Liste "
             "priorisiert, was eine menschliche Tiefenanalyse verdient.")

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0; padding:0; background:#f5f5f7;
             font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7; padding:32px 0;">
  <tr><td align="center">
  <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px; width:100%;">

    <!-- Header -->
    <tr>
      <td style="padding-bottom:8px;">
        <p style="margin:0; font-size:12px; color:#86868b; letter-spacing:0.06em;
                  text-transform:uppercase;">Thiel Monopolist Detector</p>
      </td>
    </tr>

    <!-- Title card -->
    <tr>
      <td style="background:#ffffff; border-radius:16px; padding:28px 28px 24px;
                 box-shadow:0 1px 3px rgba(0,0,0,.08);">
        <p style="margin:0 0 4px 0; font-size:12px; font-weight:600; color:#0071e3;
                  letter-spacing:0.08em; text-transform:uppercase;">Wochenreport</p>
        <h1 style="margin:0 0 12px 0; font-size:30px; font-weight:700;
                   letter-spacing:-0.02em; color:#1d1d1f;">{today}</h1>
        <p style="margin:0; font-size:14px; line-height:1.6; color:#3a3a3c;">{intro}</p>
      </td>
    </tr>

    <tr><td style="height:12px;"></td></tr>

    {table_card}
    {buy_card}
    {theses_card}
    {signals_card}
    {eu_warning}

    <!-- Footer -->
    <tr>
      <td style="padding:4px 4px 32px;">
        <p style="margin:0; font-size:11px; color:#86868b; line-height:1.6;">
          Automatisch generiert vom Thiel Monopolist Detector.<br>
          Einstufungen sind regelbasierte Priorisierung, keine Anlageberatung.
        </p>
      </td>
    </tr>

  </table>
  </td></tr>
  </table>

</body>
</html>"""


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


def _send_report_email(markdown: str, html_body: str, config: dict) -> bool:
    """Report per Gmail-SMTP verschicken (Apple-Style HTML + Markdown-Fallback)."""
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
    # Markdown als Plaintext-Fallback, gestyltes HTML als bevorzugte Darstellung
    msg.attach(MIMEText(markdown, "plain"))
    msg.attach(MIMEText(html_body, "html"))

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

    _enrich_with_entry_signals(candidates)
    open_signals = _fetch_open_signals_performance(conn)
    markdown = build_report_markdown(candidates, open_signals=open_signals)
    html_body = build_report_html(candidates, open_signals=open_signals)

    if dry_run:
        logger.info(f"DRY RUN — weekly report with {len(candidates)} candidates:\n{markdown}")
        return {"candidates": len(candidates), "issue": None,
                "email_sent": False, "dry_run": True}

    issue = _create_report_issue(markdown, config)
    email_sent = _send_report_email(markdown, html_body, config)
    return {"candidates": len(candidates), "issue": issue, "email_sent": email_sent}
