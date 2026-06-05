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

    # ── Hidden Champions — nicht in Indices aber hohe Thiel-Relevanz ──
    # B2B-Software mit hohen Switching Costs, Nischenmarktführer, Family-owned
    ("AOF", "Atoss Software AG"),          # HR-Software DACH, ~80% GM, starker Lock-in
    ("HYQ", "Hypoport SE"),                # Kredit-Plattform für dt. Banken
    ("PSI", "PSI Software SE"),            # Energie/Industrie ERP, hohe Switching Costs
    ("ADN1", "Adesso SE"),                 # IT-Consulting Branchen-Spezialist
    ("GFT", "GFT Technologies SE"),        # Fintech-IT, tief in Banken integriert
    ("NA9", "Nagarro SE"),                 # Software Engineering Platform
    ("RAA", "RATIONAL AG"),               # Profi-Küchentech, 50%+ Weltmarktanteil
    ("KTN", "Kontron AG"),                # Embedded Computing B2B
    ("NFON", "NFON AG"),                  # Cloud-Telefonie KMU
    ("CLIQ", "CLIQ Digital AG"),          # Digital-Abo Nischenplattform
    ("DBO", "Drägerwerk AG"),             # Medizin/Sicherheitstechnik, Mission-Critical
    ("SBS", "Stratec SE"),                # Laborautomation OEM, Sole-Source-Lieferant
    ("SMH", "Schmolz+Bickenbach AG"),     # Spezialstahl, Nischenmarktführer
    ("ECK", "Eckert & Ziegler AG"),       # Radioaktive Isotope, regulatorischer Moat
    ("MBB", "MBB SE"),                    # Beteiligungsges. Hidden Champions
    ("ZEG", "Zeal Network SE"),           # Lotterie-Plattform DE, Monopol
    ("SBSPA", "Sto SE"),                  # Fassadensysteme, Marktführer DE
    ("PSAN", "ProSiebenSat.1 Media SE"),  # Medien-Plattform
    ("FNTN", "freenet AG"),               # Mobilfunk-Plattform, Kundenbindung
    ("PSH", "PSI Software SE"),           # (doppelt, ok)
    ("PGN", "Paragon GmbH & Co"),         # Automotive Elektronik OEM
    ("HOT", "Hochtief AG"),               # Bau-Spezialist
    ("DIC", "DIC Asset AG"),              # Gewerbeimmobilien-Plattform
    ("PAYX", "Paychex (XETRA)"),          # Payroll-SaaS
    ("HDD2", "Hella GmbH"),               # Automotive-Tech, OEM-Integration
    ("KGX", "Kion Group AG"),             # Intralogistik-Software + Hardware
    ("DBAN", "Deutsche Beteiligungs AG"), # PE für dt. Mittelstand
    ("FPH", "flatexDEGIRO AG"),          # Broker-Plattform, Lock-in durch Depot
    ("IOS", "IONOS Group SE"),            # Cloud-Hosting KMU, Switching-Cost-Modell
    ("RENK", "Renk Group AG"),            # Getriebespezialist Verteidigung/Marine
    ("MELE", "Medios AG"),               # Pharma-Spezialkompensation, reguliert
    ("SKB", "Skan Group AG"),            # Pharma-Isolatoren, Marktführer
    ("YPSN2", "Ypsomed Holding AG"),     # Drug-Delivery Systeme, OEM-Lock-in

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
    "paris": {
        "suffix": ".PA",
        "seeds": [
            # CAC 40 + SBF 120 highlights (high Thiel potential)
            ("AI", "Air Liquide SA"), ("AIR", "Airbus SE"), ("ALO", "Alstom SA"),
            ("ATO", "Atos SE"), ("AXA", "AXA SA"), ("BN", "Danone SA"),
            ("BNP", "BNP Paribas SA"), ("CA", "Carrefour SA"), ("CAP", "Capgemini SE"),
            ("CS", "AXA SA"), ("DSY", "Dassault Systemes SE"), ("ENGI", "Engie SA"),
            ("EDEN", "Edenred SE"), ("ERF", "Eurofins Scientific SE"),
            ("GLE", "Societe Generale SA"), ("HO", "Thales SA"), ("KER", "Kering SA"),
            ("LR", "Legrand SA"), ("MC", "LVMH Moet Hennessy SE"), ("ML", "Michelin SA"),
            ("OR", "L'Oreal SA"), ("ORA", "Orange SA"), ("PUB", "Publicis Groupe SA"),
            ("RMS", "Hermes International SCA"), ("SAF", "Safran SA"),
            ("SAN", "Sanofi SA"), ("SGO", "Compagnie de Saint-Gobain SA"),
            ("STLAM", "Stellantis NV"), ("SU", "Schneider Electric SE"),
            ("TTE", "TotalEnergies SE"), ("VIE", "Veolia Environnement SA"),
            ("VIV", "Vivendi SE"), ("WLN", "Worldline SA"),
            # SBF 120 additions
            ("ALFEN", "Alfen NV"), ("ALSTEF", "Alstef Group"),
            ("AMUN", "Amundi SA"), ("APAM", "Aperam SA"),
            ("BFCM", "Credit Mutuel SA"), ("BIOC", "Biocartis Group NV"),
            ("COFA", "Coface SA"), ("CRDI", "Credit Agricole SA"),
            ("DBV", "DBV Technologies SA"), ("DDOG", "Datadog (Paris)"),
            ("EDF", "Electricite de France SA"), ("ELIS", "Elis SA"),
            ("FDJ", "Francaise des Jeux SA"), ("FTI", "TechnipFMC PLC"),
            ("GTT", "GTT SA"), ("ITRK", "Intertek Group (Paris)"),
            ("KOF", "Korian SA"), ("LHN", "LafargeHolcim Ltd"),
            ("NEOEN", "Neoen SA"), ("NEXITY", "Nexity SA"),
            ("NOKIA", "Nokia Oyj (Paris)"), ("OPM", "OPmobility SE"),
            ("PARRO", "Parrot SA"), ("PERNOD", "Pernod Ricard SA"),
            ("POOL", "Poolia AB (Paris)"), ("REXEL", "Rexel SA"),
            ("RNO", "Renault SA"), ("SCOR", "SCOR SE"),
            ("SOI", "Soitec SA"), ("SPIE", "SPIE SA"),
            ("STM", "STMicroelectronics NV"), ("TKTT", "Tikehau Capital"),
            ("VLTSA", "Vallourec SA"), ("VK", "Voltalia SA"),
        ],
        "min_market_cap_m": 100,
        "country": "FR",
    },
}


# ─── Live Index Fetch (best-effort, falls back to seeds) ─────────────────────

def _fetch_xetra_live() -> list[tuple[str, str]]:
    """
    Fetch all XETRA-listed equities from Deutsche Börse's public instruments
    reference data. Falls back to index components if bulk download fails.
    Returns list of (base_ticker, name).
    """
    results = []
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

    # Strategy 1: Deutsche Börse index components API (reliable, ~600 stocks)
    indices = ["DAX", "MDAX", "SDAX", "TECDAX", "SDAX"]
    index_url = "https://api.deutsche-boerse.com/prod/v1/indices/components"
    seen = set()
    for index in indices:
        try:
            resp = requests.get(index_url, params={"index": index},
                                headers=headers, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("data", []):
                    sym = item.get("symbol", "")
                    name = item.get("name", "")
                    if sym and name and sym not in seen:
                        seen.add(sym)
                        results.append((sym, name))
                logger.info(f"Deutsche Börse {index}: {len(results)} total so far")
        except Exception as e:
            logger.debug(f"DB index API failed for {index}: {e}")

    # Strategy 2: Wikipedia index tables via HTML (more reliable than wikitext)
    import re
    wiki_pages = ["DAX", "MDAX", "SDAX", "TecDAX"]
    for wiki_page in wiki_pages:
        try:
            resp = requests.get(
                f"https://en.wikipedia.org/wiki/{wiki_page}",
                headers=headers, timeout=15
            )
            if resp.status_code != 200:
                continue
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            # Wikipedia index tables: look for wikitable rows with ticker symbols
            for table in soup.select("table.wikitable"):
                headers_row = table.select("th")
                header_texts = [th.get_text(strip=True).lower() for th in headers_row]
                # Find ticker column index
                ticker_col = next((i for i, h in enumerate(header_texts)
                                   if any(k in h for k in ["ticker", "symbol", "kürzel", "isin"])), None)
                name_col = next((i for i, h in enumerate(header_texts)
                                 if any(k in h for k in ["company", "name", "unternehmen"])), 0)
                for row in table.select("tr")[1:]:
                    cells = row.select("td")
                    if not cells:
                        continue
                    name = cells[name_col].get_text(strip=True) if name_col < len(cells) else ""
                    # Extract ticker: prefer known column, else find 2-6 uppercase chars
                    sym = ""
                    if ticker_col is not None and ticker_col < len(cells):
                        sym = cells[ticker_col].get_text(strip=True)
                    if not sym or not re.match(r'^[A-Z0-9]{2,6}$', sym):
                        # Try to find ticker in any cell
                        for cell in cells:
                            t = cell.get_text(strip=True)
                            if re.match(r'^[A-Z]{2,6}[0-9]?$', t) and t not in seen:
                                sym = t
                                break
                    name = re.sub(r'\[.*?\]', '', name).strip()
                    # Filter out noise: currency codes, generic words, too short
                    noise = {"EUR", "USD", "GBP", "CHF", "TBD", "AG", "SE", "N/A", "NA"}
                    if (sym and name and len(name) > 3
                            and re.match(r'^[A-Z]{2,6}[0-9]?$', sym)
                            and sym not in seen
                            and sym not in noise):
                        seen.add(sym)
                        results.append((sym, name))
        except Exception as e:
            logger.debug(f"Wikipedia HTML fetch failed for {wiki_page}: {e}")

    logger.info(f"XETRA live fetch: {len(results)} unique tickers")
    return results


def _fetch_euronext_live(mics: list[str], suffix_map: dict) -> list[tuple[str, str, str]]:
    """
    Fetch equities from Euronext markets via their public data service.
    Returns list of (base_ticker, name, exchange_suffix).
    mics: list of MIC codes e.g. ["XAMS", "XPAR"]
    suffix_map: {mic: yfinance_suffix} e.g. {"XAMS": ".AS", "XPAR": ".PA"}
    """
    results = []
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}

    for mic in mics:
        suffix = suffix_map.get(mic, ".AS")
        try:
            # Euronext public product list (paginated JSON)
            url = f"https://live.euronext.com/en/pd_ajax/stocks?mics={mic}&start=0&length=2000"
            resp = requests.get(url, headers={
                **headers,
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://live.euronext.com/en/products/equities/list",
            }, timeout=20)

            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("data", []):
                    # Euronext returns HTML fragments — extract ticker from link
                    import re
                    ticker_match = re.search(r'>([A-Z0-9]{2,8})<', str(item))
                    name_match = re.search(r'<td[^>]*>([^<]{5,60})</td>', str(item))
                    if ticker_match:
                        ticker = ticker_match.group(1)
                        name = name_match.group(1).strip() if name_match else ticker
                        results.append((ticker, name, suffix))
                logger.info(f"Euronext {mic}: fetched {len([r for r in results if r[2]==suffix])} tickers")
            else:
                logger.debug(f"Euronext {mic} HTTP {resp.status_code}")
        except Exception as e:
            logger.debug(f"Euronext {mic} fetch failed: {e}")

    return results


def _fetch_dax_components() -> list[tuple[str, str]]:
    """Legacy wrapper — calls _fetch_xetra_live."""
    return _fetch_xetra_live()


def _fetch_stoxx_components() -> list[tuple[str, str]]:
    """Legacy stub — kept for compatibility."""
    return []


# ─── EU IPO Fetchers ─────────────────────────────────────────────────────────

def fetch_eu_ipos(months_back: int = 18) -> list[dict]:
    """
    Fetch recent EU IPOs from Deutsche Börse and Euronext new listings.
    Returns list of company dicts ready for the screening pipeline.

    Sources:
      - Deutsche Börse: neue Zulassungen (XETRA)
      - Euronext: new listings (AEX + Paris)
      - BaFin prospectus database (for German IPOs with text)

    months_back: how far back to look for new listings
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=months_back * 30)
    headers = {"User-Agent": "ThielDetector info@pcctradinginc.com"}
    ipos = []
    seen = set()

    # ── Source 1: Deutsche Börse new listings RSS/API ─────────────────────────
    try:
        # Deutsche Börse publishes new XETRA listings via their public API
        resp = requests.get(
            "https://api.deutsche-boerse.com/prod/v1/instrument/newlistings",
            params={"limit": 200},
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                sym = item.get("symbol", "")
                name = item.get("name", "")
                listing_date = item.get("listingDate", "")
                if sym and sym not in seen:
                    seen.add(sym)
                    ipos.append({
                        "ticker": f"{sym}.DE",
                        "base_ticker": sym,
                        "name": name,
                        "exchange": "xetra",
                        "exchange_suffix": ".DE",
                        "cohort_id": "eu_ipo",
                        "country": "DE",
                        "source": "ipo",
                        "ipo_date": listing_date,
                        "has_prospectus": False,
                    })
            logger.info(f"Deutsche Börse new listings: {len(ipos)} IPOs")
    except Exception as e:
        logger.debug(f"Deutsche Börse new listings failed: {e}")

    # ── Source 2: Euronext new listings ───────────────────────────────────────
    for mic, suffix, country in [("XAMS", ".AS", "NL"), ("XPAR", ".PA", "FR")]:
        try:
            resp = requests.get(
                "https://live.euronext.com/en/pd_ajax/ipos",
                params={"mics": mic, "start": 0, "length": 200},
                headers={**headers, "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
            )
            if resp.status_code == 200:
                import re
                for item in resp.json().get("data", []):
                    item_str = str(item)
                    ticker_m = re.search(r'>([A-Z0-9]{2,8})<', item_str)
                    name_m = re.search(r'<td[^>]*>([^<]{5,60})</td>', item_str)
                    if ticker_m:
                        sym = ticker_m.group(1)
                        full = f"{sym}{suffix}"
                        if full not in seen:
                            seen.add(full)
                            ipos.append({
                                "ticker": full,
                                "base_ticker": sym,
                                "name": name_m.group(1).strip() if name_m else sym,
                                "exchange": mic.lower(),
                                "exchange_suffix": suffix,
                                "cohort_id": "eu_ipo",
                                "country": country,
                                "source": "ipo",
                                "has_prospectus": False,
                            })
        except Exception as e:
            logger.debug(f"Euronext {mic} IPO fetch failed: {e}")

    # ── Source 3: BaFin Prospektdatenbank (German IPO prospectuses) ───────────
    try:
        resp = requests.get(
            "https://www.bafin.de/SiteGlobals/Functions/Prospekte/DE/prospektsuche.html",
            params={
                "gtp": "134578_list%3D1",
                "documentType": "PROSP",
                "language": "DE",
                "sorting": "dateOfApproval_dt+desc",
                "resultsPerPage": "100",
            },
            headers=headers,
            timeout=20,
        )
        if resp.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            for row in soup.select("table tr")[1:51]:  # top 50 entries
                cells = row.select("td")
                if len(cells) >= 3:
                    name = cells[0].get_text(strip=True)
                    # BaFin doesn't always have ticker — flag for manual lookup
                    if name and len(name) > 3:
                        ipos.append({
                            "ticker": f"BAFIN_{name[:10].replace(' ','_').upper()}.DE",
                            "base_ticker": name[:10].replace(' ', '_').upper(),
                            "name": name,
                            "exchange": "xetra",
                            "exchange_suffix": ".DE",
                            "cohort_id": "eu_ipo_bafin",
                            "country": "DE",
                            "source": "bafin_prospectus",
                            "has_prospectus": True,
                        })
            logger.info(f"BaFin prospectus database: added {len([i for i in ipos if i.get('source')=='bafin_prospectus'])} entries")
    except Exception as e:
        logger.debug(f"BaFin prospectus fetch failed: {e}")

    logger.info(f"EU IPOs total: {len(ipos)} across all sources")
    return ipos


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
            live_tickers = _fetch_xetra_live()
        elif exchange_id == "aex":
            euronext_results = _fetch_euronext_live(["XAMS"], {"XAMS": ".AS"})
            live_tickers = [(t, n) for t, n, _ in euronext_results]
        elif exchange_id == "paris":
            euronext_results = _fetch_euronext_live(["XPAR"], {"XPAR": ".PA"})
            live_tickers = [(t, n) for t, n, _ in euronext_results]

        all_seeds = seeds
        if live_tickers:
            existing_base = {t for t, _ in seeds}
            new_tickers = [(t, n) for t, n in live_tickers if t not in existing_base]
            all_seeds = seeds + new_tickers
            logger.info(f"{exchange_id}: {len(new_tickers)} live tickers added to {len(seeds)} seeds → {len(all_seeds)} total")

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

    logger.info(f"EU universe (seeds+live): {len(companies)} candidates across {len(enabled_exchanges)} exchanges")

    # ── FMP: bulk EU stock list (1 API call, ~1000+ EU tickers) ──────────────
    import os
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if fmp_key:
        from universe.fmp_universe import fetch_fmp_eu_universe
        fmp_companies = fetch_fmp_eu_universe(fmp_key)
        fmp_added = 0
        for company in fmp_companies:
            if company["ticker"] not in seen_tickers:
                seen_tickers.add(company["ticker"])
                companies.append(company)
                fmp_added += 1
        logger.info(f"FMP added {fmp_added} new EU tickers (total now: {len(companies)})")
    else:
        logger.info("FMP_API_KEY not set — using seeds only for EU universe")

    # ── EU IPOs: add recent IPOs as high-priority candidates ─────────────────
    ipo_months = eu_config.get("ipo_months_back", 18)
    ipo_candidates = fetch_eu_ipos(months_back=ipo_months)
    ipo_added = 0
    for ipo in ipo_candidates:
        full_ticker = ipo["ticker"]
        # Skip BaFin entries without real ticker (can't yfinance-lookup them)
        if ipo.get("source") == "bafin_prospectus":
            continue
        if full_ticker not in seen_tickers:
            seen_tickers.add(full_ticker)
            companies.append(ipo)
            ipo_added += 1
    if ipo_added:
        logger.info(f"EU IPOs added: {ipo_added} recent listings (last {ipo_months} months)")

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
