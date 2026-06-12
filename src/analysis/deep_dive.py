"""
Deep Dive — automatische Tiefenanalyse mit Trade-Empfehlung.

Für die wenigen Kandidaten pro Jahr, die Moat-Bestätigung UND Einstiegsfenster
erreichen, lohnt sich die teuerste Analyse: Sonnet mit Websuche, voller
Filing-Kontext, 4-Jahres-Finanzhistorie. Output ist ein Initiation-Report
(Moat-Audit, Bear/Base/Bull-Kursziele, Einstiegszone, Kill-Kriterien) plus
strukturierte Empfehlung KAUFEN / BEOBACHTEN / ABLEHNEN.

Kostenrahmen: ~0,30-0,80 USD pro Report bei erwartet 2-7 Reports/Jahr.
Die Ausführung bleibt manuell — das System liefert die Entscheidungsvorlage,
keine Order (siehe trades-Tabelle: "Manuelle Entscheidung, nie automatisch").
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


_DEEP_DIVE_PROMPT = """\
Du bist Senior-Analyst und erstellst eine Initiation-of-Coverage-Tiefenanalyse \
im Stil einer Investmentbank — aber radikal ehrlich, ohne Gefälligkeits-Optimismus. \
Grundlage ist Peter Thiels Monopol-Framework (Zero to One).

Das Screening-System hat diese Firma als seltenen Kandidaten bestätigt \
(Moat-Evidenz über mehrere Wochen + Einstiegsfenster). Deine Aufgabe: \
die These mit allen verfügbaren Daten erhärten ODER zerstören. \
Ein ABLEHNEN mit guter Begründung ist genauso wertvoll wie ein KAUFEN.

Nutze die Websuche aktiv für: aktuelle Wettbewerbslandschaft, jüngste \
Nachrichten/Quartalszahlen, Bewertungsvergleich mit Peers, Insider-Aktivität.

Antworte auf DEUTSCH in exakt diesem Format:

Zuerst ein JSON-Block:
```json
{
  "recommendation": "KAUFEN|BEOBACHTEN|ABLEHNEN",
  "confidence": 0-100,
  "entry_low": <Kurs oder null>,
  "entry_high": <Kurs oder null>,
  "stop_price": <Kurs oder null>,
  "target_bear": <Kursziel oder null>,
  "target_base": <Kursziel oder null>,
  "target_bull": <Kursziel oder null>,
  "position_size_pct": <1-5 oder null>,
  "kill_criteria": ["konkret messbare Bedingung, die die These widerlegt", "..."]
}
```

Danach der Markdown-Report mit diesen Abschnitten:
# Tiefenanalyse {ticker}
## 1. Executive Summary & Empfehlung
## 2. Moat-Audit (Thiels 4 Kriterien — Evidenz UND Gegenevidenz)
## 3. Der enge Markt: Definition, Größe, Expansionspfad
## 4. Wettbewerb & Substitute (mit Websuche verifiziert)
## 5. Unit Economics & Finanzhistorie
## 6. Bewertung: Bear / Base / Bull mit Annahmen und Kurszielen
## 7. Einstiegsplan: Zone, Positionsgröße, Stop
## 8. Kill-Kriterien — was die These widerlegen würde
## 9. Risiken & was diese Analyse NICHT weiß

Regeln:
- Kursziele aus nachvollziehbaren Annahmen ableiten (Multiple x Kennzahl), keine Punktlandungs-Scheingenauigkeit
- position_size_pct konservativ (1-5% Portfolio), abhängig von Confidence und Liquidität
- KAUFEN nur, wenn Moat-These UND Bewertung UND Timing zusammenpassen
- Jede Behauptung über Wettbewerber mit Websuche prüfen, nicht aus dem Gedächtnis
"""

_DEEP_DIVE_DATA = """

=== SCREENING-KONTEXT (eigenes System, mehrere Wochen) ===
Ticker: {ticker}
Monopoly-Score: {monopoly_score} (in {consecutive_runs} aufeinanderfolgenden Runs >= 65)
Einstiegssignal: {entry_context}
Score-Historie: {score_history}

=== LETZTE LLM-BEWERTUNG (Screening) ===
{last_assessment}

=== FILING-TEXTE ===
Business Description:
{business_description}

Risk Factors:
{risk_factors}

MD&A / Lagebericht:
{mda}

=== FINANZHISTORIE (yfinance) ===
{financial_history}
"""


def _build_financial_history(ticker: str) -> str:
    """4-Jahres-Finanzhistorie + Bewertung als kompakte Texttabelle."""
    if not YFINANCE_AVAILABLE:
        return "nicht verfügbar"
    lines = []
    try:
        t = yf.Ticker(ticker)
        fin = t.financials
        if fin is not None and not fin.empty:
            rows = ["Total Revenue", "Gross Profit", "Operating Income", "Net Income"]
            years = [str(c)[:10] for c in fin.columns]
            lines.append("Jahr: " + " | ".join(years))
            for r in rows:
                if r in fin.index:
                    vals = [f"{v/1e6:,.0f}M" if v == v else "—" for v in fin.loc[r]]
                    lines.append(f"{r}: " + " | ".join(vals))
        cf = t.cashflow
        if cf is not None and not cf.empty and "Free Cash Flow" in cf.index:
            vals = [f"{v/1e6:,.0f}M" if v == v else "—" for v in cf.loc["Free Cash Flow"]]
            lines.append("Free Cash Flow: " + " | ".join(vals))

        info = t.info
        fast = t.fast_info
        lines.append("")
        lines.append(f"Aktueller Kurs: {getattr(fast, 'last_price', None)}")
        lines.append(f"52w Hoch/Tief: {getattr(fast, 'year_high', None)} / {getattr(fast, 'year_low', None)}")
        for label, key in [("Market Cap", "marketCap"), ("P/S (ttm)", "priceToSalesTrailing12Months"),
                            ("Trailing P/E", "trailingPE"), ("Forward P/E", "forwardPE"),
                            ("EV/EBITDA", "enterpriseToEbitda"), ("Gross Margin", "grossMargins"),
                            ("Umsatzwachstum yoy", "revenueGrowth"), ("Analysten", "numberOfAnalystOpinions"),
                            ("Insider-Anteil", "heldPercentInsiders")]:
            v = info.get(key)
            if v is not None:
                lines.append(f"{label}: {v}")
    except Exception as e:
        lines.append(f"(Finanzdaten unvollständig: {e})")
    return "\n".join(lines) if lines else "nicht verfügbar"


def _parse_response(raw_text: str) -> tuple[dict, str]:
    """JSON-Block + Markdown-Report aus der Modellantwort trennen."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", raw_text, flags=re.S)
    structured = {}
    if m:
        try:
            structured = json.loads(m.group(1))
        except json.JSONDecodeError:
            logger.warning("Deep dive: JSON block unparseable")
    report_md = raw_text[m.end():].strip() if m else raw_text.strip()
    return structured, report_md


def _call_deep_dive_llm(prompt: str, data: str, config: dict) -> Optional[str]:
    """Sonnet-Call mit Websuche; Fallback ohne Tools wenn nicht verfügbar."""
    dd_cfg = config.get("deep_dive", {})
    model = dd_cfg.get("model") or config.get("screening", {}).get(
        "model_final", "claude-sonnet-4-6")
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "text", "text": data},
    ]}]

    kwargs = {"model": model, "max_tokens": 8000, "messages": messages}
    if dd_cfg.get("web_search", True):
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                            "max_uses": 6}]
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        if "tools" in kwargs:
            logger.warning(f"Deep dive mit Websuche fehlgeschlagen ({e}) — retry ohne")
            kwargs.pop("tools")
            response = client.messages.create(**kwargs)
        else:
            raise

    parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "\n".join(parts) if parts else None


def _post_deep_dive_issue(ticker: str, structured: dict, report_md: str,
                          config: dict) -> Optional[int]:
    """Report als GitHub Issue (Label: deep-dive)."""
    import requests
    token = os.environ.get("GITHUB_TOKEN")
    repo = config.get("alerts", {}).get("github_repo", "")
    if not token or not repo:
        return None
    rec = structured.get("recommendation", "?")
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github.v3+json"},
            json={"title": f"Tiefenanalyse {ticker} — Empfehlung: {rec}",
                  "body": report_md[:65000],
                  "labels": ["deep-dive", f"rec-{rec.lower()}"]},
            timeout=30)
        resp.raise_for_status()
        return resp.json().get("number")
    except Exception as e:
        logger.error(f"Deep dive issue failed for {ticker}: {e}")
        return None


def _send_deep_dive_email(ticker: str, structured: dict, report_md: str,
                          config: dict) -> bool:
    """Report per E-Mail."""
    import smtplib
    import ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT")
    if not all([sender, password, recipient]):
        return False
    prefix = config.get("alerts", {}).get("email_subject_prefix", "[Thiel Detector]")
    rec = structured.get("recommendation", "?")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{prefix} TIEFENANALYSE {ticker} — {rec}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(report_md, "plain"))
    msg.attach(MIMEText(
        "<html><body style='font-family: -apple-system, sans-serif;'>"
        f"<pre style='white-space: pre-wrap; font-size: 13px;'>{report_md}</pre>"
        "</body></html>", "html"))
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Deep dive email failed for {ticker}: {e}")
        return False


def find_deep_dive_candidates(conn, config: dict) -> list[dict]:
    """
    Kandidaten, die eine Tiefenanalyse rechtfertigen:
      - bestätigter Moat (>= min_score in >= 2 aufeinanderfolgenden Runs)
      - frisch evaluiert (diese Woche)
      - kein Deep Dive in den letzten cooldown_days
    """
    dd_cfg = config.get("deep_dive", {})
    min_score = dd_cfg.get("min_monopoly_score", 65)
    min_runs = dd_cfg.get("min_consecutive_runs", 2)
    cooldown_days = dd_cfg.get("cooldown_days", 120)
    cutoff_eval = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    cutoff_dd = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()

    rows = conn.execute("""
        SELECT cs.ticker, cs.monopoly_score, cs.consecutive_high_score_runs
        FROM company_status cs
        WHERE cs.monopoly_score >= ?
          AND cs.consecutive_high_score_runs >= ?
          AND cs.last_evaluated >= ?
          AND NOT EXISTS (
              SELECT 1 FROM deep_dives dd
              WHERE dd.ticker = cs.ticker AND dd.created_at >= ?)
        ORDER BY cs.monopoly_score DESC
    """, (min_score, min_runs, cutoff_eval, cutoff_dd)).fetchall()
    return [dict(r) for r in rows]


def run_deep_dive(conn, config: dict, ticker: str, monopoly_score: int,
                  consecutive_runs: int, dry_run: bool = False) -> dict:
    """Eine komplette Tiefenanalyse: Daten sammeln → LLM → speichern → versenden."""
    from analysis.entry_signal import compute_entry_signal
    from main import _load_snapshot_as_filing

    # Kontext sammeln
    entry = compute_entry_signal(ticker, monopoly_score, consecutive_runs)
    snapshot = _load_snapshot_as_filing(conn, ticker, max_age_days=120)
    filing = snapshot[0] if snapshot else {}

    history = conn.execute("""
        SELECT substr(evaluated_at, 1, 10) AS d, monopoly_score
        FROM evaluations WHERE ticker = ? ORDER BY evaluated_at DESC LIMIT 8
    """, (ticker,)).fetchall()
    score_history = ", ".join(f"{r['d']}: {r['monopoly_score']}" for r in history)

    last_eval = conn.execute("""
        SELECT llm_assessment FROM evaluations
        WHERE ticker = ? ORDER BY evaluated_at DESC LIMIT 1
    """, (ticker,)).fetchone()
    last_assessment = (last_eval["llm_assessment"] or "{}")[:4000] if last_eval else "{}"

    data = _DEEP_DIVE_DATA.format(
        ticker=ticker,
        monopoly_score=monopoly_score,
        consecutive_runs=consecutive_runs,
        entry_context=json.dumps(entry, ensure_ascii=False),
        score_history=score_history or "keine",
        last_assessment=last_assessment,
        business_description=(filing.get("business_description") or "")[:12000],
        risk_factors=(filing.get("risk_factors") or "")[:6000],
        mda=(filing.get("mda") or "")[:6000],
        financial_history=_build_financial_history(ticker),
    )

    if dry_run:
        logger.info(f"DRY RUN — Deep Dive {ticker} würde gestartet "
                    f"({len(data)} Zeichen Kontext)")
        return {"ticker": ticker, "dry_run": True}

    raw = _call_deep_dive_llm(_DEEP_DIVE_PROMPT, data, config)
    if not raw:
        return {"ticker": ticker, "error": "empty LLM response"}

    structured, report_md = _parse_response(raw)
    rec = structured.get("recommendation")
    if rec not in ("KAUFEN", "BEOBACHTEN", "ABLEHNEN"):
        logger.warning(f"{ticker}: Deep dive ohne valide Empfehlung ({rec!r})")
        rec = None

    issue = _post_deep_dive_issue(ticker, structured, report_md, config)
    email_sent = _send_deep_dive_email(ticker, structured, report_md, config)

    conn.execute("""
        INSERT INTO deep_dives
        (ticker, created_at, recommendation, confidence, entry_low, entry_high,
         stop_price, target_bear, target_base, target_bull, position_size_pct,
         kill_criteria, report_md, issue_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker, datetime.now(timezone.utc).isoformat(), rec,
        structured.get("confidence"),
        structured.get("entry_low"), structured.get("entry_high"),
        structured.get("stop_price"),
        structured.get("target_bear"), structured.get("target_base"),
        structured.get("target_bull"),
        structured.get("position_size_pct"),
        json.dumps(structured.get("kill_criteria", []), ensure_ascii=False),
        report_md, issue,
    ))
    conn.commit()

    logger.info(f"Deep Dive {ticker}: {rec} "
                f"(Confidence {structured.get('confidence')}, Issue #{issue})")
    return {"ticker": ticker, "recommendation": rec, "issue": issue,
            "email_sent": email_sent}


def run_deep_dives(conn, config: dict, dry_run: bool = False) -> dict:
    """Alle fälligen Tiefenanalysen dieses Laufs (gedeckelt via max_per_run)."""
    dd_cfg = config.get("deep_dive", {})
    if not dd_cfg.get("enabled", True):
        return {"enabled": False}
    if not ANTHROPIC_AVAILABLE:
        return {"error": "anthropic not available"}

    max_per_run = dd_cfg.get("max_per_run", 2)
    candidates = find_deep_dive_candidates(conn, config)
    if not candidates:
        logger.info("Deep Dive: keine fälligen Kandidaten diese Woche")
        return {"candidates": 0, "dives": []}

    logger.info(f"Deep Dive: {len(candidates)} Kandidat(en), "
                f"max {max_per_run} pro Lauf")
    results = []
    for c in candidates[:max_per_run]:
        try:
            results.append(run_deep_dive(
                conn, config, c["ticker"], c["monopoly_score"],
                c["consecutive_high_score_runs"], dry_run=dry_run))
        except Exception as e:
            logger.error(f"Deep dive failed for {c['ticker']}: {e}", exc_info=True)
            results.append({"ticker": c["ticker"], "error": str(e)})
    return {"candidates": len(candidates), "dives": results}
