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
from datetime import datetime, timezone
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
    if prev:
        prev_score = prev["monopoly_score"] or 0
        if monopoly_score >= 65 and prev_score >= 65:
            consecutive = (prev["consecutive_high_score_runs"] or 0) + 1
        else:
            consecutive = 1 if monopoly_score >= 65 else 0

    conn.execute("""
        INSERT OR REPLACE INTO company_status
        (ticker, current_status, monopoly_score, confidence_score, data_quality_score,
         consecutive_high_score_runs, last_alert_type, last_alert_date, last_evaluated,
         is_alert_eligible)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        1 if status in ("STRONG", "PARTIAL") else 0
    ))
    conn.commit()


def save_evaluation(conn, ticker: str, run_id: str, filing_data: dict,
                    lane_data: dict, analysis: dict, alert_outcome: dict):
    """Persist full evaluation to DB."""
    now = datetime.now(timezone.utc).isoformat()
    assessment = analysis.get("assessment", {})
    scores = assessment.get("scores", {})

    conn.execute("""
        INSERT INTO evaluations
        (ticker, run_id, evaluated_at, lanes_triggered, lane_score,
         monopoly_score, confidence_score, data_quality_score,
         market_hypotheses, contradictions_detected, llm_assessment,
         alert_type, alert_sent, status,
         used_10k, used_s1, used_10q, filing_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        filing_data.get("filing_date")
    ))
    conn.commit()


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

            # Compute lane scores
            lane_data = compute_lanes(filing_data, config)
            lane_score = lane_data.get("total_lane_score", 0)

            # Skip if below minimum lane score (not excluded permanently — just this run)
            if lane_score < min_lane_score and ticker not in (
                config.get("screening", {}).get("lanes", {})
                    .get("manual_watchlist", {}).get("tickers", [])
            ):
                logger.debug(f"{ticker}: Lane score {lane_score} < {min_lane_score} — skip LLM this run")
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
            save_evaluation(conn, ticker, run_id, filing_data, lane_data, analysis, alert_outcome)
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
    parser.add_argument("--mode", choices=["full", "feedback", "status"], default="full")
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

    if args.mode == "feedback":
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
