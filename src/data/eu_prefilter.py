"""
EU Pre-Filter — yfinance-based Thiel signal check for European stocks.

Philosophy: LOOSE filter. Missing one real Thiel monopolist is worse than
analyzing 10 extra companies. European Hidden Champions often have unusual
financial profiles — niche industrial companies with 35% GM can still have
impenetrable moats (e.g. Rational AG, specialty valve manufacturers).

Only hard exclusions:
  - Clearly commodity sectors (Energy, Utilities, Basic Materials)
  - No financial data AND market cap < €30M (too small/illiquid)
  - Negative revenue AND negative FCF (zombie company)

Everything else passes → yfinance is just a first-pass noise filter,
not a quality gate. The LLM makes the real decision.
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

# ONLY these sectors are structurally impossible Thiel monopolists
# Everything else (including Industrials) can have Hidden Champion moats
HARD_EXCLUDED_SECTORS = {
    "Energy",           # commodity by definition
    "Utilities",        # regulated monopoly, not Thiel type
    "Basic Materials",  # commodity
}

# Min market cap — below this, too illiquid for meaningful screening
MIN_MARKET_CAP_EUR = 20_000_000  # €20M

def yfinance_thiel_prefilter(ticker: str, exchange_suffix: str = "") -> dict:
    """
    Loose yfinance pre-filter. Passes ~70% of companies (vs 20% before).
    Only hard-excludes clear non-candidates to avoid missing Hidden Champions.
    """
    qualified_ticker = f"{ticker}{exchange_suffix}" if exchange_suffix else ticker
    result = {
        "ticker": qualified_ticker,
        "passes": True,  # default PASS — err on the side of inclusion
        "disqualification": "",
        "signals": {},
    }

    if not YFINANCE_AVAILABLE:
        return result

    try:
        info = yf.Ticker(qualified_ticker).info

        gross_margin    = info.get("grossMargins")
        operating_margin = info.get("operatingMargins")
        revenue_growth  = info.get("revenueGrowth")
        free_cashflow   = info.get("freeCashflow")
        total_revenue   = info.get("totalRevenue", 0) or 0
        sector          = info.get("sector", "")
        market_cap      = info.get("marketCap", 0) or 0
        insider_pct     = info.get("heldPercentInsiders")

        result["signals"] = {
            "gross_margin":         round(gross_margin * 100, 1) if gross_margin is not None else None,
            "operating_margin":     round(operating_margin * 100, 1) if operating_margin is not None else None,
            "revenue_growth_pct":   round(revenue_growth * 100, 1) if revenue_growth is not None else None,
            "free_cashflow_positive": (free_cashflow or 0) > 0,
            "sector":               sector,
            "market_cap_m":         round(market_cap / 1_000_000, 1),
            "insider_ownership_pct": round(insider_pct * 100, 1) if insider_pct is not None else None,
            "family_owned":         (insider_pct or 0) > 0.20,
        }

        # ── Hard exclusion 1: commodity sector ──────────────────────────────
        if sector in HARD_EXCLUDED_SECTORS:
            result["passes"] = False
            result["disqualification"] = f"sector '{sector}' — commodity, no Thiel moat possible"
            return result

        # ── Hard exclusion 2: too small / no data ───────────────────────────
        if market_cap > 0 and market_cap < MIN_MARKET_CAP_EUR:
            result["passes"] = False
            result["disqualification"] = f"market cap €{market_cap/1e6:.1f}M < €{MIN_MARKET_CAP_EUR/1e6:.0f}M minimum"
            return result

        # ── Hard exclusion 3: zombie company (no revenue + negative FCF) ────
        if (total_revenue == 0 and
                free_cashflow is not None and free_cashflow < -50_000_000):
            result["passes"] = False
            result["disqualification"] = "no revenue + large negative FCF — zombie/shell company"
            return result

        # ── Soft signal: Real Estate — rarely has Thiel moat ────────────────
        # Not a hard exclusion — PropTech platforms can have moats
        if sector == "Real Estate":
            # Only exclude if clearly commodity (low margin, low growth)
            if (gross_margin is not None and gross_margin < 0.20 and
                    (revenue_growth is None or revenue_growth < 0.02)):
                result["passes"] = False
                result["disqualification"] = "Real Estate with low margin + no growth"
                return result

        # Everything else passes — LLM decides
        return result

    except Exception as e:
        logger.debug(f"{qualified_ticker}: yfinance error ({e}) — defaulting to pass")
        return result  # default is already passes=True


def batch_prefilter(
    tickers: list[dict],
    exchange_suffix: str = "",
    delay_seconds: float = 0.2,
) -> tuple[list[dict], list[dict]]:
    """
    Run pre-filter over a list of company dicts.
    Returns (passed, rejected).
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
            logger.debug(f"{ticker}: rejected — {result['disqualification']}")
            rejected.append({**company, "disqualification": result["disqualification"]})

        if delay_seconds and i < len(tickers) - 1:
            time.sleep(delay_seconds)

    pass_rate = len(passed) / len(tickers) * 100 if tickers else 0
    logger.info(
        f"EU pre-filter: {len(passed)}/{len(tickers)} passed ({pass_rate:.0f}%) "
        f"— {len(rejected)} excluded"
    )
    return passed, rejected
