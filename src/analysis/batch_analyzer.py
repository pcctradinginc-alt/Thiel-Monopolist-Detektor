"""
Batch analyzer — Anthropic Message Batches API for 50% cost reduction.

Instead of 3 sequential API calls per company (hypothesis → substitute → audit),
this module submits ONE combined prompt per company as a batch request.

Cost impact:
  - 50% discount on all tokens (Batch API pricing)
  - 1 API call instead of 3 (eliminates repeated context transfer)
  - Combined: ~65% cheaper than the original 3-call sequential approach

Workflow:
  batch_submit  → collect all companies, submit batch, store batch_id in DB
  batch_collect → called next run (or after delay), retrieves results, saves to DB

GitHub Actions:
  Sunday 20:00 → batch_submit
  Monday 08:00 → batch_collect + alert processing
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


# ─── Combined Single-Pass Prompt ─────────────────────────────────────────────
# Replaces the 3-step sequential prompts. The model works through all three
# analytical stages in one response, outputting a single JSON object.

_BATCH_STATIC = """\
You are a Thiel monopoly analyst. For each company you receive, work through \
three analytical stages and return a single JSON object.

Peter Thiel's framework (Zero to One):
- Real monopolists hide by claiming large competitive markets. They dominate narrow spaces.
- 4 criteria: Proprietary Technology (10x better), Network Effects, Economies of Scale, Branding
- Test: does the company have NO close substitute? Do customers stay because switching is impossible?

STAGE 1 — Market Hypothesis: What narrow market might this company actually dominate?
STAGE 2 — Substitute Gap: Is there a realistic alternative? How hard is switching?
STAGE 3 — Audit: Score each criterion with evidence AND counter-evidence. Be skeptical.

CRITICAL RULES:
- Provide counter-evidence for every criterion — a weak counter-evidence section is a failure
- Do not construct a narrative. Find the strongest case FOR and AGAINST.
- All scores 0-100. Alert only if monopoly_score > 65.

Return ONLY valid JSON matching this exact schema:

{
  "market_hypotheses": {
    "company_claimed_market": "broad market company claims",
    "narrow_market_hypotheses": [
      {
        "narrow_market": "specific narrow space",
        "why_narrow": "why this is genuinely narrow",
        "customer_pain_point": "specific problem solved better than alternatives",
        "wedge_type": "workflow|dataset|standard|api_integration|compliance|marketplace|vertical_software|infrastructure",
        "possible_substitutes": ["max 3 substitutes"],
        "confidence": "high|medium|low"
      }
    ],
    "red_flags_against_hypothesis": ["max 2 flags"]
  },
  "substitute_analysis": {
    "primary_substitute": "single most realistic alternative",
    "substitute_type": "direct_competitor|internal_tool|manual_process|legacy_system|open_source|build_own",
    "switching_cost_estimate": "high|medium|low",
    "switching_barriers": ["max 2 barriers"],
    "switching_ease_factors": ["max 2 ease factors"],
    "substitute_gap_verdict": "strong|moderate|weak|unclear"
  },
  "assessment": {
    "ticker": "<ticker>",
    "evaluation_summary": "2 sentence honest summary",
    "criteria": {
      "proprietary_technology": {"evidence": ["1 item max"], "counter_evidence": ["1 item max"], "score": 0},
      "network_effects":        {"evidence": ["1 item max"], "counter_evidence": ["1 item max"], "score": 0},
      "economies_of_scale":     {"evidence": ["1 item max"], "counter_evidence": ["1 item max"], "score": 0},
      "branding":               {"evidence": ["1 item max"], "counter_evidence": ["1 item max"], "score": 0}
    },
    "scores": {
      "monopoly_score": 0,
      "monopoly_score_reasoning": "one sentence",
      "confidence_score": 0,
      "data_quality_score": 0
    },
    "alert_type": null,
    "status": "STRONG|PARTIAL|WEAK|NONE",
    "next_verification_steps": ["max 2 steps"]
  }
}

For alert_type use ONE of these if monopoly_score > 65, else null:
HIDDEN_WEDGE_DETECTED, SUBSTITUTE_GAP_DETECTED, LOCK_IN_STRENGTHENING,
SCALE_INFLECTION, CUSTOMER_EXPANSION_SIGNAL, IPO_WITH_NARROW_DOMINANCE,
MOAT_EVIDENCE_IMPROVED, MOAT_RISK_DETECTED"""

_BATCH_DYNAMIC = """\

--- COMPANY DATA ---
Ticker: {ticker}
Business Description: {business_description}
Risk Factors: {risk_factors}
MD&A: {mda}
Financial Signals: {financial_signals}
Lock-in Keywords Found: {lock_in_keywords}
Contradiction Signal: {has_contradiction}"""


# ─── LLM Output Validation ───────────────────────────────────────────────────

# Minimales Schema für LLM-Ausgaben — verhindert stille Fehler
_REQUIRED_FIELDS = {
    ("assessment", "scores", "monopoly_score"),
    ("assessment", "scores", "confidence_score"),
    ("assessment", "scores", "data_quality_score"),
    ("assessment", "status"),
}

_VALID_ALERT_TYPES = {
    "HIDDEN_WEDGE_DETECTED", "SUBSTITUTE_GAP_DETECTED", "LOCK_IN_STRENGTHENING",
    "SCALE_INFLECTION", "CUSTOMER_EXPANSION_SIGNAL", "IPO_WITH_NARROW_DOMINANCE",
    "MOAT_EVIDENCE_IMPROVED", "MOAT_RISK_DETECTED", None,
}

_VALID_STATUSES = {"STRONG", "PARTIAL", "WEAK", "NONE", "BASELINE"}


def validate_llm_output(parsed: dict, ticker: str) -> list[str]:
    """
    Validiert LLM-Output gegen minimales Schema.
    Gibt Liste von Fehlern zurück (leer = valid).
    Wirft keine Exception — Caller entscheidet was zu tun ist.
    """
    errors = []

    # Pflichtfelder prüfen
    for path in _REQUIRED_FIELDS:
        node = parsed
        for key in path:
            if not isinstance(node, dict) or key not in node:
                errors.append(f"Missing field: {'.'.join(path)}")
                break
            node = node[key]

    # Score-Ranges
    assessment = parsed.get("assessment", {})
    scores = assessment.get("scores", {})
    for score_field in ("monopoly_score", "confidence_score", "data_quality_score"):
        val = scores.get(score_field)
        if val is not None and not (0 <= int(val) <= 100):
            errors.append(f"{score_field}={val} outside 0-100")

    # Alert-Type und Status
    alert_type = assessment.get("alert_type")
    if alert_type not in _VALID_ALERT_TYPES:
        errors.append(f"Invalid alert_type: {alert_type!r}")

    status = assessment.get("status")
    if status not in _VALID_STATUSES:
        errors.append(f"Invalid status: {status!r}")

    if errors:
        logger.warning(f"{ticker}: LLM output validation errors: {errors}")

    return errors


# ─── Data Quality Score ──────────────────────────────────────────────────────

def compute_data_quality_score(filing_data: dict) -> int:
    """
    Regelbasierter data_quality_score (0-100).
    Bewertet die Qualität der Eingabedaten — unabhängig vom LLM.

    Warum regelbasiert statt LLM: Das LLM kann die Qualität seiner eigenen
    Eingabe nicht objektiv bewerten (Interessenskonflikt). Ein LLM mit wenig
    Text schreibt sich trotzdem gute Datenlage.
    """
    score = 0

    biz  = filing_data.get("business_description", "") or ""
    risk = filing_data.get("risk_factors", "") or ""
    mda  = filing_data.get("mda", "") or ""
    sigs = filing_data.get("financial_signals", {}) or {}

    # ── Textquellen (max 55 Punkte) ──────────────────────────────────────────
    biz_words = len(biz.split())
    if biz_words >= 300:   score += 25   # vollständige Beschreibung
    elif biz_words >= 100: score += 15   # kurze Beschreibung
    elif biz_words >= 20:  score += 5    # Minimaltext

    if len(risk.split()) >= 100: score += 15   # Risk Factors vorhanden
    if len(mda.split())  >= 100: score += 15   # MD&A vorhanden

    # ── Filing-Typ (max 15 Punkte) ───────────────────────────────────────────
    if filing_data.get("has_10k"): score += 10
    if filing_data.get("has_s1"):  score += 5

    # ── Finanzsignale (max 30 Punkte) ────────────────────────────────────────
    if sigs.get("gross_margin_current") is not None: score += 10
    if sigs.get("revenue_growth_yoy")   is not None: score += 8
    if sigs.get("gross_margin_trend")   is not None: score += 7   # historischer Trend
    if sigs.get("sm_ratio_current")     is not None: score += 5

    return min(score, 100)


# ─── Batch Submit ─────────────────────────────────────────────────────────────

def build_batch_request(ticker: str, filing_data: dict, model: str) -> dict:
    """
    Build a single Anthropic batch request for one company.
    Returns a request dict ready for the batches API.
    """
    # Regelbasierter data_quality_score — dem LLM als Kontext mitgeben
    dq_score = compute_data_quality_score(filing_data)
    filing_data = {**filing_data, "_data_quality_score": dq_score}

    signals_str = json.dumps(filing_data.get("financial_signals", {}), indent=2)
    biz_desc = filing_data.get("business_description", "")[:3000]
    risk_factors = filing_data.get("risk_factors", "")[:1500]
    mda = filing_data.get("mda", "")[:1500]

    dynamic = _BATCH_DYNAMIC.format(
        ticker=ticker,
        business_description=biz_desc,
        risk_factors=risk_factors,
        mda=mda,
        financial_signals=signals_str,
        lock_in_keywords=str(filing_data.get("lock_in_keyword_hits", [])[:10]),
        has_contradiction=filing_data.get("has_contradiction_signal", False),
    )

    return {
        "custom_id": ticker,
        "params": {
            "model": model,
            "max_tokens": 4096,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _BATCH_STATIC,
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": dynamic},
                ],
            }],
        },
    }


def submit_batch(
    companies_with_filings: list[tuple[str, dict]],
    config: dict,
    conn,
    run_id: str,
) -> Optional[str]:
    """
    Submit all company analyses as a single Anthropic batch.

    Args:
        companies_with_filings: list of (ticker, filing_data) tuples
        config: app config
        conn: DB connection
        run_id: current run ID

    Returns:
        batch_id string, or None on failure
    """
    if not ANTHROPIC_AVAILABLE:
        logger.error("anthropic package not available")
        return None

    if not companies_with_filings:
        logger.warning("No companies to batch")
        return None

    client = anthropic.Anthropic()
    model = config.get("screening", {}).get("model_screening", "claude-haiku-4-5-20251001")

    requests = []
    for ticker, filing_data in companies_with_filings:
        req = build_batch_request(ticker, filing_data, model)
        requests.append(req)

    logger.info(f"Submitting batch of {len(requests)} companies (model={model})")

    try:
        batch = client.beta.messages.batches.create(requests=requests)
        batch_id = batch.id
        logger.info(f"Batch submitted: {batch_id}")

        # Persist batch_id so batch_collect can find it later
        conn.execute("""
            INSERT OR REPLACE INTO batch_runs
            (batch_id, run_id, submitted_at, status, company_count)
            VALUES (?, ?, ?, 'submitted', ?)
        """, (batch_id, run_id, datetime.now(timezone.utc).isoformat(), len(requests)))
        conn.commit()

        return batch_id

    except Exception as e:
        logger.error(f"Batch submission failed: {e}")
        return None


# ─── Batch Collect ────────────────────────────────────────────────────────────

def collect_batch(batch_id: str, conn, config: dict, dry_run: bool = False) -> dict:
    """
    Poll for batch completion and process results.

    Returns summary dict with counts of processed/failed/alerted companies.
    """
    if not ANTHROPIC_AVAILABLE:
        return {"error": "anthropic not available"}

    client = anthropic.Anthropic()

    # Check batch status
    try:
        batch = client.beta.messages.batches.retrieve(batch_id)
    except Exception as e:
        logger.error(f"Failed to retrieve batch {batch_id}: {e}")
        return {"error": str(e)}

    status = batch.processing_status
    logger.info(f"Batch {batch_id} status: {status}")

    if status != "ended":
        counts = batch.request_counts
        logger.info(
            f"Batch still processing — "
            f"succeeded={counts.succeeded} processing={counts.processing} "
            f"errored={counts.errored}"
        )
        return {"status": status, "batch_id": batch_id, "complete": False}

    # Collect results
    from alerts.alert_manager import process_alerts

    # Pre-register all batch tickers in companies table to satisfy FK constraints.
    # The collect job starts with a fresh DB (no universe build), so companies
    # may be empty. We bulk-insert all tickers before processing results.
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        # Temporarily disable FK checks for bulk pre-registration
        conn.execute("PRAGMA foreign_keys=OFF")
        for req_result in client.beta.messages.batches.results(batch_id):
            t = req_result.custom_id
            conn.execute("""
                INSERT OR IGNORE INTO companies
                (ticker, name, cohort_id, first_seen_in_universe, is_active)
                VALUES (?, ?, NULL, ?, 1)
            """, (t, t, now_iso))
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        logger.info("Pre-registered all batch tickers into companies table")
    except Exception as e:
        conn.execute("PRAGMA foreign_keys=ON")
        logger.warning(f"Ticker pre-registration failed: {e}")

    summary = {"processed": 0, "failed": 0, "alerted": 0, "batch_id": batch_id}

    try:
        for result in client.beta.messages.batches.results(batch_id):
            ticker = result.custom_id

            if result.result.type == "error":
                logger.warning(f"{ticker}: batch result error — {result.result.error}")
                summary["failed"] += 1
                continue

            # Parse the combined JSON response
            raw = result.result.message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                # Attempt to salvage truncated JSON by finding last complete object
                logger.warning(f"{ticker}: JSON parse failed ({e}) — attempting salvage")
                try:
                    # Find last valid closing brace
                    for end in range(len(raw) - 1, 0, -1):
                        if raw[end] == '}':
                            try:
                                parsed = json.loads(raw[:end + 1])
                                logger.info(f"{ticker}: JSON salvaged at char {end}")
                                break
                            except json.JSONDecodeError:
                                continue
                    else:
                        raise ValueError("Could not salvage JSON")
                except Exception:
                    logger.warning(f"{ticker}: JSON unrecoverable — skipping")
                    summary["failed"] += 1
                    continue

            # Schema-Validierung — stille Fehler verhindern
            validation_errors = validate_llm_output(parsed, ticker)
            if len(validation_errors) > 2:
                # Zu viele Fehler → Ergebnis unbrauchbar
                logger.warning(f"{ticker}: {len(validation_errors)} validation errors — skipping")
                summary["failed"] += 1
                continue

            # Reconstruct the analysis dict (same shape as analyze_company output)
            assessment = parsed.get("assessment", {})
            scores = dict(assessment.get("scores", {}))

            # Regelbasierter data_quality_score überschreibt LLM-Selbstbewertung
            filing_data_for_dq = _get_cached_filing(conn, ticker)
            if filing_data_for_dq:
                scores["data_quality_score"] = compute_data_quality_score(
                    filing_data_for_dq)
            assessment = {**assessment, "scores": scores}

            analysis = {
                "ticker": ticker,
                "market_hypotheses": parsed.get("market_hypotheses", {}),
                "substitute_analysis": parsed.get("substitute_analysis", {}),
                "assessment": assessment,
                "scores": scores,
                "alert_type": assessment.get("alert_type"),
                "status": assessment.get("status", "NONE"),
                "tokens_used": {
                    "input": result.result.message.usage.input_tokens,
                    "output": result.result.message.usage.output_tokens,
                },
            }

            # Fetch filing data from DB for alert context
            filing_data = _get_cached_filing(conn, ticker)
            prev_status = _get_prev_status(conn, ticker)

            # Sonnet-Validierung: nur bei neuem Alert + hohem Score
            # Haiku hat Kandidaten vorselektiert — Sonnet bestätigt oder verwirft
            monopoly_score = assessment.get("scores", {}).get("monopoly_score", 0)
            alert_type = assessment.get("alert_type")
            is_new_alert = alert_type and not prev_status.get("last_alert_date")

            if is_new_alert and monopoly_score >= 65 and not dry_run:
                analysis = _sonnet_validate(ticker, analysis, filing_data, config)

            alert_outcome = process_alerts(
                ticker, analysis, filing_data, prev_status, config, conn, dry_run
            )

            _save_batch_result(conn, ticker, analysis, alert_outcome)

            summary["processed"] += 1
            if alert_outcome.get("email_sent") or alert_outcome.get("github_issue"):
                summary["alerted"] += 1

        # Mark batch as collected in DB
        conn.execute("""
            UPDATE batch_runs SET status='collected', collected_at=?
            WHERE batch_id=?
        """, (datetime.now(timezone.utc).isoformat(), batch_id))
        conn.commit()

        logger.info(
            f"Batch {batch_id} collected: "
            f"processed={summary['processed']} failed={summary['failed']} "
            f"alerted={summary['alerted']}"
        )
        return {**summary, "status": "collected", "complete": True}

    except Exception as e:
        logger.error(f"Batch collection failed: {e}", exc_info=True)
        return {"error": str(e), "batch_id": batch_id}


# ─── Sonnet Validation ───────────────────────────────────────────────────────

def _sonnet_validate(ticker: str, haiku_analysis: dict, filing_data: dict,
                     config: dict) -> dict:
    """
    Sonnet-Validierung für neue Alerts mit monopoly_score >= 65.

    Haiku hat schnell vorselektiert — Sonnet prüft kritisch ob der Moat real ist.
    Kosten: nur ~15-20 Sonnet-Calls/Woche (nur echte Alert-Kandidaten).

    Gibt aktualisierte analysis zurück. Bei Widerspruch: Sonnet überschreibt Haiku.
    Bei API-Fehler: Haiku-Ergebnis bleibt unverändert (fail-safe).
    """
    import anthropic

    model = config.get("screening", {}).get("model_final", "claude-sonnet-4-6")
    client = anthropic.Anthropic()

    biz_desc    = filing_data.get("business_description", "")[:3000]
    haiku_score = haiku_analysis.get("scores", {}).get("monopoly_score", 0)
    haiku_type  = haiku_analysis.get("alert_type", "")
    haiku_summary = ""
    if haiku_analysis.get("assessment"):
        haiku_summary = str(haiku_analysis["assessment"].get(
            "evaluation_summary", ""))[:500]

    prompt = f"""Du bist ein skeptischer Investment-Analyst der Thiel-artige Monopole sucht.
Haiku hat folgendes Unternehmen als potenziellen Moat-Kandidaten identifiziert:

Ticker: {ticker}
Haiku Monopoly Score: {haiku_score}/100
Haiku Alert Type: {haiku_type}
Haiku Summary: {haiku_summary}

Business Description:
{biz_desc}

Deine Aufgabe: Kritische Überprüfung. Ist dieser Moat WIRKLICH real und dauerhaft?

Häufige Haiku-Fehler: überschätzte Netzwerkeffekte, verwechselt Marktführer mit Monopolist,
ignorierten Wettbewerb, temporäre Kundenkonzentration als strukturellen Moat dargestellt.

Antworte NUR mit JSON:
{{"confirmed": true/false, "sonnet_monopoly_score": 0-100, "reason": "1 Satz", "alert_type": "{haiku_type} oder null"}}"""

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        # JSON extrahieren
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        parsed = json.loads(raw[start:end])

        confirmed   = parsed.get("confirmed", True)
        new_score   = parsed.get("sonnet_monopoly_score", haiku_score)
        reason      = parsed.get("reason", "")
        new_alert   = parsed.get("alert_type")

        logger.info(
            f"{ticker}: Sonnet validation — confirmed={confirmed}, "
            f"score {haiku_score}→{new_score}, reason: {reason}"
        )

        # Sonnet überschreibt Haiku bei Widerspruch
        updated = dict(haiku_analysis)
        updated["sonnet_validated"] = True
        updated["sonnet_confirmed"] = confirmed
        if not confirmed:
            updated["alert_type"] = None          # Alert zurückgezogen
            updated["status"]     = "HAIKU_ONLY"
        if new_score != haiku_score:
            scores = dict(updated.get("scores", {}))
            scores["monopoly_score"]          = new_score
            scores["monopoly_score_haiku"]    = haiku_score  # Haiku-Wert erhalten
            updated["scores"] = scores
        if new_alert != haiku_type:
            updated["alert_type"] = new_alert

        return updated

    except Exception as e:
        logger.warning(f"{ticker}: Sonnet validation failed ({e}) — keeping Haiku result")
        return haiku_analysis  # fail-safe: Haiku-Ergebnis bleibt


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_cached_filing(conn, ticker: str) -> dict:
    """Retrieve last known filing data for a ticker (for alert context)."""
    row = conn.execute("""
        SELECT llm_assessment FROM evaluations
        WHERE ticker = ? ORDER BY evaluated_at DESC LIMIT 1
    """, (ticker,)).fetchone()
    if row and row["llm_assessment"]:
        try:
            return json.loads(row["llm_assessment"])
        except Exception:
            pass
    return {"ticker": ticker}


def _get_prev_status(conn, ticker: str) -> dict:
    """Get previous company status for hysteresis checks."""
    row = conn.execute(
        "SELECT * FROM company_status WHERE ticker = ?", (ticker,)
    ).fetchone()
    return dict(row) if row else {}


def _save_batch_result(conn, ticker: str, analysis: dict, alert_outcome: dict):
    """Persist batch result to evaluations + company_status tables."""
    from main import save_evaluation, update_company_status
    now = datetime.now(timezone.utc).isoformat()
    run_id = conn.execute(
        "SELECT run_id FROM batch_runs ORDER BY submitted_at DESC LIMIT 1"
    ).fetchone()
    run_id = run_id["run_id"] if run_id else "batch"

    # Ensure ticker exists in companies table (FK requirement)
    existing = conn.execute("SELECT ticker FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO companies (ticker, name, cohort_id, first_seen_in_universe, is_active)
            VALUES (?, ?, 'batch', ?, 1)
        """, (ticker, ticker, now))
        conn.commit()

    save_evaluation(conn, ticker, run_id, {}, {}, analysis, alert_outcome)
    update_company_status(conn, ticker, analysis, alert_outcome)


def get_pending_batch(conn) -> Optional[str]:
    """Return the most recent submitted-but-not-collected batch_id, if any."""
    row = conn.execute("""
        SELECT batch_id FROM batch_runs
        WHERE status = 'submitted'
        ORDER BY submitted_at DESC LIMIT 1
    """).fetchone()
    return row["batch_id"] if row else None
