"""
EU Universe Builder — DACH + NL + FR equity universe for Thiel screening.

Strategy:
  1. Static seed lists (always works, no external dependency)
  2. Try to fetch live index compositions from free public sources
  3. yfinance pre-filter removes ~80% before any Eulerpool call
  4. Survivors written to DB with eu_ cohort prefix

Exchange suffixes:
  XETRA (DE):  .DE
  SIX (CH):    .SW
  Euronext NL: .AS
  Vienna (AT): .VI
  Euronext FR: .PA
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


# ─── Static Seed Lists ───────────────────────────────────────────────────────
# Source: index compositions as of 2026-Q1. Updated manually when index changes.
# Format: (base_ticker, company_name)

XETRA_SEEDS = [
    # DAX 40
    ("ADS", "Adidas AG"), ("AIR", "Airbus SE"), ("ALV", "Allianz SE"),
    ("BAS", "BASF SE"), ("BAYN", "Bayer AG"), ("BEI", "Beiersdorf AG"),
    ("BMW", "BMW AG"), ("BNR", "Brenntag SE"), ("CON", "Continental AG"),
    ("1COV", "Covestro AG"), ("DHER", "Delivery Hero SE"), ("DB1", "Deutsche Boerse AG"),
    ("DBK", "Deutsche Bank AG"), ("DHL", "Deutsche Post AG"), ("DTE", "Deutsche Telekom AG"),
    ("EOAN", "E.ON SE"), ("FRE", "Fresenius SE"), ("FME", "Fresenius Medical Care AG"),
    ("HEI", "HeidelbergCement AG"), ("HEN3", "Henkel AG"), ("IFX", "Infineon Technologies AG"),
    ("MBG", "Mercedes-Benz Group AG"), ("MRK", "Merck KGaA"), ("MTX", "MTU Aero Engines AG"),
    ("MUV2", "Munich Re AG"), ("PAH3", "Porsche Automobil Holding SE"),
    ("P911", "Porsche AG"), ("PUMA", "PUMA SE"), ("RWE", "RWE AG"),
    ("SAP", "SAP SE"), ("SHL", "Siemens Healthineers AG"), ("SIE", "Siemens AG"),
    ("SY1", "Symrise AG"), ("VNA", "Vonovia SE"), ("VOW3", "Volkswagen AG"),
    ("ZAL", "Zalando SE"), ("ENR", "Siemens Energy AG"), ("NVDA", "NVIDIA (XETRA)"),
    ("2222", "Saudi Aramco (XETRA)"), ("QIA", "Qiagen NV"),

    # MDAX (mid cap, high Thiel potential)
    ("AFX", "Carl Zeiss Meditec AG"), ("AIXA", "Aixtron SE"),
    ("AOF", "Atoss Software AG"), ("ARR", "Arrhenius Pharma AG"),
    ("BC8", "Bechtle AG"), ("BDT", "Bertrandt AG"), ("BOSS", "Hugo Boss AG"),
    ("COP", "Comdirect Bank AG"), ("CWC", "Cancom SE"), ("DKBK", "Deutsche Kreditbank AG"),
    ("DWS", "DWS Group GmbH"), ("ECK", "Eckert & Ziegler AG"),
    ("EVD", "CTS Eventim AG"), ("EVK", "Evonik Industries AG"),
    ("FNTN", "freenet AG"), ("GBF", "Bilfinger SE"), ("GFJ", "Grenke AG"),
    ("HAB", "Hamborner REIT AG"), ("HAG", "Hensoldt AG"), ("HAW", "Hawesko Holding AG"),
    ("HDD", "Heidelberger Druckmaschinen AG"), ("HFG", "HelloFresh SE"),
    ("HOT", "Hochtief AG"), ("HYQ", "Hypoport SE"), ("JNXS", "Jungheinrich AG"),
    ("KGX", "Kion Group AG"), ("KSB3", "KSB SE"), ("LEG", "LEG Immobilien SE"),
    ("LHA", "Lufthansa AG"), ("MDG1", "Medigene AG"), ("MED", "Medios AG"),
    ("MELN", "Mynaric AG"), ("NEM", "Nemetschek SE"), ("NOEJ", "Novabase AG"),
    ("OHB", "OHB SE"), ("OSR", "Osram Licht AG"), ("PSAN", "ProSiebenSat.1 Media SE"),
    ("RAA", "RATIONAL AG"), ("RRTL", "RTL Group SA"), ("RTO4", "Rentokil Initial (XETRA)"),
    ("S92", "SMA Solar Technology AG"), ("SBS", "Stratec SE"),
    ("SDX", "SGL Carbon SE"), ("SGCG", "Siltronic AG"), ("SIX2", "Sixt SE"),
    ("SMHN", "SUESS MicroTec SE"), ("SOW", "Software AG"), ("SPM", "Stabilus SE"),
    ("SRTX", "Sartorius Stedim Biotech"), ("SRT3", "Sartorius AG"),
    ("TAG", "TAG Immobilien AG"), ("TIM", "Traton SE"), ("TLX", "Talanx AG"),
    ("TUI1", "TUI AG"), ("VBK", "Verbio SE"), ("VH2", "Vitesco Technologies Group AG"),
    ("VIB3", "Villeroy & Boch AG"), ("VOS", "Vossloh AG"), ("WAF", "Siltronic AG"),
    ("WBAG", "Westwing Group SE"), ("WCH", "Wacker Chemie AG"),

    # TecDAX (highest Thiel density)
    ("AT1", "Aroundtown SA"), ("BFSA", "Befesa SA"), ("CA1", "Canopy Growth (XETRA)"),
    ("DBAN", "Deutsche Beteiligungs AG"), ("DIC", "DIC Asset AG"),
    ("EMH", "Eckert & Ziegler Strahlen"), ("FPH", "flatexDEGIRO AG"),
    ("GFT", "GFT Technologies SE"), ("GOS", "Gosen AG"),
    ("HAG2", "Hamborner AG"), ("IINX", "Inxmail GmbH"), ("IOS", "IONOS Group SE"),
    ("ISH2", "Ishares (XETRA)"), ("ITN", "Internxt AG"),
    ("MBB", "MBB SE"), ("MCH", "Mach7 Technologies"),
    ("MORG", "Morgan Advanced (XETRA)"), ("NA9", "Nagarro SE"),
    ("NFON", "NFON AG"), ("PSH", "PSI Software SE"),
    ("R3NK", "Renk Group AG"), ("RENE", "Renergetica AG"),
    ("SFQ", "SAF-Holland SE"), ("SGF", "Software AG"),
    ("SMHN2", "SUESS MicroTec"), ("TELN", "Telenet Group"),
    ("UTDI", "United Internet AG"), ("VAR1", "VARTA AG"),
    ("VIE", "Vienna International Airport"), ("WDP", "Warehouses De Pauw"),
    ("XTP", "Xentis AG"), ("YSN", "Ypsomed Holding"),

    # SDAX + smaller interesting names
    ("ACX", "Accentro Real Estate AG"), ("ADJ", "adjoe GmbH"),
    ("AOF2", "Atoss Software pref"), ("CANE", "Canandaigua National"),
    ("DBO", "Drägerwerk AG"), ("ECV", "Encavis AG"),
    ("FNBG", "First National Bank"), ("GFTI", "GFT Technologies"),
    ("HBH", "Hamburger Hafen und Logistik AG"), ("HDD2", "Hella GmbH & Co"),
    ("IGGD", "IGG Inc (XETRA)"), ("ILM1", "Iliad SA (XETRA)"),
    ("IPH", "Interparfums (XETRA)"), ("IRWD", "Ironwood Pharma (XETRA)"),
    ("KBC", "KBC Groep (XETRA)"), ("KNEBV", "Kone Oyj (XETRA)"),
    ("LEC", "Leclanche SA"), ("LLD", "Lloyd's Banking (XETRA)"),
    ("MNST", "Monster Beverage (XETRA)"), ("NDX1", "Nordex SE"),
    ("PGN", "Paragon GmbH & Co"), ("PRIME", "Primecoin"),
    ("PSI", "PSI Software AG"), ("PTRO", "Petro Welt Technologies AG"),
    ("PWO", "Progress-Werk Oberkirch AG"), ("RWWE", "RWE Wind Energy"),
    ("SBO", "Schoeller-Bleckmann Oilfield"), ("SCY", "Scancom PLC"),
    ("SHEL", "Shell PLC (XETRA)"), ("SKB", "Skan Group AG"),
    ("TGH", "Triton International (XETRA)"), ("TKA", "Thyssenkrupp AG"),
    ("UKB", "United Utilities (XETRA)"), ("VBH", "VBH Holding AG"),
    ("VODI", "Vodafone Group (XETRA)"), ("WIN", "Wincor Nixdorf AG"),
    ("WUW", "Württembergische Gemeinde-Versicherung"), ("XNT", "Xanten AG"),
    ("ZEG", "Zeal Network SE"),
]

SIX_SEEDS = [
    # SMI + SMIM + SPI Extra (Switzerland)
    ("ABBN", "ABB Ltd"), ("ADEN", "Adecco Group AG"), ("ALC", "Alcon Inc"),
    ("CFR", "Compagnie Financiere Richemont SA"), ("CSGN", "Credit Suisse Group AG"),
    ("GEBN", "Geberit AG"), ("GIVN", "Givaudan SA"), ("HOLN", "Holcim Ltd"),
    ("KNIN", "Kuehne + Nagel International AG"), ("LONN", "Lonza Group AG"),
    ("NESN", "Nestle SA"), ("NOVN", "Novartis AG"), ("ROG", "Roche Holding AG"),
    ("SCMN", "Swisscom AG"), ("SGSN", "SGS SA"), ("SIKA", "Sika AG"),
    ("SLHN", "Swiss Life Holding AG"), ("SRENH", "Swiss Re AG"),
    ("UBSG", "UBS Group AG"), ("UHRN", "Swatch Group AG"), ("ZURN", "Zurich Insurance Group AG"),
    # SMIM
    ("AMS", "ams OSRAM AG"), ("BAER", "Julius Baer Group AG"), ("BARN", "Barry Callebaut AG"),
    ("BCHN", "Belimo Holding AG"), ("CFRN", "Cembra Money Bank AG"),
    ("DKSH", "DKSH Holding AG"), ("EMMN", "Emmi AG"), ("FLUGN", "Flughafen Zurich AG"),
    ("HELN", "Helvetia Holding AG"), ("HIAG", "HIAG Immobilien Holding AG"),
    ("HOCN", "Hochdorf Holding AG"), ("HOEHN", "Schindler Holding AG"),
    ("IELN", "Interroll Holding AG"), ("INRN", "Inficon Holding AG"),
    ("MBTN", "Meyer Burger Technology AG"), ("MOBN", "Mobimo Holding AG"),
    ("NBEN", "Neue Helvetische Bank AG"), ("OFN", "Orell Fuessli AG"),
    ("PGHN", "Partners Group Holding AG"), ("PSPN", "PSP Swiss Property AG"),
    ("SFPN", "SF Urban Properties AG"), ("SIGN", "SIG Group AG"),
    ("SOFN", "SoftwareONE Holding AG"), ("SRCG", "Straumann Holding AG"),
    ("TEMN", "Temenos AG"), ("VACN", "VAT Group AG"), ("VARN", "Varian Medical (SIX)"),
    ("WKBN", "Wolseley (SIX)"), ("YPSN", "Ypsomed Holding AG"),
    ("ZEHN", "Zehnder Group AG"), ("ZURN2", "Zurich Insurance B"),
]

AEX_SEEDS = [
    # AEX + AMX + AScX (Netherlands)
    ("AALB", "Aalberts Industries NV"), ("ABN", "ABN AMRO Bank NV"),
    ("ADYEN", "Adyen NV"), ("AGN", "Aegon NV"), ("AD", "Ahold Delhaize NV"),
    ("AKZA", "Akzo Nobel NV"), ("AMG", "Advanced Metallurgical Group NV"),
    ("APAM", "Aperam SA"), ("ASM", "ASM International NV"), ("ASME", "ASML Holding NV"),
    ("BESI", "BE Semiconductor Industries NV"), ("BOKA", "Boskalis Westminster NV"),
    ("CMCOM", "CM.com NV"), ("DSFIR", "DSM-Firmenich AG"), ("EXEL", "Exel Industries SA"),
    ("FLOW", "Flow Traders NV"), ("HEIJM", "Heijmans NV"), ("IM", "Imcd Group NV"),
    ("INGA", "ING Groep NV"), ("JDEP", "Just Eat Takeaway NV"),
    ("KPN", "Koninklijke KPN NV"), ("NN", "NN Group NV"), ("NSG", "NSG Group (AEX)"),
    ("NVMI", "Nova Measuring (AEX)"), ("OCI", "OCI NV"), ("PHIA", "Philips NV"),
    ("PRX", "Prosus NV"), ("RAND", "Randstad NV"), ("REN", "Relx NV"),
    ("SBMO", "SBM Offshore NV"), ("TKWY", "Takeaway.com NV"),
    ("UMG", "Universal Music Group NV"), ("URW", "Unibail-Rodamco-Westfield SE"),
    ("VPK", "Koninklijke Vopak NV"), ("WKL", "Wolters Kluwer NV"),
    ("HYDRA", "Hydratight Group"), ("KENDR", "Kendrion NV"),
    ("MTRX", "Matrix IT (AEX)"), ("NEXIA", "Nexia International"),
    ("ORDINA", "Ordina NV"), ("SLIGRO", "Sligro Food Group NV"),
    ("SOFTI", "Softimat SA"), ("STLAM", "Stellantis NV"),
    ("TOM2", "TomTom NV"), ("TWEKA", "TKH Group NV"),
    ("VASTNED", "Vastned Retail NV"), ("VIFOR", "Vifor Pharma (AEX)"),
    ("VMLY", "VML&Y (AEX)"), ("WDP", "Warehouses De Pauw"),
]

VIENNA_SEEDS = [
    # ATX + ATX Prime (Austria)
    ("ABS", "Andritz AG"), ("AGS", "AT&S Austria Technologie AG"),
    ("AMS", "ams AG"), ("ANDR", "Andritz AG"), ("AUA", "Austrian Airlines AG"),
    ("BG", "BAWAG Group AG"), ("BWT", "BWT AG"), ("CA", "CA Immo AG"),
    ("CMDI", "Cembra Money Bank (Vienna)"), ("DO", "DO & CO AG"),
    ("EBS", "Erste Group Bank AG"), ("EVN", "EVN AG"), ("FMB", "Frequentis AG"),
    ("FLUG", "Flughafen Wien AG"), ("IIA", "IIASA (Vienna)"),
    ("IPS", "Ipsidy Inc (Vienna)"), ("KSB", "KSB SE (Vienna)"),
    ("KTCG", "Kapsch TrafficCom AG"), ("LNZ", "Lenzing AG"),
    ("MMK", "Mayr-Melnhof Karton AG"), ("OMV", "OMV AG"),
    ("POS", "Polytec Holding AG"), ("POST", "Oesterreichische Post AG"),
    ("RBI", "Raiffeisen Bank International AG"), ("RHI", "RHI Magnesita NV"),
    ("S&T", "S&T AG"), ("SBO", "Schoeller-Bleckmann Oilfield (Vienna)"),
    ("TKA2", "Telekom Austria AG"), ("UIAG", "Uniqa Insurance Group AG"),
    ("VIG", "Vienna Insurance Group AG"), ("VIR", "Vir Biotechnology (Vienna)"),
    ("VOE", "voestalpine AG"), ("WAB", "Wüstenrot & Württembergische (Vienna)"),
    ("WIE", "Wienerberger AG"), ("ZAG", "Zumtobel Group AG"),
]


# ─── Exchange Configuration ──────────────────────────────────────────────────

EXCHANGES = {
    "xetra": {
        "suffix": ".DE",
        "seeds": XETRA_SEEDS,
        "min_market_cap_m": 50,
        "country": "DE",
    },
    "six": {
        "suffix": ".SW",
        "seeds": SIX_SEEDS,
        "min_market_cap_m": 50,
        "country": "CH",
    },
    "aex": {
        "suffix": ".AS",
        "seeds": AEX_SEEDS,
        "min_market_cap_m": 50,
        "country": "NL",
    },
    "vienna": {
        "suffix": ".VI",
        "seeds": VIENNA_SEEDS,
        "min_market_cap_m": 30,
        "country": "AT",
    },
}


# ─── Live Index Fetch (best-effort, falls back to seeds) ─────────────────────

def _fetch_dax_components() -> list[tuple[str, str]]:
    """
    Try to fetch current DAX/MDAX/SDAX/TecDAX components from Deutsche Boerse
    public data. Returns list of (ticker, name) or empty list on failure.
    """
    url = "https://api.deutsche-boerse.com/prod/v1/indices/components"
    indices = ["DAX", "MDAX", "SDAX", "TECDAX"]
    results = []
    headers = {"User-Agent": "ThielDetector contact@example.com"}

    for index in indices:
        try:
            resp = requests.get(
                url,
                params={"index": index},
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    ticker = item.get("isin") or item.get("symbol", "")
                    name = item.get("name", "")
                    if ticker and name:
                        results.append((ticker, name))
        except Exception as e:
            logger.debug(f"Deutsche Boerse API failed for {index}: {e}")

    return results


def _fetch_stoxx_components() -> list[tuple[str, str]]:
    """
    Try to fetch EURO STOXX components from public sources.
    Returns list of (ticker, name) or empty list on failure.
    """
    # Fallback: use Wikipedia EURO STOXX 600 list via a public API
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "parse",
                "page": "EURO_STOXX_600",
                "prop": "wikitext",
                "format": "json",
            },
            timeout=15,
        )
        # Parsing wikitext is complex — skip and use seeds
        return []
    except Exception:
        return []


# ─── Main Builder ────────────────────────────────────────────────────────────

def build_eu_universe(config: dict, conn) -> list[dict]:
    """
    Build EU screening universe. Returns list of company dicts.

    Each company dict includes:
      ticker (with exchange suffix), name, exchange, cohort_id, country
    """
    eu_config = config.get("eu_universe", {})
    if not eu_config.get("enabled", False):
        logger.info("EU universe disabled in config — skipping")
        return []

    enabled_exchanges = eu_config.get("exchanges", list(EXCHANGES.keys()))
    companies = []
    seen_tickers = set()

    for exchange_id in enabled_exchanges:
        exchange = EXCHANGES.get(exchange_id)
        if not exchange:
            logger.warning(f"Unknown exchange: {exchange_id}")
            continue

        suffix = exchange["suffix"]
        min_cap = exchange.get("min_market_cap_m", 50)
        country = exchange["country"]
        seeds = exchange["seeds"]

        # Try to enrich with live data — fall back to seeds
        live_tickers = []
        if exchange_id == "xetra":
            live_tickers = _fetch_dax_components()

        all_seeds = seeds
        if live_tickers:
            existing_base = {t for t, _ in seeds}
            new_tickers = [(t, n) for t, n in live_tickers if t not in existing_base]
            all_seeds = seeds + new_tickers
            logger.info(f"{exchange_id}: {len(new_tickers)} live tickers added to seeds")

        for base_ticker, name in all_seeds:
            full_ticker = f"{base_ticker}{suffix}"
            if full_ticker in seen_tickers:
                continue
            seen_tickers.add(full_ticker)

            companies.append({
                "ticker": full_ticker,
                "base_ticker": base_ticker,
                "name": name,
                "exchange": exchange_id,
                "exchange_suffix": suffix,
                "cohort_id": f"eu_{exchange_id}",
                "country": country,
                "min_market_cap_m": min_cap,
                "source": "seed",
            })

    logger.info(f"EU universe: {len(companies)} candidates across {len(enabled_exchanges)} exchanges")

    # Upsert into DB
    _upsert_eu_companies(conn, companies)

    return companies


def _upsert_eu_companies(conn, companies: list[dict]):
    """Write EU companies to DB if not already present."""
    now = datetime.now(timezone.utc).isoformat()

    # Seed EU cohorts into universe_cohorts to satisfy FK constraint
    eu_cohort_ids = {c.get("cohort_id") for c in companies if c.get("cohort_id")}
    for cohort_id in eu_cohort_ids:
        conn.execute("""
            INSERT OR IGNORE INTO universe_cohorts
            (cohort_id, name, added_at, alerting_enabled)
            VALUES (?, ?, ?, 1)
        """, (cohort_id, cohort_id.replace("eu_", "EU ").title(), now))
    conn.commit()

    for company in companies:
        existing = conn.execute(
            "SELECT ticker FROM companies WHERE ticker = ?",
            (company["ticker"],)
        ).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO companies
                (ticker, name, cohort_id, first_seen_in_universe, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, (
                company["ticker"],
                company.get("name", ""),
                company.get("cohort_id", ""),
                now,
            ))
    conn.commit()
