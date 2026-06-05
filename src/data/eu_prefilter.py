"""
EU Pre-Filter — yfinance-based Thiel signal check for European stocks.

Purpose: Before spending Eulerpool API calls on a company, use free yfinance
data to rule out companies that cannot be Thiel monopolists by definition.

Logic:
  yfinance (free, unlimited) → disqualify obvious non-candidates (~80%)
  → only survivors get Eulerpool calls for historical trend data (~20%)

Disqualification rules (any one sufficient to skip):
  - Gross margin < 40%  → commodity business, no proprietary tech
  - Operating margin < 5% → no scale advantage
  - Revenue growth < 3% AND gross margin < 60% → mature competitive market
  - Sector in hard-excluded list → no moat possible by structure
  - Free cash flow negative AND revenue growth < 10% → burning cash, no lock-in

These are intentionally loose — false negatives (missing a real candidate)
are worse than false positives (wasting one Eulerpool call).
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    logger.warning("yfinance not available — EU pre-filter disabled")

# Sectors structurally incompatible with Thiel monopoly:
# energy/utilities/materials are commodities by definition,
# real estate and consumer staples rarely have proprietary tech moats.
EXCLUDED_SECTORS = {
    "Energy",
    "Utilities",
    "Basic Materials",
    "Real Estate",
    "Consumer Staples",
}


def yfinance_thiel_prefilter(ticker: str, exchange_suffix: str = "") -> dict:
    """
    Fetch yfinance data for a European ticker and compute a pass/fail decision.

    Args:
        ticker: Base ticker symbol (e.g. "SAP")
        exchange_suffix: yfinance exchange suffix (e.g. ".DE", ".SW", ".AS")
                         Pass empty string if ticker already includes it.

    Returns:
        {
          "ticker": str,
          "passes": bool,          # True = worth a Eulerpool call
          "disqualification": str, # Reason if passes=False, else ""
          "signals": dict,         # Raw yfinance values used
        }
    """
    qualified_ticker = f"{ticker}{exchange_suffix}" if exchange_suffix else ticker
    result = {
        "ticker": qualified_ticker,
        "passes": False,
        "disqualification": "",
        "signals": {},
    }

    if not YFINANCE_AVAILABLE:
        # Can't filter — let it through to avoid missing real candidates
        result["passes"] = True
        result["disqualification"] = "yfinance unavailable — defaulting to pass"
        return result

    try:
        info = yf.Ticker(qualified_ticker).info

        gross_margin = info.get("grossMargins")
        operating_margin = info.get("operatingMargins")
        revenue_growth = info.get("revenueGrowth")
        free_cashflow = info.get("freeCashflow")
        sector = info.get("sector", "")
        market_cap = info.get("marketCap", 0) or 0

        result["signals"] = {
            "gross_margin": round(gross_margin * 100, 1) if gross_margin is not None else None,
            "operating_margin": round(operating_margin * 100, 1) if operating_margin is not None else None,
            "revenue_growth_pct": round(revenue_growth * 100, 1) if revenue_growth is not None else None,
            "free_cashflow_positive": (free_cashflow or 0) > 0,
            "sector": sector,
            "market_cap_m": round(market_cap / 1_000_000, 0),
        }

        # ── Disqualification checks ──────────────────────────────────────────

        if sector in EXCLUDED_SECTORS:
            result["disqualification"] = f"sector '{sector}' structurally excludes Thiel moat"
            return result

        # No usable data at all → let through (don't silently miss candidates)
        if gross_margin is None and operating_margin is None and revenue_growth is None:
            result["passes"] = True
            result["disqualification"] = "no yfinance data — defaulting to pass"
            return result

        # Marketplace/platform companies (e.g. ticketing, payments) book GMV as
        # revenue, which artificially deflates gross margin. Allow them through
        # if operating margin is healthy — the moat signal is in OM, not GM.
        gm_threshold = 0.25 if (operating_margin or 0) > 0.12 else 0.40
        if gross_margin is not None and gross_margin < gm_threshold:
            result["disqualification"] = (
                f"gross margin {gross_margin*100:.1f}% < {gm_threshold*100:.0f}% — commodity business"
            )
            return result

        if operating_margin is not None and operating_margin < 0.05:
            result["disqualification"] = (
                f"operating margin {operating_margin*100:.1f}% < 5% — no scale advantage"
            )
            return result

        if (revenue_growth is not None and gross_margin is not None
                and revenue_growth < 0.03 and gross_margin < 0.60):
            result["disqualification"] = (
                f"low growth ({revenue_growth*100:.1f}%) + moderate margin "
                f"({gross_margin*100:.1f}%) — mature competitive market"
            )
            return result

        if (free_cashflow is not None and free_cashflow < 0
                and revenue_growth is not None and revenue_growth < 0.10):
            result["disqualification"] = (
                f"negative FCF + revenue growth only {revenue_growth*100:.1f}% "
                f"— burning cash without lock-in growth"
            )
            return result

        result["passes"] = True
        return result

    except Exception as e:
        logger.warning(f"{qualified_ticker}: yfinance pre-filter error: {e} — defaulting to pass")
        result["passes"] = True
        result["disqualification"] = f"error: {e}"
        return result


def batch_prefilter(
    tickers: list[dict],
    exchange_suffix: str = "",
    delay_seconds: float = 0.3,
) -> tuple[list[dict], list[dict]]:
    """
    Run yfinance_thiel_prefilter over a list of company dicts.

    Args:
        tickers: List of dicts with at least {"ticker": str}
        exchange_suffix: Applied to all tickers (e.g. ".DE" for XETRA)
        delay_seconds: Polite delay between yfinance calls

    Returns:
        (passed, rejected) — two lists of company dicts.
        Passed dicts get a "prefilter_signals" key added.
    """
    passed = []
    rejected = []

    for i, company in enumerate(tickers):
        ticker = company.get("ticker", "")
        if not ticker:
            continue

        result = yfinance_thiel_prefilter(ticker, exchange_suffix)

        if result["passes"]:
            company["prefilter_signals"] = result["signals"]
            passed.append(company)
        else:
            logger.debug(
                f"{ticker}: pre-filter rejected — {result['disqualification']}"
            )
            rejected.append({**company, "disqualification": result["disqualification"]})

        if delay_seconds and i < len(tickers) - 1:
            time.sleep(delay_seconds)

    pass_rate = len(passed) / len(tickers) * 100 if tickers else 0
    logger.info(
        f"EU pre-filter: {len(passed)}/{len(tickers)} passed ({pass_rate:.0f}%) "
        f"— {len(rejected)} rejected without Eulerpool call"
    )
    return passed, rejected
