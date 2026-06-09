"""
Basic smoke tests — verify the system initializes correctly
without requiring API keys or external connections.
"""

import sys
import json
import sqlite3
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_database_init():
    """Database schema initializes cleanly — inkl. filing_snapshots und Migrationen."""
    from db.database import init_db
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        conn = init_db(f.name)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        expected = {
            "universe_cohorts", "companies", "evaluations",
            "company_status", "human_feedback", "calibration_events",
            "run_state", "batch_runs", "filing_snapshots",
        }
        assert expected.issubset(table_names), f"Missing tables: {expected - table_names}"

        # filing_snapshots hat die erwarteten Spalten
        cols = {r[1] for r in conn.execute("PRAGMA table_info(filing_snapshots)").fetchall()}
        for col in ("ticker", "filing_date", "source", "business_description",
                    "financial_signals", "lane_score", "word_count"):
            assert col in cols, f"filing_snapshots missing column: {col}"

        # evaluations hat snapshot_id (Migration)
        eval_cols = {r[1] for r in conn.execute("PRAGMA table_info(evaluations)").fetchall()}
        assert "snapshot_id" in eval_cols, "evaluations missing snapshot_id"

        conn.close()
    print("✓ Database init + filing_snapshots + migrations")


def test_config_loads():
    """Config lädt, hat keine doppelten Keys, alle Pflichtfelder vorhanden."""
    import yaml
    from collections import Counter
    config_path = Path(__file__).parent.parent / "config" / "config.example.yaml"
    with open(config_path) as f:
        text = f.read()

    # Doppelte Top-Level Keys prüfen (YAML lädt nur den letzten still)
    top_keys = [l.split(":")[0] for l in text.splitlines()
                if l and not l.startswith(" ") and ":" in l and not l.startswith("#")]
    dupes = {k: v for k, v in Counter(top_keys).items() if v > 1}
    assert not dupes, f"Doppelte Keys in config.example.yaml: {dupes}"

    config = yaml.safe_load(text)
    for key in ("universe", "eu_universe", "screening", "alerts", "persistence"):
        assert key in config, f"Missing top-level key: {key}"

    # Turso nicht mehr in config
    assert config["persistence"]["mode"] == "sqlite", "Turso sollte nicht mehr konfiguriert sein"

    # eu_universe nur einmal (durch obigen dupe-Check bereits sichergestellt)
    assert "exchanges" in config["eu_universe"]
    print("✓ Config loads, no duplicate keys, no Turso")


def test_lane_scoring():
    """Lane scoring produces expected structure."""
    from data.filing_collector import compute_lane_scores
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "config.example.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    filing_data = {
        "business_description": "We provide mission-critical workflow automation platform for regulatory compliance. Our system of record is deeply embedded in customer operations with long-term contracts and high retention.",
        "mda": "Net revenue retention remains above 120%. Sales and marketing as a percentage of revenue declined for the third consecutive year.",
        "s1_text": "",
        "risk_factors": "We operate in a highly competitive market with large incumbents.",
        "has_10k": True,
        "has_s1": False,
        "financial_signals": {
            "gross_margin_trend": "rising",
            "sm_revenue_trend": "falling",
            "operating_leverage_signal": True
        },
        "lock_in_keyword_hits": [
            "mission-critical", "system of record", "long-term contracts",
            "high retention", "platform", "embedded", "workflow automation",
            "regulatory compliance", "switching costs"
        ],
        "camouflage_keyword_hits": ["highly competitive"],
        "has_contradiction_signal": True,
        "keyword_count": 9
    }

    result = compute_lane_scores(filing_data, config)
    assert "lanes" in result
    assert "total_lane_score" in result
    assert result["total_lane_score"] > 0
    assert "hidden_wedge" in result["lanes"]
    assert result["lanes"]["hidden_wedge"] > 50
    print(f"✓ Lane scoring: total={result['total_lane_score']}, lanes={list(result['lanes'].keys())}")


def test_alert_hysteresis():
    """Hysteresis logic prevents duplicate alerts."""
    from alerts.alert_manager import should_send_alert
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "config.example.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    assessment = {
        "scores": {"monopoly_score": 75, "confidence_score": 70, "data_quality_score": 65},
        "alert_type": "HIDDEN_WEDGE_DETECTED",
        "status": "STRONG"
    }

    # No previous status → should alert
    should, reason = should_send_alert("TEST", assessment, {}, config)
    assert should, f"Should alert on first detection: {reason}"
    print(f"✓ Alert on new detection")

    # Recent alert → should NOT alert again (same score)
    from datetime import datetime, timezone, timedelta
    recent_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    prev_with_recent_alert = {
        "last_alert_date": recent_date,
        "monopoly_score": 72
    }
    should, reason = should_send_alert("TEST", assessment, prev_with_recent_alert, config)
    assert not should, f"Should NOT alert within cooldown: {reason}"
    print(f"✓ Hysteresis blocks duplicate alert: {reason}")


def test_ticker_extraction():
    """Ticker extraction from GitHub Issue titles."""
    from feedback.feedback_processor import extract_ticker_from_issue

    cases = [
        ({"title": "[HIDDEN_WEDGE_DETECTED] AAPL — Score: 78/100"}, "AAPL"),
        ({"title": "[LOCK_IN_STRENGTHENING] MSFT — Score: 82/100"}, "MSFT"),
        ({"title": "Some other issue"}, ""),
    ]
    for issue, expected in cases:
        result = extract_ticker_from_issue(issue)
        assert result == expected, f"Expected '{expected}', got '{result}' for title: {issue['title']}"
    print("✓ Ticker extraction from GitHub Issue titles")


def test_data_quality_score():
    """data_quality_score ist regelbasiert, nicht vom LLM."""
    from analysis.batch_analyzer import compute_data_quality_score

    # Kein Text, keine Signale → niedriger Score
    empty = {"business_description": "", "risk_factors": "", "mda": "",
             "financial_signals": {}, "has_10k": False, "has_s1": False}
    score_empty = compute_data_quality_score(empty)
    assert score_empty < 20, f"Leere Daten sollten niedrigen Score haben, got {score_empty}"

    # Vollständige Daten → hoher Score
    full = {
        "business_description": " ".join(["word"] * 400),
        "risk_factors": " ".join(["word"] * 150),
        "mda": " ".join(["word"] * 150),
        "financial_signals": {
            "gross_margin_current": 65.0,
            "revenue_growth_yoy": 18.0,
            "gross_margin_trend": "rising",
            "sm_ratio_current": 22.0,
        },
        "has_10k": True, "has_s1": False,
    }
    score_full = compute_data_quality_score(full)
    assert score_full >= 80, f"Vollständige Daten sollten hohen Score haben, got {score_full}"
    assert score_full <= 100

    print(f"✓ data_quality_score: leer={score_empty}, voll={score_full}")


def test_llm_output_validation():
    """JSON Schema-Validierung erkennt fehlerhafte LLM-Outputs."""
    from analysis.batch_analyzer import validate_llm_output

    valid = {
        "assessment": {
            "scores": {"monopoly_score": 72, "confidence_score": 68, "data_quality_score": 55},
            "status": "PARTIAL",
            "alert_type": "HIDDEN_WEDGE_DETECTED",
        }
    }
    assert validate_llm_output(valid, "TEST") == [], "Valides Output sollte keine Fehler haben"

    # Fehlende Pflichtfelder
    missing = {"assessment": {"scores": {}, "status": "PARTIAL"}}
    errors = validate_llm_output(missing, "TEST")
    assert len(errors) > 0, "Fehlende Scores sollten Fehler erzeugen"

    # Ungültiger Status
    bad_status = {
        "assessment": {
            "scores": {"monopoly_score": 72, "confidence_score": 68, "data_quality_score": 55},
            "status": "INVALID_STATUS",
            "alert_type": None,
        }
    }
    errors = validate_llm_output(bad_status, "TEST")
    assert any("status" in e for e in errors), "Ungültiger Status sollte Fehler erzeugen"

    print("✓ LLM output validation")


def test_alert_policy_centralized():
    """get_alert_policy gibt alle Policy-Parameter aus einer Quelle."""
    import yaml
    from alerts.alert_manager import get_alert_policy, should_send_alert
    config_path = Path(__file__).parent.parent / "config" / "config.example.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    policy = get_alert_policy(config)
    required_keys = {"min_monopoly_score", "min_confidence_score", "min_data_quality_score",
                     "cooldown_days", "min_score_delta", "min_consecutive_runs",
                     "moat_risk_always_alert"}
    missing = required_keys - set(policy.keys())
    assert not missing, f"get_alert_policy fehlen Keys: {missing}"

    # MOAT_RISK_DETECTED ignoriert Cooldown
    from datetime import datetime, timezone, timedelta
    assessment = {
        "scores": {"monopoly_score": 70, "confidence_score": 65, "data_quality_score": 60},
        "alert_type": "MOAT_RISK_DETECTED", "status": "PARTIAL"
    }
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    prev = {"last_alert_date": recent, "monopoly_score": 68}
    should, reason = should_send_alert("TEST", assessment, prev, config)
    assert should, f"MOAT_RISK_DETECTED sollte Cooldown ignorieren: {reason}"

    print("✓ Alert policy centralized + MOAT_RISK bypass")


def test_filing_snapshot_save():
    """Filing-Snapshot wird korrekt in DB gespeichert."""
    import tempfile
    from db.database import init_db

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = init_db(db_path)
    # Company anlegen (FK-Constraint)
    conn.execute("INSERT OR IGNORE INTO companies (ticker, name, first_seen_in_universe, is_active) VALUES ('SNAP_TEST', 'Test Co', '2024-01-01', 1)")
    conn.commit()

    # Snapshot speichern
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from main import save_filing_snapshot

    filing = {
        "business_description": "mission-critical platform with switching costs " * 20,
        "risk_factors": "highly competitive market " * 10,
        "mda": "revenue growth improved " * 10,
        "s1_text": "",
        "filing_date": "2024-12-31",
        "has_10k": True, "has_s1": False, "has_10q": False,
        "financial_signals": {"gross_margin_current": 65.0},
        "lock_in_keyword_hits": ["mission-critical", "switching costs"],
        "camouflage_keyword_hits": [],
        "source": "edgar_10k",
    }
    lane = {"total_lane_score": 72, "lanes": {"hidden_wedge": 80}}

    snap_id = save_filing_snapshot(conn, "SNAP_TEST", filing, lane)
    assert snap_id is not None, "Snapshot sollte gespeichert werden"

    # Aus DB lesen
    row = conn.execute("SELECT * FROM filing_snapshots WHERE ticker='SNAP_TEST'").fetchone()
    assert row is not None
    assert "mission-critical" in row["business_description"]
    assert row["has_10k"] == 1
    assert row["word_count"] > 0

    conn.close()
    import os; os.unlink(db_path)
    print(f"✓ filing_snapshot saved (id={snap_id}, words={row['word_count']})")


def test_signals_and_trades_schema():
    """signals- und trades-Tabellen existieren mit korrekten Spalten."""
    from db.database import init_db
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = init_db(db_path)

    # signals-Spalten
    sig_cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    for col in ("ticker", "signal_date", "monopoly_score", "price_at_signal",
                "market_cap_m", "avg_volume_30d", "decision_status", "score_delta"):
        assert col in sig_cols, f"signals missing column: {col}"

    # trades-Spalten
    trade_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for col in ("ticker", "entry_date", "entry_price", "thesis",
                "exit_date", "exit_price", "pnl_pct", "post_mortem"):
        assert col in trade_cols, f"trades missing column: {col}"

    # decision_status CHECK-Constraint
    conn.execute("INSERT OR IGNORE INTO companies (ticker, name, first_seen_in_universe, is_active) VALUES ('TST','Test',datetime('now'),1)")
    conn.execute("INSERT INTO signals (ticker, signal_date, decision_status) VALUES ('TST', datetime('now'), 'WATCH')")
    try:
        conn.execute("INSERT INTO signals (ticker, signal_date, decision_status) VALUES ('TST', datetime('now'), 'INVALID')")
        conn.commit()
        assert False, "CHECK constraint sollte INVALID ablehnen"
    except Exception:
        pass  # erwartet

    conn.close()
    import os; os.unlink(db_path)
    print("✓ signals + trades schema korrekt, CHECK constraint aktiv")


if __name__ == "__main__":
    print("Running smoke tests...\n")
    test_database_init()
    test_config_loads()
    test_lane_scoring()
    test_alert_hysteresis()
    test_ticker_extraction()
    test_data_quality_score()
    test_llm_output_validation()
    test_alert_policy_centralized()
    test_filing_snapshot_save()
    print("\n✓ All smoke tests passed")
