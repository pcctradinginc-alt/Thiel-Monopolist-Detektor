"""
Data collection from SEC EDGAR using edgartools.

Primary source for all filing data:
  - 10-K: Item 1 (Business), Item 1A (Risk Factors), Item 7 (MD&A)
  - S-1: Full prospectus for IPOs
  - Financial trends from XBRL data

edgartools is the most stable free source — no rate limits, SEC legal obligation to keep data.
"""

import logging
import os
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


# Lock-in and moat keywords — English
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
    "customer concentration", "sole source", "single source",
]

# German lock-in and moat keywords — DACH annual reports
LOCK_IN_KEYWORDS_DE = [
    "weltmarktführer", "marktführer", "nischenmarkt", "nischenanbieter",
    "alleinstellungsmerkmal", "kundenbindung", "wiederkehrende umsätze",
    "wiederkehrende erlöse", "switching costs", "wechselkosten",
    "kritische infrastruktur", "geschäftskritisch", "systemkritisch",
    "tief integriert", "tief verwurzelt", "proprietäre technologie",
    "proprietäre daten", "betriebssystem für", "plattform",
    "ökosystem", "marktplatz", "langfristige verträge",
    "mehrjährige verträge", "hohe kundenbindung", "hohe retention",
    "umsatzwachstum pro kunde", "cross-selling", "upselling",
    "unverzichtbar", "kernprozess", "standard in der branche",
    "de-facto-standard", "technologieführer", "innovationsführer",
]

# Management camouflage keywords (signal of potential hidden moat)
CAMOUFLAGE_KEYWORDS = [
    "highly competitive", "highly fragmented", "intense competition",
    "compete with large incumbents", "may not maintain growth",
    "customers could develop internal alternatives", "well-funded competitors",
    # German equivalents
    "intensiver wettbewerb", "stark fragmentiert", "starker wettbewerb",
    "wettbewerbsintensiv", "große wettbewerber",
]


def set_edgar_identity():
    """
    Set required identity for SEC EDGAR API calls.
    Die SEC verlangt eine erreichbare Kontaktadresse im User-Agent —
    bei den wöchentlichen Massenabrufen riskiert eine falsche Adresse IP-Sperren.
    """
    if not EDGAR_AVAILABLE:
        return
    contact = os.environ.get("SEC_CONTACT_EMAIL")
    if not contact:
        contact = "info@pcctradinginc.com"
        logger.warning(
            f"SEC_CONTACT_EMAIL nicht gesetzt — nutze Fallback {contact}. "
            "Bitte als GitHub Secret / env-Variable konfigurieren."
        )
    set_identity(f"ThielDetector {contact}")


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

        all_lock_in = LOCK_IN_KEYWORDS + LOCK_IN_KEYWORDS_DE
        result["lock_in_keyword_hits"] = [
            kw for kw in all_lock_in if kw.lower() in full_text
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
            any(kw.lower() in biz_lower for kw in all_lock_in[:15])
        )

    except Exception as e:
        logger.error(f"Error fetching data for {ticker}: {e}")
        result["error"] = str(e)

    return result


def fetch_eu_filing_data(ticker: str, company_name: str, exchange: str = "xetra") -> dict:
    """
    Fetch filing data for a European company.

    Strategy:
      1. yfinance for financial signals (always)
      2. Bundesanzeiger for text (DE companies)
      3. Fallback: empty text (LLM will rely on financial signals only)
    """
    result = {
        "ticker": ticker,
        "cik": None,
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
        "error": None,
        "source": "eu",
        "exchange": exchange,
    }

    # Financial signals + Business Description via yfinance
    # longBusinessSummary ist für ~70-85% aller EU-Firmen verfügbar — kostenlos
    if YFINANCE_AVAILABLE:
        try:
            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.info

            # ── Text Source 0: yfinance longBusinessSummary (gratis, ~70% Abdeckung) ──
            yf_desc = info.get("longBusinessSummary", "") or ""
            if len(yf_desc) > 100:
                result["business_description"] = yf_desc[:4000]
                result["source_enriched"] = "yfinance"
                logger.debug(f"{ticker}: yfinance description {len(yf_desc)} chars")

            result["financial_signals"] = _extract_financial_signals(ticker, ticker_obj.info.get("_company"))
            insider_pct = info.get("heldPercentInsiders")
            result["financial_signals"]["insider_ownership_pct"] = (
                round(insider_pct * 100, 1) if insider_pct is not None else None
            )
            result["financial_signals"]["family_owned"] = (insider_pct or 0) > 0.20
        except Exception as e:
            logger.warning(f"{ticker}: yfinance failed: {e}")

    # ── Text Source 1: Bundesanzeiger (DE) — überschreibt yfinance wenn verfügbar ──
    if exchange in ("xetra", "eu_ipo", "eu_ipo_bafin") and company_name:
        from data.bundesanzeiger import fetch_bundesanzeiger_text
        ba_result = fetch_bundesanzeiger_text(company_name, ticker)
        if not ba_result.get("error") and ba_result.get("business_description"):
            result["business_description"] = ba_result["business_description"]
            result["risk_factors"] = ba_result.get("risk_factors", "")
            result["mda"] = ba_result.get("mda", "")
            result["filing_date"] = ba_result.get("filing_date")
            result["has_10k"] = True
            result["source_enriched"] = "bundesanzeiger"

    # ── Text Source 2: Companies House (UK) ──────────────────────────────────
    if exchange in ("lse", "aim") and company_name:
        import os
        ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
        if ch_key:
            from data.companies_house import get_filing_text
            ch_result = get_filing_text(ticker, company_name, ch_key)
            if not ch_result.get("error") and ch_result.get("business_description"):
                result["business_description"] = ch_result["business_description"]
                result["risk_factors"] = ch_result.get("risk_factors", "")
                result["mda"] = ch_result.get("mda", "")
                result["filing_date"] = ch_result.get("filing_date")
                result["has_10k"] = True

    # ── Text Source 3: ESEF-Geschäftsbericht (filings.xbrl.org, alle EU) ────
    # Echte Berichtsabschnitte statt Kurzbeschreibungen — der größte Hebel
    # für die Datenqualität bei EU-Small-Caps. Nur wenn Text noch dünn ist.
    biz_words = len((result["business_description"] or "").split())
    if biz_words < 250 and company_name:
        from data.esef_fetcher import fetch_esef_text
        esef = fetch_esef_text(company_name, ticker)
        if not esef.get("error") and esef.get("business_description"):
            result["business_description"] = esef["business_description"]
            if esef.get("risk_factors"):
                result["risk_factors"] = esef["risk_factors"]
            if esef.get("mda"):
                result["mda"] = esef["mda"]
            if esef.get("filing_date"):
                result["filing_date"] = esef["filing_date"]
            result["has_10k"] = True
            result["source_enriched"] = "esef"

    # ── Text Source 4: Wikipedia (kostenlos, ~60-70% Abdeckung) ─────────────
    # Für Firmen die weder Bundesanzeiger noch yfinance-Beschreibung haben
    # Primär: kleinere Nordics, Benelux, AT-Firmen
    if not result["business_description"] and company_name:
        from data.wikipedia_enricher import get_wikipedia_description
        wiki_desc = get_wikipedia_description(company_name, ticker)
        if wiki_desc:
            result["business_description"] = wiki_desc
            result["source_enriched"] = "wikipedia"

    # ── Text Source 4: EODHD (fallback for all EU — benötigt bezahlten Plan) ─
    if not result["business_description"]:
        import os
        eodhd_key = os.environ.get("EODHD_API_KEY", "")
        if eodhd_key:
            from data.eodhd_enricher import enrich_filing_data
            result = enrich_filing_data(result, ticker, eodhd_key)

    # Keyword scoring on whatever text we have
    full_text = (
        result["business_description"] + " " +
        result["risk_factors"] + " " +
        result["mda"]
    ).lower()

    all_lock_in = LOCK_IN_KEYWORDS + LOCK_IN_KEYWORDS_DE
    result["lock_in_keyword_hits"] = [kw for kw in all_lock_in if kw.lower() in full_text]
    result["camouflage_keyword_hits"] = [kw for kw in CAMOUFLAGE_KEYWORDS if kw.lower() in full_text]
    result["keyword_count"] = len(result["lock_in_keyword_hits"])

    risk_lower = result["risk_factors"].lower()
    biz_lower = result["business_description"].lower()
    result["has_contradiction_signal"] = (
        any(kw.lower() in risk_lower for kw in CAMOUFLAGE_KEYWORDS) and
        any(kw.lower() in biz_lower for kw in all_lock_in[:15])
    )

    return result


def _extract_10k_sections(filing, result: dict):
    """Extract key sections from 10-K filing using edgartools TenK attributes."""
    try:
        tenk = filing.obj()
        if not tenk:
            return
        # Try new parser attribute names first, then legacy fallbacks
        item1 = _safe_get_attr(tenk,
            "business",           # edgartools v5 new parser
            "part_i_item_1",      # new parser section key
            "item_1", "Item 1",   # legacy
        )
        if item1:
            result["business_description"] = _truncate(item1, 4000)

        item1a = _safe_get_attr(tenk,
            "risk_factors",
            "part_i_item_1a",
            "item_1a", "Item 1A",
        )
        if item1a:
            result["risk_factors"] = _truncate(item1a, 2000)

        item7 = _safe_get_attr(tenk,
            "management_discussion",
            "part_ii_item_7",
            "item_7", "Item 7",
        )
        if item7:
            result["mda"] = _truncate(item7, 2000)

        # Last resort: use get_item_with_part if available
        if not result["business_description"] and hasattr(tenk, "get_item_with_part"):
            try:
                text = tenk.get_item_with_part("1", "I")
                if text and len(str(text)) > 100:
                    result["business_description"] = _truncate(str(text), 4000)
            except Exception:
                pass

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
        "analyst_count": None,
    }

    if not YFINANCE_AVAILABLE:
        return signals

    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info

        # Analysten-Abdeckung: bei <3 Analysten ist Fehlbepreisung strukturell
        # wahrscheinlicher — dort hat systematisches Lesen den größten Vorsprung
        analyst_count = info.get("numberOfAnalystOpinions")
        if analyst_count is not None:
            signals["analyst_count"] = int(analyst_count)

        # Basic margins from yfinance
        gross_margin = info.get("grossMargins")
        operating_margin = info.get("operatingMargins")
        revenue_growth = info.get("revenueGrowth")

        if gross_margin is not None:
            signals["gross_margin_current"] = round(gross_margin * 100, 1)
        if revenue_growth is not None:
            signals["revenue_growth_yoy"] = round(revenue_growth * 100, 1)

        # Multi-year margin trends (up to 4 years from yfinance annual financials)
        financials = ticker_obj.financials
        if financials is not None and not financials.empty:
            try:
                total_rev = financials.loc["Total Revenue"] if "Total Revenue" in financials.index else None
                gross_profit = financials.loc["Gross Profit"] if "Gross Profit" in financials.index else None
                sm_expense = (
                    financials.loc["Selling General Administrative"]
                    if "Selling General Administrative" in financials.index else None
                )

                if total_rev is not None and gross_profit is not None and len(total_rev) >= 2:
                    # Compute gross margin for all available years (newest first)
                    gm_series = []
                    for i in range(len(total_rev)):
                        rev = total_rev.iloc[i]
                        gp = gross_profit.iloc[i]
                        if rev and rev != 0:
                            gm_series.append(round(gp / rev * 100, 1))

                    if gm_series:
                        signals["gross_margin_current"] = gm_series[0]
                        signals["gross_margin_prev"] = gm_series[1] if len(gm_series) > 1 else None
                        signals["gross_margin_history"] = gm_series  # full history for LLM

                        delta_1y = gm_series[0] - gm_series[1] if len(gm_series) > 1 else 0
                        signals["gross_margin_trend"] = (
                            "rising" if delta_1y > 2 else
                            "falling" if delta_1y < -2 else
                            "stable"
                        )

                        # 4-year consistency: count years where GM was rising
                        if len(gm_series) >= 3:
                            rising_years = sum(
                                1 for i in range(len(gm_series) - 1)
                                if gm_series[i] > gm_series[i + 1] + 1
                            )
                            signals["gross_margin_consistently_rising"] = (
                                rising_years >= len(gm_series) - 1
                            )

                if sm_expense is not None and total_rev is not None and len(total_rev) >= 2:
                    sm_series = []
                    for i in range(len(total_rev)):
                        rev = total_rev.iloc[i]
                        sm = sm_expense.iloc[i]
                        if rev and rev != 0:
                            sm_series.append(round(abs(sm) / rev * 100, 1))

                    if sm_series:
                        signals["sm_revenue_ratio_current"] = sm_series[0]
                        signals["sm_revenue_ratio_prev"] = sm_series[1] if len(sm_series) > 1 else None
                        signals["sm_revenue_trend"] = (
                            "falling" if sm_series[0] < sm_series[1] * 0.95 else
                            "rising" if sm_series[0] > sm_series[1] * 1.05 else
                            "stable"
                        ) if len(sm_series) > 1 else "unknown"

                        # Operating leverage: GM rising + S&M/Rev falling (multi-year)
                        gm_rising = signals.get("gross_margin_trend") == "rising"
                        sm_falling = signals.get("sm_revenue_trend") == "falling"
                        gm_consistent = signals.get("gross_margin_consistently_rising", False)
                        signals["operating_leverage_signal"] = gm_rising and sm_falling
                        # Stronger signal: consistent over multiple years
                        signals["strong_operating_leverage"] = gm_consistent and sm_falling

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
    if signals.get("strong_operating_leverage"):
        lanes["scale_inflection"] = 100  # multi-year confirmed
        total_score += 100
    elif signals.get("operating_leverage_signal"):
        lanes["scale_inflection"] = 80
        total_score += 80
    elif signals.get("gross_margin_consistently_rising"):
        lanes["scale_inflection"] = 60
        total_score += 60
    elif signals.get("gross_margin_trend") == "rising":
        lanes["scale_inflection"] = 40
        total_score += 40

    # Lane 4: IPO / Recent Filing
    # EU IPOs (source=ipo) get automatic high priority — prospectus analysis
    # is the single best signal for hidden Thiel moats
    if filing_data.get("source") == "ipo" or filing_data.get("cohort_id") in ("eu_ipo", "ipo_recent"):
        lanes["ipo_narrow"] = 80
        total_score += 80
    elif filing_data.get("has_s1") and not filing_data.get("has_10k"):
        lanes["ipo_narrow"] = 60
        total_score += 60
    elif filing_data.get("has_s1"):
        lanes["ipo_narrow"] = 30
        total_score += 30

    # Lane 5: Filing Change (new keywords vs previous — simplified)
    if filing_data.get("keyword_count", 0) >= 8:
        lanes["filing_change"] = min(filing_data["keyword_count"] * 3, 60)
        total_score += lanes["filing_change"]

    # Lane 6: Financial Quality Signal (Daten/Infrastruktur-Monopole ohne SaaS-Keywords)
    # Hohe Gross Margin + Wachstum = starkes Moat-Signal unabhängig von Keywords
    # Behebt: CSGP/CME/MCO bekommen Lane-Score 0 weil keine lock-in Keywords
    gm = signals.get("gross_margin_current", 0) or 0
    rev_growth = signals.get("revenue_growth_yoy", 0) or 0
    if gm >= 70 and rev_growth >= 8:
        # Starkes finanzielles Moat-Signal: hohe Marge + Wachstum
        fin_score = 50
        if gm >= 80:
            fin_score += 15
        if rev_growth >= 15:
            fin_score += 10
        if signals.get("sm_ratio_declining"):
            fin_score += 10  # fallende S&M-Ratio = struktureller Vorteil
        lanes["financial_quality"] = min(fin_score, 85)
        total_score += lanes["financial_quality"]
    elif gm >= 60 and rev_growth >= 5:
        # Moderates Signal — reicht für LLM-Call
        lanes["financial_quality"] = 30
        total_score += 30

    # Lane 7: Under-Followed (Coverage-Filter)
    # Bei < 3 Analysten ist Fehlbepreisung strukturell wahrscheinlicher —
    # genau dort hat systematisches Filing-Lesen den größten Vorsprung.
    # Boost nur wenn ein anderes Moat-Signal existiert (sonst priorisiert
    # er bloß illiquide Leerstellen).
    analyst_count = signals.get("analyst_count")
    if analyst_count is not None and lanes:
        if analyst_count < 3:
            lanes["under_followed"] = 25
            total_score += 25
        elif analyst_count <= 5:
            lanes["under_followed"] = 12
            total_score += 12

    return {
        "lanes": lanes,
        "total_lane_score": min(total_score, 300),
        "primary_lane": max(lanes, key=lanes.get) if lanes else None
    }
