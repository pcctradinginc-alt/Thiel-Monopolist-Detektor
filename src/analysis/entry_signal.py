"""
Einstiegssignal — verbindet Moat-These mit Preis und Timing.

Ein hoher monopoly_score allein ist kein Kaufsignal: Er sagt nichts darüber,
ob die Aktie zu einem vernünftigen Preis zu haben ist und ob die Fundamentaldaten
die These noch tragen. Dieses Modul holt für bestätigte Kandidaten kostenlose
Marktdaten (yfinance) und stuft regelbasiert ein:

  KAUFFENSTER     — bestätigter Moat + Rücksetzer/moderate Bewertung + Wachstum intakt
  QUALITAET_TEUER — bestätigter Moat, Wachstum intakt, aber nahe Hoch und hohe Bewertung
  THESE_PRUEFEN   — bestätigter Moat, aber Wachstum/Marge bröckelt
  WATCH           — Moat noch nicht bestätigt (< 2 aufeinanderfolgende Runs >= 65)

Die Einstufung ist eine Priorisierung für menschliche Analyse, keine Anlageberatung.
Alle Schwellen sind bewusst konservativ und im Code sichtbar.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    logger.warning("yfinance not installed — entry signals will be limited")


# Schwellen — bewusst einfach und transparent
CONFIRMED_MIN_SCORE = 65        # monopoly_score für "bestätigt"
CONFIRMED_MIN_RUNS = 2          # aufeinanderfolgende Runs >= 65
DRAWDOWN_BUY_PCT = 15.0         # Rücksetzer vom 52w-Hoch, ab dem ein Fenster aufgeht
PS_MODERATE = 8.0               # P/S, unterhalb dessen Bewertung als moderat gilt
MIN_REVENUE_GROWTH = 0.0        # Umsatzwachstum, unter dem die These wackelt


def fetch_market_context(ticker: str) -> dict:
    """
    Kostenloser Markt-Snapshot via yfinance.
    Gibt {} zurück, wenn keine Daten verfügbar sind (toter Ticker, Netzfehler).
    """
    if not YFINANCE_AVAILABLE:
        return {}
    try:
        t = yf.Ticker(ticker)
        fast = t.fast_info
        price = getattr(fast, "last_price", None)
        year_high = getattr(fast, "year_high", None)
        if not price or not year_high:
            return {}

        drawdown_pct = round((1 - price / year_high) * 100, 1) if year_high else None

        # .info ist langsamer und fragiler — nur die benötigten Felder, fehlertolerant
        ps_ratio = None
        rev_growth = None
        margin_trend = None
        try:
            info = t.info
            ps_ratio = info.get("priceToSalesTrailing12Months")
            rg = info.get("revenueGrowth")
            rev_growth = round(rg * 100, 1) if rg is not None else None
            gm = info.get("grossMargins")
            # Ein einzelner Margin-Wert ergibt keinen Trend — der Trend kommt,
            # falls vorhanden, aus den Filing-Signalen des Aufrufers.
            margin_trend = None if gm is None else "unknown"
        except Exception:
            pass

        return {
            "price": round(price, 2),
            "year_high": round(year_high, 2),
            "drawdown_pct": drawdown_pct,
            "ps_ratio": round(ps_ratio, 1) if ps_ratio else None,
            "revenue_growth_pct": rev_growth,
            "margin_trend": margin_trend,
        }
    except Exception as e:
        logger.warning(f"{ticker}: market context fetch failed: {e}")
        return {}


def classify_entry(monopoly_score: int, consecutive_runs: int,
                   drawdown_pct: Optional[float], ps_ratio: Optional[float],
                   revenue_growth_pct: Optional[float],
                   margin_trend: Optional[str] = None) -> tuple[str, str]:
    """
    Regelbasierte Einstufung. Reine Funktion — testbar ohne Netz.
    Gibt (Einstufung, Begründung) zurück.
    """
    confirmed = monopoly_score >= CONFIRMED_MIN_SCORE and \
        consecutive_runs >= CONFIRMED_MIN_RUNS

    if not confirmed:
        return ("WATCH",
                f"Moat noch nicht bestätigt ({consecutive_runs} Run(s) >= {CONFIRMED_MIN_SCORE})")

    # These wackelt: schrumpfender Umsatz oder fallende Marge schlägt alles
    if revenue_growth_pct is not None and revenue_growth_pct < MIN_REVENUE_GROWTH:
        return ("THESE_PRUEFEN",
                f"Umsatz schrumpft ({revenue_growth_pct}%) — Moat-These gegen Zahlen prüfen")
    if margin_trend == "falling":
        return ("THESE_PRUEFEN", "Bruttomarge fällt — Pricing Power gegen Zahlen prüfen")

    pullback = drawdown_pct is not None and drawdown_pct >= DRAWDOWN_BUY_PCT
    moderate_valuation = ps_ratio is not None and ps_ratio <= PS_MODERATE

    if pullback and (moderate_valuation or ps_ratio is None):
        return ("KAUFFENSTER",
                f"Bestätigter Moat, {drawdown_pct}% unter 52w-Hoch"
                + (f", P/S {ps_ratio}" if ps_ratio else ""))
    if pullback:
        return ("KAUFFENSTER",
                f"Bestätigter Moat, {drawdown_pct}% unter 52w-Hoch — P/S {ps_ratio} aber hoch")
    if moderate_valuation:
        return ("KAUFFENSTER",
                f"Bestätigter Moat bei moderater Bewertung (P/S {ps_ratio}), kein Pullback nötig")

    return ("QUALITAET_TEUER",
            "Bestätigter Moat, aber nahe 52w-Hoch"
            + (f" und P/S {ps_ratio}" if ps_ratio else "") + " — auf Rücksetzer warten")


def compute_entry_signal(ticker: str, monopoly_score: int,
                         consecutive_runs: int,
                         margin_trend: Optional[str] = None) -> dict:
    """
    Voller Einstiegs-Check für einen Kandidaten: Marktdaten + Einstufung.
    margin_trend kommt aus den Filing-Signalen (gross_margin_trend), falls bekannt.
    """
    ctx = fetch_market_context(ticker)
    classification, reason = classify_entry(
        monopoly_score, consecutive_runs,
        ctx.get("drawdown_pct"), ctx.get("ps_ratio"),
        ctx.get("revenue_growth_pct"),
        margin_trend or ctx.get("margin_trend"),
    )
    return {**ctx, "classification": classification, "reason": reason}


def fetch_current_price(ticker: str) -> Optional[float]:
    """Aktueller Kurs für Performance-Tracking offener Signale."""
    if not YFINANCE_AVAILABLE:
        return None
    try:
        price = getattr(yf.Ticker(ticker).fast_info, "last_price", None)
        return round(price, 2) if price else None
    except Exception:
        return None
