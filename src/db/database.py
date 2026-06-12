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

-- Batch API runs (submitted async, collected next day)
CREATE TABLE IF NOT EXISTS batch_runs (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    collected_at TEXT,
    status TEXT CHECK(status IN ('submitted', 'collected', 'failed')) DEFAULT 'submitted',
    company_count INTEGER DEFAULT 0
);

-- Filing snapshots: rohe Texte + Finanzdaten pro Filing-Datum
-- Evaluations referenzieren snapshots — Rekonstruktion ohne LLM-Output möglich
CREATE TABLE IF NOT EXISTS filing_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    source TEXT NOT NULL,              -- 'edgar_10k', 'edgar_s1', 'bundesanzeiger', 'eodhd'
    fetched_at TEXT NOT NULL,

    -- Rohtexte (nicht vom LLM verändert)
    business_description TEXT,
    risk_factors TEXT,
    mda TEXT,
    s1_text TEXT,

    -- Finanzsignale (regelbasiert, nicht LLM)
    financial_signals TEXT,            -- JSON: gross_margin, revenue_growth, sm_ratio etc.
    keyword_hits TEXT,                 -- JSON: lock_in_keyword_hits, camouflage_keyword_hits
    lane_score REAL,
    lane_detail TEXT,                  -- JSON: {lane_name: score}

    -- Metadaten
    has_10k INTEGER DEFAULT 0,
    has_s1 INTEGER DEFAULT 0,
    has_10q INTEGER DEFAULT 0,
    word_count INTEGER DEFAULT 0,

    FOREIGN KEY (ticker) REFERENCES companies(ticker),
    UNIQUE (ticker, filing_date, source)
);

-- Evaluations referenzieren den Filing-Snapshot der analysiert wurde
-- (snapshot_id kann NULL sein für alte Evaluations ohne Snapshot)
-- Migrationsschritt: ALTER TABLE evaluations ADD COLUMN snapshot_id INTEGER
--   REFERENCES filing_snapshots(id);
-- Wird beim ersten Start mit neuer DB automatisch gesetzt.

-- Indexes
-- Signals: jedes Alert-Event mit Marktdaten zum Zeitpunkt des Signals.
-- Zweck: saubere Grundlage für späteres Backtesting (ab ~20+ Signalen sinnvoll).
-- Manuell befüllt via decision_status + human_reason.
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    signal_date TEXT NOT NULL,          -- ISO8601, UTC

    -- Scores zum Signalzeitpunkt (regelbasiert, nicht LLM)
    monopoly_score INTEGER,
    confidence_score INTEGER,
    data_quality_score INTEGER,
    primary_lane TEXT,                  -- z.B. "hidden_wedge"
    alert_type TEXT,
    score_delta INTEGER,                -- Differenz zum letzten Signal (NULL = erstes Signal)

    -- Marktdaten zum Signalzeitpunkt (yfinance-Snapshot)
    price_at_signal REAL,
    market_cap_m REAL,
    avg_volume_30d REAL,

    -- Manuelle Entscheidung (nie automatisch)
    decision_status TEXT CHECK(decision_status IN
        ('WATCH', 'CANDIDATE', 'BOUGHT', 'REJECTED', 'CLOSED')) DEFAULT 'WATCH',
    human_reason TEXT,                  -- Freitext: warum BOUGHT/REJECTED

    -- Referenz zur Evaluation die das Signal ausgelöst hat
    evaluation_id INTEGER REFERENCES evaluations(id),

    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

-- Trades: manuelles Journal. Kein Broker-Anschluss, keine Automatisierung.
-- Zweck: Thesis dokumentieren, P&L verfolgen, Post-Mortem lernen.
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),

    -- Entry
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    position_size_pct REAL,             -- % des Portfolios
    stop_price REAL,                    -- initialer Stop-Loss
    thesis TEXT,                        -- kurze Begründung

    -- Exit (NULL solange offen)
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,                   -- z.B. "stop hit", "thesis invalidated", "target reached"
    post_mortem TEXT,                   -- was hat man gelernt

    -- Performance (berechnet beim Eintragen des Exits)
    pnl_pct REAL,
    max_drawdown_pct REAL,
    max_gain_pct REAL,

    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_evaluations_ticker ON evaluations(ticker);
CREATE INDEX IF NOT EXISTS idx_evaluations_run ON evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_status ON evaluations(status);
CREATE INDEX IF NOT EXISTS idx_evaluations_monopoly_score ON evaluations(monopoly_score);
CREATE INDEX IF NOT EXISTS idx_feedback_ticker ON human_feedback(ticker);
CREATE INDEX IF NOT EXISTS idx_feedback_verdict ON human_feedback(verdict);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(signal_date);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(decision_status);

-- Deep Dives: automatische Tiefenanalysen für bestätigte Kandidaten
-- (~2-7/Jahr). Empfehlung ist Entscheidungsvorlage — Ausführung bleibt manuell.
CREATE TABLE IF NOT EXISTS deep_dives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,

    recommendation TEXT CHECK(recommendation IN ('KAUFEN', 'BEOBACHTEN', 'ABLEHNEN')),
    confidence INTEGER,                 -- 0-100
    entry_low REAL,                     -- Einstiegszone
    entry_high REAL,
    stop_price REAL,                    -- Kill-Level
    target_bear REAL,                   -- Kursziele je Szenario
    target_base REAL,
    target_bull REAL,
    position_size_pct REAL,             -- Vorschlag % des Portfolios

    kill_criteria TEXT,                 -- JSON: was die These widerlegen würde
    report_md TEXT,                     -- voller Markdown-Report
    issue_number INTEGER,               -- GitHub Issue

    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

CREATE INDEX IF NOT EXISTS idx_deep_dives_ticker ON deep_dives(ticker);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with full schema. Runs migrations on existing DBs."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _run_migrations(conn)
    conn.commit()
    logger.info(f"Database initialized at {db_path}")
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    Inkrementelle Schema-Migrationen für bestehende Datenbanken.
    Jede Migration ist idempotent (IF NOT EXISTS / try/except).
    """
    migrations = [
        # Migration 001: snapshot_id auf evaluations
        "ALTER TABLE evaluations ADD COLUMN snapshot_id INTEGER REFERENCES filing_snapshots(id)",
        # Migration 002: last_filing_date auf company_status (für Filing-Trigger in Rotation)
        "ALTER TABLE company_status ADD COLUMN last_filing_date TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            logger.debug(f"Migration applied: {sql[:60]}...")
        except Exception:
            pass  # Spalte existiert bereits — normal bei bestehenden DBs


def get_connection(config: dict) -> sqlite3.Connection:
    """Get database connection. Currently SQLite only."""
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
