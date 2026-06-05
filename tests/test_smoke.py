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
    """Database schema initializes cleanly."""
    from db.database import init_db
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        conn = init_db(f.name)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        expected = {
            "universe_cohorts", "companies", "evaluations",
            "company_status", "human_feedback", "calibration_events", "run_state"
        }
        assert expected.issubset(table_names), f"Missing tables: {expected - table_names}"
    print("✓ Database init")


def test_config_loads():
    """Example config is valid YAML."""
    import yaml
    config_path = Path(__file__).parent.parent / "config" / "config.example.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    assert "universe" in config
    assert "screening" in config
    assert "alerts" in config
    assert "persistence" in config
    print("✓ Config loads")


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


if __name__ == "__main__":
    print("Running smoke tests...\n")
    test_database_init()
    test_config_loads()
    test_lane_scoring()
    test_alert_hysteresis()
    test_ticker_extraction()
    print("\n✓ All smoke tests passed")
