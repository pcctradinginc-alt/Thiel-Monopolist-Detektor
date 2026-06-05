"""
Data collection from SEC EDGAR using edgartools.

Primary source for all filing data:
  - 10-K: Item 1 (Business), Item 1A (Risk Factors), Item 7 (MD&A)
  - S-1: Full prospectus for IPOs
  - Financial trends from XBRL data

edgartools is the most stable free source — no rate limits, SEC legal obligation to keep data.
"""

import logging
import time
import re
from typing import Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

try:
    import edgar
    from edgar import Company, get_filings, set_identity
    EDGAR_AVAILABLE = True
except ImportError:
    EDGAR_AVAILABLE = False
    logger.warning("edgartools not installed — filing data will be limited")

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


# Lock-in and moat keywords to search for in filings
LOCK_IN_KEYWORDS = [
    "mission-critical", "system of record", "deeply embedded", "deeply integrated",
    "switching costs", "switching cost", "proprietary data", "proprietary technology",
    "long-term contracts", "multi-year agreements", "multi-year contract",
    "workflow automation", "regulatory compliance", "high retention",
    "net revenue retention", "net dollar retention", "expansion revenue",
    "platform", "ecosystem", "marketplace", "developer community",
    "interoperability", "standardized", "standard protocol",
    "deeply embedded", "mission critical", "business critical",
    "recurring revenue", "land and expand", "upsell", "cross-sell",
    "remaining performance obligations", "deferred revenue",
    "customer concentration", "sole source", "single source"
]

# Management camouflage keywords (signal of potential hidden moat)
CAMOUFLAGE_KEYWORDS = [
    "highly competitive", "highly fragmented", "intense competition",
    "compete with large incumbents", "may not maintain growth",
    "customers could develop internal alternatives", "well-funded competitors"
]


def set_edgar_identity():
    """Set required identity for SEC EDGAR API calls."""
    if EDGAR_AVAILABLE:
        set_identity("ThielDetector contact@example.com")


def fetch_filing_data(ticker: str, cik: str = None) -> dict:
    """
    Main entry: fetch all relevant filing data for a company.
    Returns structured dict with text sections and financial signals.
    """
    set_edgar_identity()
    result = {
        "ticker": ticker,
        "cik": cik,
        "has_10k": False,
        "has_s1": False,
        "has_10q": False,
        "business_description": "",
        "risk_factors": "",
        "mda": "",
        "s1_text": "",
        "filing_date": None,
        "financial_signals": {},
        "lock_in_keyword_hits": [],
        "camouflage_keyword_hits": [],
        "keyword_count": 0,
        "error": None
    }

    if not EDGAR_AVAILABLE:
        result["error"] = "edgartools not available"
        return result

    try:
        company = Company(ticker)

        # Try 10-K first
        filings_10k = company.get_filings(form="10-K")
        if filings_10k and len(filings_10k) > 0:
            filing = filings_10k[0]  # Most recent
            result["has_10k"] = True
            result["filing_date"] = str(filing.filing_date) if hasattr(filing, "filing_date") else None
            _extract_10k_sections(filing, result)

        # Try S-1 for IPOs (if no 10-K or recent IPO)
        if not result["has_10k"] or _is_recent_ipo(company):
            filings_s1 = company.get_filings(form="S-1")
            if filings_s1 and len(filings_s1) > 0:
                result["has_s1"] = True
                _extract_s1_sections(filings_s1[0], result)

        # Extract financial signals
        result["financial_signals"] = _extract_financial_signals(ticker, company)

        # Count lock-in and camouflage keywords
        full_text = (
            result["business_description"] + " " +
            result["risk_factors"] + " " +
            result["mda"] + " " +
            result["s1_text"]
        ).lower()

        result["lock_in_keyword_hits"] = [
            kw for kw in LOCK_IN_KEYWORDS if kw.lower() in full_text
        ]
        result["camouflage_keyword_hits"] = [
            kw for kw in CAMOUFLAGE_KEYWORDS if kw.lower() in full_text
        ]
        result["keyword_count"] = len(result["lock_in_keyword_hits"])

        # Contradiction signal: camouflage in risk factors + lock-in in business desc
        risk_lower = result["risk_factors"].lower()
        biz_lower = result["business_description"].lower()
        result["has_contradiction_signal"] = (
            any(kw.lower() in risk_lower for kw in CAMOUFLAGE_KEYWORDS) and
            any(kw.lower() in biz_lower for kw in LOCK_IN_KEYWORDS[:10])
        )

    except Exception as e:
        logger.error(f"Error fetching data for {ticker}: {e}")
        result["error"] = str(e)

    return result


def _extract_10k_sections(filing, result: dict):
    """Extract key sections from 10-K filing using edgartools TenK attributes."""
    try:
        tenk = filing.obj()
        if not tenk:
            return
        item1 = _safe_get_attr(tenk, "business", "item_1", "Item 1")
        if item1:
            result["business_description"] = _truncate(item1, 4000)

        item1a = _safe_get_attr(tenk, "risk_factors", "item_1a", "Item 1A")
        if item1a:
            result["risk_factors"] = _truncate(item1a, 2000)

        item7 = _safe_get_attr(tenk, "management_discussion", "item_7", "Item 7")
        if item7:
            result["mda"] = _truncate(item7, 2000)
    except Exception as e:
        logger.warning(f"10-K section extraction failed: {e}")


def _safe_get_attr(obj, *attr_names) -> Optional[str]:
    """Try multiple attribute names; return first non-empty string value."""
    for attr in attr_names:
        try:
            val = getattr(obj, attr, None)
            if val and isinstance(val, str) and len(val) > 100:
                return val
        except Exception:
            continue
    return None


def _extract_s1_sections(filing, result: dict):
    """Extract key sections from S-1 prospectus."""
    try:
        s1 = filing.obj()
        if s1:
            # S-1 has Business section and Risk Factors
            text = str(s1)
            result["s1_text"] = _truncate(text, 5000)

            # Try to get business section
            biz = _safe_get_attr(s1, "business", "item_1", "Business")
            if biz:
                result["business_description"] = result["business_description"] or _truncate(biz, 4000)
    except Exception as e:
        logger.warning(f"S-1 section extraction failed: {e}")


def _is_recent_ipo(company, months: int = 36) -> bool:
    """Check if company had its IPO within the last N months."""
    try:
        filings_10k = company.get_filings(form="10-K")
        if not filings_10k or len(filings_10k) == 0:
            return True
        if len(filings_10k) <= 2:
            return True  # Very few 10-Ks = recent IPO
        return False
    except Exception:
        return False


def _extract_financial_signals(ticker: str, company) -> dict:
    """
    Extract quantitative financial signals for trend analysis.
    Focus on TRENDS, not absolute levels.
    """
    signals = {
        "gross_margin_current": None,
        "gross_margin_prev": None,
        "gross_margin_trend": None,    # "rising", "falling", "stable"
        "sm_revenue_ratio_current": None,
        "sm_revenue_ratio_prev": None,
        "sm_revenue_trend": None,      # "falling" is good signal
        "revenue_growth_yoy": None,
        "revenue_per_customer_trend": None,
        "deferred_revenue_growth": None,
        "has_nrr_mention": False,
        "has_rpo_mention": False,
        "operating_leverage_signal": False,
    }

    if not YFINANCE_AVAILABLE:
        return signals

    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info

        # Basic margins from yfinance
        gross_margin = info.get("grossMargins")
        operating_margin = info.get("operatingMargins")
        revenue_growth = info.get("revenueGrowth")

        if gross_margin is not None:
            signals["gross_margin_current"] = round(gross_margin * 100, 1)
        if revenue_growth is not None:
            signals["revenue_growth_yoy"] = round(revenue_growth * 100, 1)

        # Operating leverage: revenue grows faster than operating expenses
        # Proxy: if gross margin is rising while revenue grows
        # (Would need multi-year income statement for proper calc)
        financials = ticker_obj.financials
        if financials is not None and not financials.empty:
            if len(financials.columns) >= 2:
                # Try to compute gross margin trend from income statement
                try:
                    total_rev = financials.loc["Total Revenue"] if "Total Revenue" in financials.index else None
                    gross_profit = financials.loc["Gross Profit"] if "Gross Profit" in financials.index else None
                    sm_expense = (
                        financials.loc["Selling General Administrative"] 
                        if "Selling General Administrative" in financials.index else None
                    )

                    if total_rev is not None and gross_profit is not None:
                        gm_current = (gross_profit.iloc[0] / total_rev.iloc[0]) if total_rev.iloc[0] else 0
                        gm_prev = (gross_profit.iloc[1] / total_rev.iloc[1]) if total_rev.iloc[1] else 0
                        signals["gross_margin_current"] = round(gm_current * 100, 1)
                        signals["gross_margin_prev"] = round(gm_prev * 100, 1)
                        delta = gm_current - gm_prev
                        signals["gross_margin_trend"] = (
                            "rising" if delta > 0.02 else
                            "falling" if delta < -0.02 else
                            "stable"
                        )

                    if sm_expense is not None and total_rev is not None:
                        sm_current = abs(sm_expense.iloc[0]) / total_rev.iloc[0] if total_rev.iloc[0] else 0
                        sm_prev = abs(sm_expense.iloc[1]) / total_rev.iloc[1] if total_rev.iloc[1] else 0
                        signals["sm_revenue_ratio_current"] = round(sm_current * 100, 1)
                        signals["sm_revenue_ratio_prev"] = round(sm_prev * 100, 1)
                        signals["sm_revenue_trend"] = (
                            "falling" if sm_current < sm_prev * 0.95 else
                            "rising" if sm_current > sm_prev * 1.05 else
                            "stable"
                        )

                        # Operating leverage signal: GM rising AND S&M/Rev falling
                        signals["operating_leverage_signal"] = (
                            signals.get("gross_margin_trend") == "rising" and
                            signals.get("sm_revenue_trend") == "falling"
                        )
                except Exception as e:
                    logger.debug(f"Trend calculation failed for {ticker}: {e}")

    except Exception as e:
        logger.warning(f"Financial signal extraction failed for {ticker}: {e}")

    return signals


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, preserving sentence boundaries."""
    if not text or len(text) <= max_chars:
        return text or ""
    truncated = text[:max_chars]
    last_period = truncated.rfind(".")
    if last_period > max_chars * 0.8:
        return truncated[:last_period + 1]
    return truncated + "..."


def compute_lane_scores(filing_data: dict, config: dict) -> dict:
    """
    Assign companies to candidate lanes and compute priority scores.
    Lanes are additive — a company can qualify for multiple.
    No company is excluded by lane assignment.
    """
    lanes = {}
    total_score = 0

    text = (
        filing_data.get("business_description", "") + " " +
        filing_data.get("mda", "") + " " +
        filing_data.get("s1_text", "")
    ).lower()

    signals = filing_data.get("financial_signals", {})

    # Lane 1: Hidden Wedge
    hidden_wedge_score = (
        filing_data.get("keyword_count", 0) * 5 +
        (20 if filing_data.get("has_contradiction_signal") else 0) +
        (10 if any(kw in text for kw in ["system of record", "mission-critical", "mission critical"]) else 0)
    )
    if hidden_wedge_score > 0:
        lanes["hidden_wedge"] = min(hidden_wedge_score, 100)
        total_score += lanes["hidden_wedge"]

    # Lane 2: Emerging Platform
    platform_keywords = ["platform", "ecosystem", "marketplace", "developer community", "api"]
    platform_hits = sum(1 for kw in platform_keywords if kw in text)
    if platform_hits >= 2:
        lanes["emerging_platform"] = min(platform_hits * 15, 100)
        total_score += lanes["emerging_platform"]

    # Lane 3: Scale Inflection (operating leverage signal)
    if signals.get("operating_leverage_signal"):
        lanes["scale_inflection"] = 80
        total_score += 80
    elif signals.get("gross_margin_trend") == "rising":
        lanes["scale_inflection"] = 40
        total_score += 40

    # Lane 4: IPO / Recent Filing
    if filing_data.get("has_s1") and not filing_data.get("has_10k"):
        lanes["ipo_narrow"] = 60
        total_score += 60
    elif filing_data.get("has_s1"):
        lanes["ipo_narrow"] = 30
        total_score += 30

    # Lane 5: Filing Change (new keywords vs previous — simplified)
    if filing_data.get("keyword_count", 0) >= 8:
        lanes["filing_change"] = min(filing_data["keyword_count"] * 3, 60)
        total_score += lanes["filing_change"]

    return {
        "lanes": lanes,
        "total_lane_score": min(total_score, 300),
        "primary_lane": max(lanes, key=lanes.get) if lanes else None
    }
