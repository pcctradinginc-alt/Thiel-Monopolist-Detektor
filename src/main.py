"""
Main orchestrator for the Thiel Monopolist Detector.

Runs the full pipeline:
1. Build/refresh universe
2. Assign candidate lanes
3. LLM analysis for qualified candidates
4. Alert on new evidence
5. Store all results

Designed to be resumable — a crashed GitHub Actions job can restart
from where it left off using run_state table.
"""

import argparse
import json
import logging
import os
import sys
import uuid
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Setup paths ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from db.database import get_connection, seed_cohorts
from universe.universe_builder import build_universe
from universe.eu_universe_builder import build_eu_universe
from data.filing_collector import fetch_filing_data, fetch_eu_filing_data, compute_lane_scores as compute_lanes
from data.eu_prefilter import batch_prefilter
from analysis.llm_analyzer import analyze_company
from analysis.batch_analyzer import submit_batch, collect_batch, get_pending_batch, compute_data_quality_score
from alerts.alert_manager import process_alerts
from feedback.feedback_processor import process_feedback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(ROOT / "logs" / "run.log", mode="a") if (ROOT / "logs").exists() else logging.StreamHandler()
    ]
)
logger = logging.getLogger("main")


def load_config(config_path: str = None) -> dict:
    """Load config from YAML file."""
    if not config_path:
        config_path = ROOT / "config" / "config.yaml"
        if not Path(config_path).exists():
            config_path = ROOT / "config" / "config.example.yaml"

    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Override with environment variables
    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("Using ANTHROPIC_API_KEY from environment")

    return config


class RunStateTracker:
    """Tracks run progress for resumability."""

    def __init__(self, conn, run_id: str, total_tickers: int):
        self.conn = conn
        self.run_id = run_id
        self.total_tickers = total_tickers
        self.processed = 0
        self.llm_called = 0
        self.alerts_sent = 0
        self.tokens_input = 0
        self.tokens_output = 0

    def mark_started(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT OR REPLACE INTO run_state
            (run_id, started_at, status, tickers_total)
            VALUES (?, ?, 'RUNNING', ?)
        """, (self.run_id, now, self.total_tickers))
        self.conn.commit()

    def update(self, ticker: str, llm_used: bool = False, alert_sent: bool = False):
        self.processed += 1
        if llm_used:
            self.llm_called += 1
        if alert_sent:
            self.alerts_sent += 1
        self.conn.execute("""
            UPDATE run_state SET
                tickers_processed = ?,
                tickers_llm_called = ?,
                alerts_sent = ?,
                last_processed_ticker = ?,
                tokens_used_input = ?,
                tokens_used_output = ?
            WHERE run_id = ?
        """, (self.processed, self.llm_called, self.alerts_sent, ticker,
              self.tokens_input, self.tokens_output, self.run_id))
        self.conn.commit()

    def add_tokens(self, input_t: int, output_t: int):
        self.tokens_input += input_t
        self.tokens_output += output_t

    def mark_completed(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            UPDATE run_state SET status = 'COMPLETED', completed_at = ?
            WHERE run_id = ?
        """, (now, self.run_id))
        self.conn.commit()

    def mark_failed(self, error: str):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            UPDATE run_state SET status = 'FAILED', completed_at = ?, error_message = ?
            WHERE run_id = ?
        """, (now, error[:500], self.run_id))
        self.conn.commit()

    def get_last_processed(self) -> str:
        """For resumability: get last processed ticker from a previous run."""
        row = self.conn.execute("""
            SELECT last_processed_ticker FROM run_state
            WHERE run_id = ? AND status = 'PARTIAL'
        """, (self.run_id,)).fetchone()
        return row["last_processed_ticker"] if row else None


def get_previous_status(conn, ticker: str) -> dict:
    """Get the company's last known status for hysteresis checks."""
    row = conn.execute(
        "SELECT * FROM company_status WHERE ticker = ?", (ticker,)
    ).fetchone()
    return dict(row) if row else {}


def update_company_status(conn, ticker: str, analysis: dict, alert_outcome: dict):
    """Update the company_status table after each evaluation."""
    now = datetime.now(timezone.utc).isoformat()
    assessment = analysis.get("assessment", {})
    scores = assessment.get("scores", {})
    status = assessment.get("status", "NONE")
    alert_type = assessment.get("alert_type")

    prev = conn.execute(
        "SELECT consecutive_high_score_runs, monopoly_score FROM company_status WHERE ticker = ?",
        (ticker,)
    ).fetchone()

    monopoly_score = scores.get("monopoly_score", 0)
    consecutive = 0
    if monopoly_score >= 65:
        prev_score = (prev["monopoly_score"] if prev else 0) or 0
        prev_runs = (prev["consecutive_high_score_runs"] if prev else 0) or 0
        consecutive = prev_runs + 1 if prev_score >= 65 else 1

    # last_filing_date aus dem jüngsten Snapshot — Grundlage für den
    # Rotation-Trigger "neues Filing seit letzter Analyse"
    fs = conn.execute(
        "SELECT filing_date FROM filing_snapshots "
        "WHERE ticker = ? AND filing_date != 'unknown' "
        "ORDER BY fetched_at DESC LIMIT 1", (ticker,)
    ).fetchone()
    last_filing_date = fs["filing_date"] if fs else None

    conn.execute("""
        INSERT OR REPLACE INTO company_status
        (ticker, current_status, monopoly_score, confidence_score, data_quality_score,
         consecutive_high_score_runs, last_alert_type, last_alert_date, last_evaluated,
         is_alert_eligible, last_filing_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker,
        status,
        monopoly_score,
        scores.get("confidence_score", 0),
        scores.get("data_quality_score", 0),
        consecutive,
        alert_type if alert_outcome.get("email_sent") or alert_outcome.get("github_issue") else None,
        now if alert_outcome.get("email_sent") else None,
        now,
        1 if status in ("STRONG", "PARTIAL") else 0,
        last_filing_date,
    ))
    conn.commit()


def save_filing_snapshot(conn, ticker: str, filing_data: dict,
                          lane_data: dict) -> int | None:
    """
    Speichert Filing-Rohdaten in filing_snapshots.
    Gibt snapshot_id zurück (oder None bei Fehler).
    Idempotent: bei gleichem ticker + filing_date + source wird nichts überschrieben.
    """
    filing_date = filing_data.get("filing_date") or "unknown"
    source = filing_data.get("source_enriched") or (
        "edgar_10k" if filing_data.get("has_10k") else
        "edgar_s1"  if filing_data.get("has_s1")  else
        "eu"        if filing_data.get("source") == "eu" else
        "unknown"
    )
    biz = filing_data.get("business_description", "")
    now = datetime.now(timezone.utc).isoformat()

    try:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO filing_snapshots
            (ticker, filing_date, source, fetched_at,
             business_description, risk_factors, mda, s1_text,
             financial_signals, keyword_hits, lane_score, lane_detail,
             has_10k, has_s1, has_10q, word_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticker, filing_date, source, now,
            biz,
            filing_data.get("risk_factors", ""),
            filing_data.get("mda", ""),
            filing_data.get("s1_text", ""),
            json.dumps(filing_data.get("financial_signals", {})),
            json.dumps({
                "lock_in": filing_data.get("lock_in_keyword_hits", []),
                "camouflage": filing_data.get("camouflage_keyword_hits", []),
            }),
            lane_data.get("total_lane_score", 0),
            json.dumps(lane_data.get("lanes", {})),
            1 if filing_data.get("has_10k") else 0,
            1 if filing_data.get("has_s1") else 0,
            1 if filing_data.get("has_10q") else 0,
            len(biz.split()) if biz else 0,
        ))
        conn.commit()
        if cursor.lastrowid:
            return cursor.lastrowid
        # Zeile existierte bereits — fetched_at auffrischen, damit die
        # Snapshot-Wiederverwendung (reuse_days) nicht dauerhaft abläuft
        # und unveränderte/leere Filings wieder wöchentlich gefetcht werden
        conn.execute(
            "UPDATE filing_snapshots SET fetched_at=? "
            "WHERE ticker=? AND filing_date=? AND source=?",
            (now, ticker, filing_date, source))
        conn.commit()
        row = conn.execute(
            "SELECT id FROM filing_snapshots WHERE ticker=? AND filing_date=? AND source=?",
            (ticker, filing_date, source)
        ).fetchone()
        return row["id"] if row else None
    except Exception as e:
        logger.warning(f"{ticker}: filing_snapshot save failed: {e}")
        return None


def save_evaluation(conn, ticker: str, run_id: str, filing_data: dict,
                    lane_data: dict, analysis: dict, alert_outcome: dict,
                    snapshot_id: int = None):
    """Persist full evaluation to DB, referencing the filing snapshot."""
    now = datetime.now(timezone.utc).isoformat()
    assessment = analysis.get("assessment", {})
    scores = assessment.get("scores", {})

    conn.execute("""
        INSERT INTO evaluations
        (ticker, run_id, evaluated_at, lanes_triggered, lane_score,
         monopoly_score, confidence_score, data_quality_score,
         market_hypotheses, contradictions_detected, llm_assessment,
         alert_type, alert_sent, status,
         used_10k, used_s1, used_10q, filing_date, snapshot_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker, run_id, now,
        json.dumps(list(lane_data.get("lanes", {}).keys())),
        lane_data.get("total_lane_score", 0),
        scores.get("monopoly_score"),
        scores.get("confidence_score"),
        scores.get("data_quality_score"),
        json.dumps(analysis.get("market_hypotheses", {})),
        json.dumps(assessment.get("contradiction_analysis", {})),
        json.dumps(assessment),
        assessment.get("alert_type"),
        1 if (alert_outcome.get("email_sent") or alert_outcome.get("github_issue")) else 0,
        assessment.get("status", "NONE"),
        1 if filing_data.get("has_10k") else 0,
        1 if filing_data.get("has_s1") else 0,
        1 if filing_data.get("has_10q") else 0,
        filing_data.get("filing_date"),
        snapshot_id,
    ))
    conn.commit()


def _get_cached_lane_score(conn, ticker: str, filing_date: str = None):
    """
    Gibt gecachten Lane-Score zurück wenn das Filing seit letzter Analyse
    unverändert ist. Spart Lane-Score-Neuberechnung für stabile Unternehmen.
    Gibt None zurück wenn kein Cache oder Filing neu.
    """
    if not filing_date:
        return None
    try:
        row = conn.execute("""
            SELECT lane_score, filing_date FROM evaluations
            WHERE ticker = ? AND filing_date = ?
            ORDER BY evaluated_at DESC LIMIT 1
        """, (ticker, filing_date)).fetchone()
        if row and row["lane_score"] is not None:
            return row["lane_score"]
    except Exception:
        pass
    return None


def _prioritize_universe(universe: list[dict], conn, max_calls: int,
                          config: dict = None) -> list[dict]:
    """
    Zweistufiges Rotationssystem:

    KERN-Tickers (rotation_tier: core) → alle 2 Wochen
    ERWEITERT-Tickers (rotation_tier: extended) → alle 8 Wochen

    Innerhalb jedes Tiers:
      1. IPOs + nie analysiert (immer vorne)
      2. Hot Leads (Score ≥ 55) — wöchentlich
      3. Trigger: neues Filing seit letzter Analyse → hochpriorisiert
      4. Fällige Core-Ticker (> 2 Wochen nicht gesehen)
      5. Fällige Extended-Ticker (> 8 Wochen nicht gesehen)
      6. Rest (noch nicht fällig)

    Philosophie: breites Universe BEHALTEN, aber smarter rotieren.
    """
    from datetime import datetime, timezone, timedelta

    # Cohort → rotation_tier Mapping aus Config
    cohort_tier = {}
    if config:
        for cohort in config.get("universe", {}).get("cohorts", []):
            cohort_tier[cohort["id"]] = cohort.get("rotation_tier", "extended")

    known = {}
    rows = conn.execute(
        "SELECT ticker, last_evaluated, monopoly_score, last_filing_date "
        "FROM company_status"
    ).fetchall()
    for row in rows:
        r = dict(row)
        known[r["ticker"]] = {
            "last_evaluated": r.get("last_evaluated"),
            "monopoly_score": r.get("monopoly_score") or 0,
            "last_filing_date": r.get("last_filing_date"),
        }

    # Jüngstes Snapshot-Filing überlagert company_status.last_filing_date:
    # Letzteres wird nur bei einer Evaluation geschrieben — neue Filings
    # zwischen Evaluationen sind ausschließlich in filing_snapshots sichtbar
    # (Snapshots werden bei jedem Fetch gespeichert, auch ohne Evaluation).
    snap_rows = conn.execute(
        "SELECT ticker, filing_date, MAX(fetched_at) FROM filing_snapshots "
        "WHERE filing_date != 'unknown' GROUP BY ticker"
    ).fetchall()
    for row in snap_rows:
        t = row["ticker"]
        if t in known:
            known[t]["last_filing_date"] = row["filing_date"]

    now = datetime.now(timezone.utc)
    two_weeks_ago   = now - timedelta(days=14)
    eight_weeks_ago = now - timedelta(days=56)

    # Buckets
    always      = []   # IPOs + nie analysiert
    hot         = []   # Score ≥ 55
    triggered   = []   # neues Filing seit letzter Analyse
    core_due    = []   # Core, > 2 Wochen nicht gesehen
    ext_due     = []   # Extended, > 8 Wochen nicht gesehen
    not_due     = []   # noch nicht fällig — am Ende

    for company in universe:
        ticker    = company.get("ticker", "")
        cohort_id = company.get("cohort_id", "")
        tier      = cohort_tier.get(cohort_id, "extended")
        info      = known.get(ticker, {})
        last_eval_str    = info.get("last_evaluated")
        last_filing_str  = info.get("last_filing_date")
        score     = info.get("monopoly_score", 0)
        is_ipo           = cohort_id in ("eu_ipo", "ipo_recent") or \
                           company.get("source") == "ipo"
        is_high_conviction = cohort_id == "high_conviction"

        # Bucket 1: Immer dabei — IPOs, nie analysiert, high_conviction
        if is_ipo or is_high_conviction or not last_eval_str:
            always.append(company)
            continue

        # Bucket 2: Hot Leads
        if score >= 55:
            hot.append(company)
            continue

        # Zeitpunkt der letzten Analyse
        try:
            last_eval = datetime.fromisoformat(last_eval_str)
        except Exception:
            always.append(company)
            continue

        # Bucket 3: Trigger — neues Filing nach letzter Analyse.
        # String-Vergleich auf Datums-Präfix: filing_date ist date-only (naiv),
        # last_eval ist aware — datetime-Vergleich würde TypeError werfen.
        if last_filing_str and last_filing_str[:10] > last_eval_str[:10]:
            triggered.append(company)
            continue

        # Bucket 4+5: Fällig je nach Tier
        if tier == "core" and last_eval < two_weeks_ago:
            core_due.append(company)
        elif tier == "extended" and last_eval < eight_weeks_ago:
            ext_due.append(company)
        else:
            not_due.append(company)

    # Innerhalb jedes Buckets: älteste zuerst
    def by_last_eval(c):
        return known.get(c.get("ticker", ""), {}).get("last_evaluated", "") or "0000"

    always.sort(key=lambda c: c.get("ticker", ""))  # deterministisch
    hot.sort(key=by_last_eval)
    triggered.sort(key=by_last_eval)
    core_due.sort(key=by_last_eval)
    ext_due.sort(key=by_last_eval)
    not_due.sort(key=by_last_eval)

    prioritized = always + hot + triggered + core_due + ext_due + not_due
    result = prioritized[:max_calls]

    logger.info(
        f"Rotation — immer: {len(always)}, hot(≥55): {len(hot)}, "
        f"trigger(neues Filing): {len(triggered)}, "
        f"core fällig(>2W): {len(core_due)}, ext fällig(>8W): {len(ext_due)}, "
        f"nicht fällig: {len(not_due)} → {len(result)} ausgewählt"
    )
    return result


def _load_snapshot_as_filing(conn, ticker: str, max_age_days: int):
    """
    Frischen Filing-Snapshot aus der DB als filing_data wiederverwenden,
    statt EDGAR/yfinance neu abzufragen. 10-Ks sind jährlich — ein Snapshot
    unter ~45 Tagen ist praktisch immer aktuell. Spart Stunden Fetch-Zeit
    pro Run. Gibt (filing_data, lane_score) oder None zurück.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    row = conn.execute("""
        SELECT * FROM filing_snapshots
        WHERE ticker = ? AND fetched_at >= ?
        ORDER BY fetched_at DESC LIMIT 1
    """, (ticker, cutoff)).fetchone()
    if not row:
        return None
    r = dict(row)
    try:
        signals = json.loads(r.get("financial_signals") or "{}")
        keywords = json.loads(r.get("keyword_hits") or "{}")
    except Exception:
        signals, keywords = {}, {}
    lock_in = keywords.get("lock_in", [])
    camouflage = keywords.get("camouflage", [])
    filing_data = {
        "ticker": ticker,
        "business_description": r.get("business_description") or "",
        "risk_factors": r.get("risk_factors") or "",
        "mda": r.get("mda") or "",
        "s1_text": r.get("s1_text") or "",
        "filing_date": r.get("filing_date"),
        "financial_signals": signals,
        "lock_in_keyword_hits": lock_in,
        "camouflage_keyword_hits": camouflage,
        "keyword_count": len(lock_in),
        # Näherung aus gespeicherten Treffern: Tarnsprache UND Lock-in zugleich
        "has_contradiction_signal": bool(lock_in and camouflage),
        "has_10k": bool(r.get("has_10k")),
        "has_s1": bool(r.get("has_s1")),
        "has_10q": bool(r.get("has_10q")),
        "source_enriched": r.get("source"),
        "_from_snapshot": True,
    }
    return filing_data, (r.get("lane_score") or 0)


def _collect_batch_candidates(queue: list[dict], budget: int, conn, config: dict,
                               min_lane_score: float, privileged: set,
                               status_map: dict = None):
    """
    Holt Filings für priorisierte Kandidaten, bis das Call-Budget voll ist.
    Gibt (selected, skipped_empty) zurück; selected = Liste von (ticker, filing_data).

    Recall + Kosten:
      - Nie evaluierte Firmen passieren mit relaxtem Lane-Gate (baseline_lane_score)
        — das Keyword-Gate darf einen Monopolisten nicht dauerhaft aussperren.
      - Low-Scorer (< low_score_threshold) mit junger Bewertung werden übersprungen
        — das Budget fließt in noch nie gesehene Namen.
      - Frische Snapshots (< snapshot_reuse_days) ersetzen den Netz-Fetch.
    """
    status_map = status_map or {}
    screening_cfg = config.get("screening", {})
    baseline_gate = screening_cfg.get("baseline_lane_score", 25)
    low_score = screening_cfg.get("low_score_threshold", 40)
    low_score_days = screening_cfg.get("low_score_reeval_days", 240)
    reuse_days = screening_cfg.get("snapshot_reuse_days", 45)
    reeval_cutoff = (datetime.now(timezone.utc)
                     - timedelta(days=low_score_days)).isoformat()

    selected = []
    skipped_empty = 0
    skipped_low = 0
    reused_snapshots = 0

    for company in queue:
        ticker = company.get("ticker", "")
        if not ticker:
            continue
        if len(selected) >= budget:
            break

        is_privileged = ticker in privileged
        prev = status_map.get(ticker)

        # Kosten: frischen Snapshot wiederverwenden statt neu zu fetchen
        cached = _load_snapshot_as_filing(conn, ticker, reuse_days)
        if cached:
            filing_data, lane_score = cached
            lane_data = {"total_lane_score": lane_score, "lanes": {},
                         "_cached": True}
            reused_snapshots += 1
        else:
            if company.get("source") == "eu" or company.get("exchange"):
                filing_data = fetch_eu_filing_data(
                    ticker, company.get("name", ""), company.get("exchange", "xetra"))
            else:
                filing_data = fetch_filing_data(ticker, cik=company.get("cik"))
            lane_data = compute_lanes(filing_data, config)
            lane_score = lane_data.get("total_lane_score", 0)
            # Snapshot IMMER speichern — auch für Verworfene. Sonst werden
            # nicht selektierte Firmen jede Woche neu gefetcht und die
            # Snapshot-Wiederverwendung greift nie für die Mehrheit.
            save_filing_snapshot(conn, ticker, filing_data, lane_data)

        # Kosten: Low-Scorer mit junger Bewertung nicht erneut durchs LLM jagen.
        # Bewusst NACH dem Fetch: Nur das frische filing_date kann ein neues
        # Filing erkennen — company_status.last_filing_date wird erst bei einer
        # Evaluation geschrieben und bleibt für Übersprungene ewig alt.
        # Ein neues Filing seit der letzten Bewertung hebt den Skip auf.
        if not is_privileged and prev:
            prev_score = prev.get("monopoly_score")
            last_eval = prev.get("last_evaluated") or ""
            filing_date = filing_data.get("filing_date") or ""
            has_new_filing = bool(
                filing_date and filing_date != "unknown" and last_eval
                and filing_date[:10] > last_eval[:10])
            if prev_score is not None and prev_score < low_score \
                    and last_eval >= reeval_cutoff and not has_new_filing:
                skipped_low += 1
                continue

        biz_words = len((filing_data.get("business_description") or "").split())
        dq = compute_data_quality_score(filing_data)
        logger.debug(
            f"{ticker}: biz_words={biz_words}, dq={dq}, "
            f"lane={lane_score}, has_10k={filing_data.get('has_10k')}, "
            f"signals={len(filing_data.get('financial_signals', {}))}"
        )

        # Leere Daten vor LLM blockieren — außer high_conviction/manual_watchlist
        if not is_privileged and dq < 20:
            logger.info(f"{ticker}: data_quality={dq} < 20, no text — skipped (save API cost)")
            skipped_empty += 1
            continue

        # Recall: nie evaluierte Firmen bekommen das relaxte Baseline-Gate —
        # ein Monopolist ohne Plattform-Vokabular darf nicht ewig unsichtbar bleiben
        gate = min_lane_score if prev else min(min_lane_score, baseline_gate)

        if lane_score >= gate or is_privileged:
            selected.append((ticker, filing_data))

    if skipped_low or reused_snapshots:
        logger.info(
            f"Budget-Optimierung: {skipped_low} Low-Scorer übersprungen "
            f"(< {low_score} Punkte, < {low_score_days} Tage alt), "
            f"{reused_snapshots} Snapshots wiederverwendet (< {reuse_days} Tage)"
        )
    return selected, skipped_empty


def run_screening(config: dict, conn, run_id: str, dry_run: bool = False,
                  max_companies: int = None, resume_from: str = None):
    """Main screening loop."""
    screening_cfg = config.get("screening", {})
    max_calls = screening_cfg.get("max_calls_per_run", 150)
    min_lane_score = screening_cfg.get("min_score_for_llm_call", 20)

    # Build/refresh universe
    logger.info("Building US universe...")
    universe = build_universe(config, conn)

    # EU universe: build → yfinance pre-filter → append survivors
    if config.get("eu_universe", {}).get("enabled", False):
        logger.info("Building EU universe...")
        eu_candidates = build_eu_universe(config, conn)
        if eu_candidates:
            eu_passed, eu_rejected = batch_prefilter(
                eu_candidates,
                exchange_suffix="",  # suffix already in ticker
            )
            logger.info(
                f"EU pre-filter: {len(eu_passed)} passed, "
                f"{len(eu_rejected)} rejected — {len(eu_rejected)} Eulerpool calls saved"
            )
            universe = universe + eu_passed

    if not universe:
        logger.error("Empty universe — aborting")
        return

    # ── 4-Week Rotation ──────────────────────────────────────────────────────
    # Priority order (descending):
    #   1. Never analyzed (new companies, IPOs)
    #   2. High-scoring candidates (score ≥ 55) — re-check every week
    #   3. Oldest last_evaluated — ensures full coverage over 4 weeks
    # Result: all companies analyzed at least once/month, hot candidates weekly
    universe = _prioritize_universe(universe, conn, max_calls, config=config)
    logger.info(f"After rotation priority: {len(universe)} companies queued "
                f"(max {max_calls} LLM calls)")

    if max_companies:
        universe = universe[:max_companies]

    tracker = RunStateTracker(conn, run_id, len(universe))
    tracker.mark_started()

    llm_calls_made = 0
    skipped_resume = resume_from is not None

    try:
        for company in universe:
            ticker = company.get("ticker", "")
            if not ticker:
                continue

            # Resumability: skip until we reach the resume point
            if skipped_resume:
                if ticker == resume_from:
                    skipped_resume = False
                else:
                    continue

            logger.info(f"Processing {ticker} ({tracker.processed + 1}/{len(universe)})")

            # Check LLM call budget
            if llm_calls_made >= max_calls:
                logger.warning(f"LLM call budget ({max_calls}) reached — stopping")
                break

            # Fetch filing data — EU companies use Bundesanzeiger, US uses EDGAR
            if company.get("source") == "eu" or company.get("exchange"):
                filing_data = fetch_eu_filing_data(
                    ticker,
                    company_name=company.get("name", ""),
                    exchange=company.get("exchange", "xetra"),
                )
            else:
                filing_data = fetch_filing_data(ticker, cik=company.get("cik"))

            if filing_data.get("error") and not filing_data.get("business_description"):
                logger.warning(f"{ticker}: No usable data — skipping")
                tracker.update(ticker)
                continue

            # Lane-Score: gecachten Wert verwenden wenn Filing unverändert
            cached_lane = _get_cached_lane_score(conn, ticker,
                                                  filing_data.get("filing_date"))
            if cached_lane is not None:
                lane_score = cached_lane
                lane_data  = {"total_lane_score": lane_score, "lanes": {}, "_cached": True}
                logger.debug(f"{ticker}: Lane score from cache: {lane_score}")
            else:
                lane_data  = compute_lanes(filing_data, config)
                lane_score = lane_data.get("total_lane_score", 0)

            # Skip wenn unter Mindestscore (nicht permanent — nur dieser Run)
            if lane_score < min_lane_score:
                logger.debug(f"{ticker}: Lane score {lane_score} < {min_lane_score} — skip LLM")
                tracker.update(ticker)
                continue

            # LLM analysis
            analysis = analyze_company(ticker, filing_data, config, tracker)
            llm_calls_made += 1

            # Get previous status for hysteresis
            prev_status = get_previous_status(conn, ticker)

            # Process alerts
            alert_outcome = process_alerts(
                ticker, analysis, filing_data, prev_status, config, conn, dry_run
            )

            # Save everything
            snapshot_id = save_filing_snapshot(conn, ticker, filing_data, lane_data)
            save_evaluation(conn, ticker, run_id, filing_data, lane_data, analysis,
                            alert_outcome, snapshot_id=snapshot_id)
            update_company_status(conn, ticker, analysis, alert_outcome)

            tracker.update(
                ticker,
                llm_used=True,
                alert_sent=bool(alert_outcome.get("email_sent") or alert_outcome.get("github_issue"))
            )

            # Progress log every 10 companies
            if tracker.processed % 10 == 0:
                logger.info(
                    f"Progress: {tracker.processed}/{len(universe)} | "
                    f"LLM calls: {llm_calls_made}/{max_calls} | "
                    f"Alerts: {tracker.alerts_sent} | "
                    f"Tokens: {tracker.tokens_input:,} in / {tracker.tokens_output:,} out"
                )

        tracker.mark_completed()
        logger.info(
            f"Run complete. Processed: {tracker.processed} | "
            f"LLM calls: {llm_calls_made} | Alerts: {tracker.alerts_sent}"
        )

    except KeyboardInterrupt:
        logger.info("Run interrupted by user")
        tracker.mark_failed("KeyboardInterrupt")
    except Exception as e:
        logger.error(f"Run failed: {e}", exc_info=True)
        tracker.mark_failed(str(e))
        raise


def main():
    parser = argparse.ArgumentParser(description="Thiel Monopolist Detector")
    parser.add_argument("--mode", choices=["full", "feedback", "status", "batch_submit", "batch_collect"], default="full")
    parser.add_argument("--config", help="Path to config YAML")
    parser.add_argument("--dry-run", action="store_true", help="Don't send alerts")
    parser.add_argument("--max-companies", type=int, help="Limit for testing")
    parser.add_argument("--resume-from", help="Resume from ticker (for crashed runs)")
    parser.add_argument("--ticker", help="Analyze single ticker")
    args = parser.parse_args()

    # Create directories
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)

    config = load_config(args.config)
    conn = get_connection(config)
    seed_cohorts(conn, config.get("universe", {}).get("cohorts", []))

    run_id = str(uuid.uuid4())[:8]
    logger.info(f"Starting run {run_id} | mode={args.mode} | dry_run={args.dry_run}")

    # Fail fast: ohne API-Key sind LLM-Modi sinnlos (klarer als RetryError nach 3 Versuchen)
    if args.mode in ("full", "batch_submit", "batch_collect") and \
            not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY ist nicht gesetzt — Abbruch. "
                     "Lokal: export ANTHROPIC_API_KEY=... | CI: GitHub Secret prüfen.")
        sys.exit(1)

    if args.mode == "batch_submit":
        # Build universe, filter, collect filing data, submit as one batch
        logger.info("Building universe for batch submission...")
        us_universe = build_universe(config, conn)
        eu_universe = []
        if config.get("eu_universe", {}).get("enabled"):
            eu_universe = build_eu_universe(config, conn)

        screening_cfg = config.get("screening", {})
        min_lane_score = screening_cfg.get("min_score_for_llm_call", 40)
        max_calls = screening_cfg.get("max_calls_per_run", 150)
        manual_watchlist = screening_cfg.get("lanes", {}).get("manual_watchlist", {}).get("tickers", [])

        # high_conviction Ticker immer einschließen (ignorieren leere-Daten-Filter)
        from universe.universe_builder import _get_auto_promoted
        high_conviction = _get_auto_promoted(conn, config)
        privileged = set(high_conviction) | set(manual_watchlist)

        # EU-Quote: fester Anteil des Call-Budgets. Ohne Quote stehen EU-Titel
        # hinter 7000+ US-Titeln an und erreichen das Budget nie (0 EU-
        # Evaluationen in allen bisherigen Runs). Unbenutzte EU-Slots gehen an US.
        eu_quota = min(int(max_calls * 0.25), len(eu_universe)) if eu_universe else 0

        # Rotation auf beide Pools anwenden (3x Budget als Puffer, weil
        # dq-/Lane-Filter einen Teil der Queue verwerfen)
        eu_queue = _prioritize_universe(eu_universe, conn, eu_quota * 3, config) if eu_quota else []
        us_queue = _prioritize_universe(us_universe, conn, (max_calls - eu_quota) * 3, config)

        # Zombie-Prefilter NACH der Rotation, nur auf die Queue (~500 Titel)
        # statt aufs ganze EODHD-Universe (~3.500) — sonst frisst der Prefilter
        # allein 1-2h yfinance-Calls pro Woche
        if eu_queue:
            eu_queue, eu_rejected = batch_prefilter(eu_queue, exchange_suffix="")
            logger.info(f"EU-Prefilter (Queue): {len(eu_queue)} passed, "
                        f"{len(eu_rejected)} rejected")

        status_map = {
            r["ticker"]: dict(r) for r in conn.execute(
                "SELECT ticker, last_evaluated, monopoly_score, last_filing_date "
                "FROM company_status").fetchall()
        }

        eu_selected, eu_skipped = _collect_batch_candidates(
            eu_queue, eu_quota, conn, config, min_lane_score, privileged,
            status_map=status_map)
        us_selected, us_skipped = _collect_batch_candidates(
            us_queue, max_calls - len(eu_selected), conn, config, min_lane_score,
            privileged, status_map=status_map)
        companies_with_filings = eu_selected + us_selected
        skipped_empty = eu_skipped + us_skipped

        logger.info(
            f"Batch selection: {len(us_selected)} US + {len(eu_selected)} EU "
            f"(EU-Quote: {eu_quota})"
        )
        if skipped_empty:
            logger.info(f"Skipped {skipped_empty} companies with empty data (dq < 20)")

        batch_id = submit_batch(companies_with_filings, config, conn, run_id)
        if batch_id:
            logger.info(f"Batch submitted: {batch_id} ({len(companies_with_filings)} companies)")
            logger.info("Run batch_collect in 1-24h to retrieve results.")
        else:
            logger.error("Batch submission failed")

    elif args.mode == "batch_collect":
        # Retrieve results: priority = CLI arg > batch_id.txt file > DB lookup
        batch_id = getattr(args, "batch_id", None)
        if not batch_id:
            batch_id_file = ROOT / "data" / "batch_id.txt"
            if batch_id_file.exists():
                batch_id = batch_id_file.read_text().strip()
                logger.info(f"Using batch_id from file: {batch_id}")
        if not batch_id:
            batch_id = get_pending_batch(conn)
        if not batch_id:
            logger.error("No pending batch found. Run batch_submit first.")
        elif (row := conn.execute(
                "SELECT status FROM batch_runs WHERE batch_id = ?",
                (batch_id,)).fetchone()) and row["status"] == "collected":
            # Guard gegen Doppel-Collect (Backup-Cron): nie doppelt verarbeiten
            logger.info(f"Batch {batch_id} wurde bereits eingesammelt — nichts zu tun")
        else:
            result = collect_batch(batch_id, conn, config, dry_run=args.dry_run)
            logger.info(f"Batch collect result: {result}")
            # Wochenreport: Top-Kandidaten IMMER nennen, nicht nur bei Alerts
            if result.get("complete"):
                from alerts.weekly_report import post_weekly_report
                report_outcome = post_weekly_report(conn, config, dry_run=args.dry_run)
                logger.info(f"Weekly report: {report_outcome}")

                # Tiefenanalyse + Trade-Empfehlung für bestätigte Kandidaten
                # (selten — erwartete 2-7/Jahr — daher teuerstes Modell + Websuche)
                from analysis.deep_dive import run_deep_dives
                dd_outcome = run_deep_dives(conn, config, dry_run=args.dry_run)
                logger.info(f"Deep dives: {dd_outcome}")

    elif args.mode == "feedback":
        count = process_feedback(conn, config)
        logger.info(f"Processed {count} feedback items")

    elif args.mode == "status":
        rows = conn.execute("""
            SELECT ticker, current_status, monopoly_score, confidence_score,
                   consecutive_high_score_runs, last_alert_type, last_evaluated
            FROM company_status
            WHERE is_alert_eligible = 1
            ORDER BY monopoly_score DESC
            LIMIT 50
        """).fetchall()
        print("\n=== Current Monopoly Candidates ===")
        print(f"{'Ticker':<10} {'Status':<10} {'Mono':<6} {'Conf':<6} {'Runs':<6} {'Alert Type':<30} {'Last Eval'}")
        print("-" * 90)
        for r in rows:
            print(f"{r['ticker']:<10} {r['current_status']:<10} {r['monopoly_score']:<6} "
                  f"{r['confidence_score']:<6} {r['consecutive_high_score_runs']:<6} "
                  f"{str(r['last_alert_type']):<30} {r['last_evaluated'][:10] if r['last_evaluated'] else 'never'}")

    elif args.mode == "full":
        if args.ticker:
            # Single ticker mode for testing
            filing_data = fetch_filing_data(args.ticker)
            lane_data = compute_lanes(filing_data, config)
            analysis = analyze_company(args.ticker, filing_data, config)
            print(json.dumps(analysis, indent=2, default=str))
        else:
            run_screening(
                config, conn, run_id,
                dry_run=args.dry_run,
                max_companies=args.max_companies,
                resume_from=args.resume_from
            )


if __name__ == "__main__":
    main()
