"""
Database initialization and schema management.
Supports SQLite (MVP) and Turso (production).
"""

import sqlite3
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
-- Universe cohorts for controlled expansion
CREATE TABLE IF NOT EXISTS universe_cohorts (
    cohort_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sector_filter TEXT,
    added_at TEXT NOT NULL,
    baseline_started_at TEXT,
    baseline_completed_at TEXT,
    alerting_enabled INTEGER DEFAULT 0,
    baseline_runs_completed INTEGER DEFAULT 0,
    min_baseline_runs INTEGER DEFAULT 2
);

-- All companies in the universe
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    cohort_id TEXT,
    cik TEXT,
    sic_code TEXT,
    exchange TEXT,
    market_cap_m REAL,
    first_seen_in_universe TEXT,
    first_evaluated TEXT,
    last_evaluated TEXT,
    alert_eligible_from TEXT,
    is_active INTEGER DEFAULT 1,
    FOREIGN KEY (cohort_id) REFERENCES universe_cohorts(cohort_id)
);

-- Each evaluation run result per company
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    run_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    
    -- Lane assignment
    lanes_triggered TEXT,  -- JSON array of lane names
    lane_score REAL,       -- Composite lane priority score
    
    -- Three scores
    monopoly_score INTEGER,       -- 0-100
    confidence_score INTEGER,     -- 0-100
    data_quality_score INTEGER,   -- 0-100
    
    -- Market hypothesis (LLM Step 1 output)
    market_hypotheses TEXT,       -- JSON
    
    -- Contradiction detection
    contradictions_detected TEXT, -- JSON
    
    -- Final LLM assessment (JSON with evidence + counter-evidence)
    llm_assessment TEXT,          -- Full JSON output
    
    -- Alert type if triggered
    alert_type TEXT,
    alert_sent INTEGER DEFAULT 0,
    
    -- Status
    status TEXT CHECK(status IN ('STRONG', 'PARTIAL', 'WEAK', 'NONE', 'BASELINE')),
    previous_status TEXT,
    
    -- Data sources used
    used_10k INTEGER DEFAULT 0,
    used_s1 INTEGER DEFAULT 0,
    used_10q INTEGER DEFAULT 0,
    used_yfinance INTEGER DEFAULT 0,
    filing_date TEXT,
    
    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

-- Current status view (latest evaluation per company)
CREATE TABLE IF NOT EXISTS company_status (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    current_status TEXT,
    monopoly_score INTEGER,
    confidence_score INTEGER,
    data_quality_score INTEGER,
    consecutive_high_score_runs INTEGER DEFAULT 0,
    last_alert_type TEXT,
    last_alert_date TEXT,
    last_evaluated TEXT,
    is_alert_eligible INTEGER DEFAULT 0,
    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

-- Human feedback from GitHub Issues
CREATE TABLE IF NOT EXISTS human_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    evaluation_id INTEGER,
    feedback_date TEXT NOT NULL,
    verdict TEXT CHECK(verdict IN ('CONFIRMED', 'REJECTED', 'WATCHLIST', 'TOO_EARLY')),
    corrected_status TEXT,
    notes TEXT,
    strongest_wrong_assumption TEXT,
    weakest_missing_evidence TEXT,
    github_issue_number INTEGER,
    FOREIGN KEY (ticker) REFERENCES companies(ticker),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

-- Calibration events (prompt adjustments based on feedback)
CREATE TABLE IF NOT EXISTS calibration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    criterion TEXT,
    change_description TEXT,
    triggered_by_feedback_count INTEGER,
    notes TEXT
);

-- Run state for resumable jobs
CREATE TABLE IF NOT EXISTS run_state (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    status TEXT CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'PARTIAL')),
    tickers_total INTEGER DEFAULT 0,
    tickers_processed INTEGER DEFAULT 0,
    tickers_llm_called INTEGER DEFAULT 0,
    alerts_sent INTEGER DEFAULT 0,
    last_processed_ticker TEXT,
    completed_at TEXT,
    error_message TEXT,
    tokens_used_input INTEGER DEFAULT 0,
    tokens_used_output INTEGER DEFAULT 0
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_evaluations_ticker ON evaluations(ticker);
CREATE INDEX IF NOT EXISTS idx_evaluations_run ON evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_status ON evaluations(status);
CREATE INDEX IF NOT EXISTS idx_evaluations_monopoly_score ON evaluations(monopoly_score);
CREATE INDEX IF NOT EXISTS idx_feedback_ticker ON human_feedback(ticker);
CREATE INDEX IF NOT EXISTS idx_feedback_verdict ON human_feedback(verdict);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with full schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.info(f"Database initialized at {db_path}")
    return conn


def get_connection(config: dict) -> sqlite3.Connection:
    """Get database connection based on config mode."""
    mode = config.get("persistence", {}).get("mode", "sqlite")

    if mode == "turso":
        # Turso via libsql — falls back to SQLite if env vars not set
        turso_url = os.environ.get("TURSO_URL")
        turso_token = os.environ.get("TURSO_TOKEN")
        if turso_url and turso_token:
            try:
                import libsql_client
                # Note: libsql_client returns an async client
                # For sync usage, we fall back to SQLite locally
                logger.info("Turso configured — using local SQLite as sync fallback")
            except ImportError:
                logger.warning("libsql_client not installed, using SQLite")

    # Default: SQLite
    db_path = config.get("persistence", {}).get("sqlite_path", "data/thiel_detector.db")
    return init_db(db_path)


def seed_cohorts(conn: sqlite3.Connection, cohorts: list):
    """Seed initial cohorts from config if not already present."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    for cohort in cohorts:
        existing = conn.execute(
            "SELECT cohort_id FROM universe_cohorts WHERE cohort_id = ?",
            (cohort["id"],)
        ).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO universe_cohorts
                (cohort_id, name, sector_filter, added_at, alerting_enabled)
                VALUES (?, ?, ?, ?, ?)
            """, (
                cohort["id"],
                cohort["name"],
                str(cohort.get("sic_codes", [])),
                now,
                1 if cohort.get("alerting_enabled", False) else 0
            ))
            logger.info(f"Seeded cohort: {cohort['id']}")

    conn.commit()
