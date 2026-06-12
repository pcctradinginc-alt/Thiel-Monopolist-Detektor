# Thiel Monopolist Detector

Ein automatisiertes Screening-System, das börsennotierte US-Unternehmen auf Anzeichen eines Thiel-artigen Monopols untersucht und per E-Mail alertet.

## Was dieses System ist — und was nicht

**Was es ist:** Ein Aufmerksamkeits-Filter. Das System sucht systematisch nach Anomalien, die auf einen frühen, noch verborgenen Moat hindeuten — und liefert 3–5 Kandidaten pro Monat, die eine tiefere menschliche Analyse rechtfertigen.

**Was es nicht ist:** Ein autonomer Entscheider. Die finale Einschätzung "Das ist ein Thiel-Monopolist" bleibt beim Menschen.

## Thiel's vier Kriterien

1. **Proprietary Technology** — mindestens 10x besser als das nächste Substitut in einer wichtigen Dimension
2. **Network Effects** — das Produkt wird wertvoller, je mehr Menschen es nutzen
3. **Economies of Scale** — das Unternehmen wird stärker, je größer es wird (Fixkosten hoch, Grenzkosten ≈ 0)
4. **Branding** — echte, dauerhafte Markenidentität (allein nicht ausreichend)

## Architektur

```
Universe (Lanes) 
  → Evidence Collection (edgartools + yfinance + SEC EDGAR)
    → market_hypothesis_generator (LLM Schritt 1)
      → Widerspruchs-Detektion (Risk Factors vs. Business Description vs. Zahlen)
        → Substitute-Analyse (LLM Schritt 2)
          → Scoring mit Evidence + Counter-Evidence (LLM Schritt 3)
            → SQLite / Turso
              → GitHub Issue Alert
                → Human Feedback → Recalibration
```

## Candidate Lanes (kein harter Ausschluss, nur Priorisierung)

| Lane | Kriterium |
|------|-----------|
| `hidden_wedge` | Enger Use Case, wenige Substitute, starke Kundenbindung |
| `emerging_platform` | Plattform/Ökosystem-Sprache + Lock-in-Signale |
| `scale_inflection` | Kostenbasis skaliert besser als Umsatz (S&M fällt, Margin steigt) |
| `ipo_narrow` | Junges Unternehmen, enge Marktdefinition, S-1 Analyse |
| `filing_change` | Neue Keywords in 10-K: platform, ecosystem, retention, standard |
| `manual_watchlist` | Manuell gepinnte Unternehmen |

## Scores

- `monopoly_score` (0–100): Stärke der Moat-Evidenz
- `confidence_score` (0–100): Datensicherheit des Systems
- `data_quality_score` (0–100): Vollständigkeit der Datenlage

Alert-Schwelle: alle drei Scores > 65 + Bestätigung in ≥ 2 aufeinanderfolgenden Runs

## Wöchentlicher Kaufkandidaten-Report

Unabhängig von Alert-Schwellen nennt das System nach jedem Batch-Lauf die
**Top 15 Kandidaten** (US + EU) per GitHub Issue (Label `weekly-report`) und
E-Mail — mit Scores, Status, Kurzthese und engem Markt. Damit liefert jeder
Wochenlauf konkrete Namen für die menschliche Tiefenanalyse, auch wenn kein
Kandidat die harten Alert-Schwellen reißt.

Jeder Kandidat bekommt zusätzlich ein **Einstiegssignal** (kostenlos via
yfinance): Preis, Abstand zum 52-Wochen-Hoch, P/S und eine regelbasierte
Einstufung —

| Einstufung | Bedeutung |
|------------|-----------|
| `KAUFFENSTER` | Bestätigter Moat (≥2 Runs ≥65) + Rücksetzer ≥15 % oder moderates P/S + Wachstum intakt |
| `QUALITAET_TEUER` | Bestätigter Moat, aber nahe Hoch und hohe Bewertung — auf Rücksetzer warten |
| `THESE_PRUEFEN` | Bestätigter Moat, aber Umsatz/Marge widersprechen der These |
| `WATCH` | Moat noch nicht über 2 Wochen bestätigt |

Dazu trackt der Report die **Performance aller offenen Signale** seit
Signalzeitpunkt (Kurs damals vs. jetzt) — der Feedback-Loop, ob die Signale
tatsächlich Geld verdient hätten. Alles regelbasierte Priorisierung, keine
Anlageberatung.

## Persistenz

Die SQLite-DB wird nach jedem Lauf als GitHub-Release-Asset `db-latest`
gesichert und beim nächsten Lauf wiederhergestellt. Nur so funktionieren
Rotation (2/8-Wochen-Tiers), die ≥2-Runs-Bestätigung und die Alert-Hysterese
über Wochen hinweg. EU-Kandidaten haben eine feste Quote von 25 % des
LLM-Call-Budgets pro Lauf.

**Run-Plan (getrennte Crons statt 6h-Sleep — GitHub-Jobs haben ein 6h-Limit):**
- So 06:00 UTC: `batch_submit` (Universe, Filings, Batch einreichen)
- So 16:00 / So 20:00 / Mo 06:00 UTC: `batch_collect` (idempotent — Backups
  sind no-ops, sobald der Batch eingesammelt ist)

**Recall-Garantien:** Nie evaluierte Firmen passieren mit relaxtem Lane-Gate
(`baseline_lane_score: 25`) — das Keyword-Gate kann einen Monopolisten ohne
Plattform-Vokabular nicht dauerhaft aussperren. Neue Filings triggern
Re-Analyse (Rotation-Trigger via `last_filing_date`).

**EU-Datenquellen (alle kostenlos):** EODHD-Symbol-Listen (~3.500 Aktien über
13 Börsen inkl. London/Mailand/Madrid) statt nur kuratierter Seeds.
Filing-Texte in dieser Reihenfolge: Bundesanzeiger (DE) → Companies House (UK)
→ **ESEF-Geschäftsberichte** via filings.xbrl.org + GLEIF-LEI-Lookup (alle EU,
echte Berichtsabschnitte: Geschäftsmodell, Risikobericht, Lagebericht) →
Wikipedia-Fallback. Firmen mit < 3 Analysten (`numberOfAnalystOpinions`)
bekommen einen Lane-Boost (`under_followed`) — dort ist Fehlbepreisung
strukturell am wahrscheinlichsten.

**Kosten-Optimierung:** Low-Scorer (< 40) mit Bewertung jünger als 120 Tage
werden übersprungen; Filing-Snapshots jünger als 45 Tage werden aus der DB
wiederverwendet statt EDGAR neu zu parsen (10-Ks sind jährlich). Das freie
Budget fließt in noch nie gesehene Namen.

## Alert-Typen

```
HIDDEN_WEDGE_DETECTED
SUBSTITUTE_GAP_DETECTED
LOCK_IN_STRENGTHENING
SCALE_INFLECTION
CUSTOMER_EXPANSION_SIGNAL
IPO_WITH_NARROW_DOMINANCE
MOAT_EVIDENCE_IMPROVED
MOAT_RISK_DETECTED
```

## Setup

### 1. Repository klonen

```bash
git clone https://github.com/pcctradinginc-alt/Thiel-Monopolist-Detektor.git
cd Thiel-Monopolist-Detektor
pip install -r requirements.txt
```

### 2. Secrets in GitHub setzen

| Secret | Beschreibung |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API Key (Haiku für Screening, Sonnet für finale Kandidaten) |
| `EMAIL_SENDER` | Gmail-Adresse für Alerts |
| `EMAIL_PASSWORD` | Gmail App-Passwort |
| `EMAIL_RECIPIENT` | Empfänger-Adresse |
| `SEC_CONTACT_EMAIL` | Echte Kontakt-E-Mail für den SEC-EDGAR-User-Agent (von der SEC für automatisierte Abrufe verlangt — Platzhalter riskieren IP-Sperren) |
| `TURSO_URL` | (Optional) Turso DB URL für Produktion |
| `TURSO_TOKEN` | (Optional) Turso Auth Token |

### 3. Konfiguration anpassen

```bash
cp config/config.example.yaml config/config.yaml
# Anpassen nach Bedarf
```

### 4. Erster Run (lokal)

```bash
python src/main.py --mode full --dry-run
```

## GitHub Actions

Das System läuft automatisch jeden **Montag um 06:00 UTC**.

- `screening.yml` — Wöchentliches Screening
- `feedback.yml` — Verarbeitet GitHub Issue Labels als Human Feedback

## Human Feedback

Alerts werden als GitHub Issues erstellt. Labels:
- `confirmed` — Echter Moat bestätigt
- `rejected` — False Positive
- `watchlist` — Weiter beobachten
- `too-early` — Zu früh, nochmal in 6 Monaten

## Datenbankschema

Siehe `docs/schema.md`

## Kosten-Schätzung

| Komponente | Kosten/Woche |
|------------|-------------|
| Claude Haiku (Screening ~100 Calls) | ~$0.20 |
| Claude Sonnet (Finale ~10 Calls) | ~$0.50 |
| Turso Free Tier | $0 |
| GitHub Actions | $0 |
| **Gesamt** | **~$0.70/Woche** |

## Lizenz

MIT
