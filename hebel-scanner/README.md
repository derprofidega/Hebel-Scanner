# Hebel-Scanner

Regelbasierter Tages-Scan für Swing-Trades mit **Knock-out-Zertifikaten** auf US- und deutsche Standardwerte.
An jedem Handelstag findet er **0 bis 3 Kandidaten** – jeweils mit Begründung, Einstiegszeit, Limit, Stop,
Ziel, passendem KO-Barrieren-Korridor und Stückzahl. Jedes Signal landet automatisch in einem
**Paper-Journal**, sodass das System selbst misst, ob seine Regeln funktionieren.

> **Vorab, ehrlich:** Das System sagt keine Kurse voraus. Es filtert Situationen, in denen die
> Chance-Risiko-Struktur laut Regelwerk günstig ist, und legt den Verlust **vor** dem Einstieg fest.
> Ob die Regeln einen Vorteil haben, zeigt erst der Backtest und danach der Forward-Test.
> Keine Anlageberatung. Hebelprodukte können wertlos verfallen.

---

## Inhalt

1. [So läuft ein Tag](#1-so-läuft-ein-tag)
2. [Die Regeln](#2-die-regeln)
3. [Zur Idee „Quartalszahlen vorab spielen“](#3-zur-idee-quartalszahlen-vorab-spielen)
4. [Einrichtung (ohne Programmieren)](#4-einrichtung-ohne-programmieren)
5. [Tagesroutine: Report lesen und handeln](#5-tagesroutine-report-lesen-und-handeln)
6. [Freigabe-Stufen: vom Test zum echten Geld](#6-freigabe-stufen-vom-test-zum-echten-geld)
7. [Anpassen](#7-anpassen)
8. [Lokal statt GitHub ausführen](#8-lokal-statt-github-ausführen)
9. [Grenzen – bitte lesen](#9-grenzen--bitte-lesen)
10. [Häufige Fragen](#10-häufige-fragen)

---

## 1. So läuft ein Tag

| Wann | Was passiert |
|---|---|
| Mo–Fr, 22:30 UTC (00:30 Uhr Sommerzeit / 23:30 Uhr Winterzeit) | GitHub startet den Scan automatisch |
| ca. 3–6 Minuten | Tageskurse für ≈ 540 Titel (S&P 500 + DAX-Umfeld), EUR/USD, US-Earnings-Kalender laden |
| | Offene Paper-Trades fortschreiben (Einstieg, Stop, Teilgewinn, Zeitstop) |
| | Marktampel → Setups → Filter → Rangfolge → **0–3 Kandidaten** |
| | Report `reports/latest.md` (+ Archiv `reports/JJJJ/`), Telegram-Nachricht |
| Nächster Handelstag | **Du:** Produkt suchen und Order legen – oder nichts tun |

„Kein Kandidat“ ist ein häufiges und richtiges Ergebnis. Das System erzwingt keine Trades.

---

## 2. Die Regeln

Alle Werte stehen in `config.yaml`. Backtest und Tages-Scan nutzen exakt denselben Code.

### Marktampel (gilt für alles)
Neue Long-Trades nur, wenn der **Heimatindex über seiner 200-Tage-Linie** schließt
(S&P 500 für US-Titel, DAX für deutsche Titel).

### Setup A – Trend-Rücksetzer (Modus: `trade`)
Alle Bedingungen am Tagesschluss des Signaltags:

1. **Trend:** Schluss über der 200-Tage-Linie, 50-Tage-Linie höher als vor 10 Handelstagen.
2. **Stärke:** Überrendite ggü. Heimatindex über ≈ 6 Monate (ohne die letzten 5 Tage) unter den **besten 30 %** des Universums.
3. **Rücksetzer:** mindestens **3 Schlusskurse in Folge tiefer**, Tief bis in die Nähe der 20-Tage-EMA (≤ 0,5 ATR darüber oder darunter), Vortagesschluss noch über der 50-Tage-Linie.
4. **Umkehr:** Schluss **über dem Vortageshoch**, in der oberen Hälfte der Tagesspanne.
5. **Qualität:** Kurs ≥ 10, Ø-Tagesumsatz ≥ 20 Mio., ATR ≤ 5 % des Kurses.

### Setup B – Earnings-Gap (Modus: `observe`)
Handel **nach** Quartalszahlen, wenn der Markt seine Richtung schon gezeigt hat:
Eröffnung ≥ 0,5 ATR über Vortagesschluss, Tagesplus ≥ 2 ATR, Volumen ≥ 2,5× Durchschnitt,
Schluss in der oberen Tageshälfte, Sprung ≤ 25 % (größere sind oft Übernahmen), Kurs über der 200-Tage-Linie.
Der Report prüft, ob tatsächlich Zahlen der Anlass waren, und zeigt die EPS-Überraschung.

**Warum nur beobachten?** Die Forschung zum Nachlaufen nach Zahlen („Post-Earnings-Drift“) zeigt, dass dieser
Effekt bei großen Aktien seit Mitte der 2000er stark geschrumpft ist. Deshalb muss sich das Setup erst im
Backtest **und** im Forward-Test beweisen. Bis dahin wird jedes Signal als Paper-Trade mitgeschrieben,
belegt aber kein Risiko-Budget. Freischalten: in `config.yaml` `earnings_gap: trade`.

### Auswahl, wenn es mehr Signale als Plätze gibt
- Rangfolge: relative Stärke, dann Qualität der Umkehrkerze.
- Höchstens **3 neue** pro Tag und **4 Positionen mit offenem Risiko** gleichzeitig.
- Keine neue Position, wenn die 60-Tage-Korrelation zu einer offenen > 0,7 ist (sonst doppeltes Risiko).
- Rücksetzer-Trades: **keine Quartalszahlen** innerhalb von 16 Kalendertagen nach dem Einstieg.
- Keine Einstiege an Tagen aus `events.csv` (Fed, EZB, US-Inflation).

### Einstieg
Am nächsten Handelstag, **15 Minuten nach Börseneröffnung** des Basiswerts (US-Titel: meist 15:45 Uhr,
in den Wochen der Zeitumstellung 14:45 Uhr – der Report rechnet das aus). Nur per **Limit**:
Limit = Signalschluss + 0,25 ATR. Läuft der Kurs darüber weg, verfällt der Trade.

### Ausstieg (steht vorher fest)
- **Stop:** Setup A unter dem 5-Tage-Tief − 0,5 ATR · Setup B unter dem Tief des Reaktionstags − 0,25 ATR.
- **Ziel 1 = +2 R:** Hälfte verkaufen, Rest-Stop mindestens auf Einstand.
- **Rest:** Stop täglich unter das 2-Tages-Tief nachziehen, spätestens nach 30 Handelstagen raus.
- **Zeitstop:** Ist Ziel 1 nach 10 Handelstagen nicht erreicht, zum Schlusskurs raus.

### Produkt und Stückzahl
Reihenfolge immer **Stop → Barriere → Stückzahl**, nie „Hebel aussuchen“.

- **Barriere-Korridor:** mindestens 1 ATR unter dem Stop (normales Rauschen knockt nicht aus) und höchstens so tief,
  dass ein Knock-out per Kurslücke **maximal 2 R** kostet. Der Report nennt Korridor, Idealwert, Abstand in % und Hebel.
- **1 R** = Kontogröße × Risiko je Trade (z. B. 10.000 € × 0,25 % = 25 €).
- **Stückzahl** = 1 R ÷ (Stopabstand × Bezugsverhältnis ÷ EUR/USD). Bei Euro-Titeln entfällt EUR/USD.
  Beispiel: Stopabstand 5 $, Bezugsverhältnis 0,1, EUR/USD 1,10 → 0,4545 € je Zertifikat → 25 € ÷ 0,4545 € = 55 Stück.
- **Max. Spread** je Zertifikat: 5 % des Stopabstands – sonst lieber ein anderes Produkt.

Bei **Mini Futures** zählt die Stop-Loss-/Knock-out-Schwelle, nicht der Basispreis.

---

## 3. Zur Idee „Quartalszahlen vorab spielen“

Die naheliegende Idee – Zahlen-Termine suchen, frühere Reaktionen auswerten, eine Tendenz ableiten und vorher
mit Hebel einsteigen – baut der Scanner bewusst **nicht** als Handelssignal. Gründe:

1. **Die Richtung ist vor den Zahlen kaum vorhersagbar.** Entscheidend ist die Überraschung gegenüber den
   Erwartungen und der Ausblick – nicht, ob die Firma die Schätzung schlägt. Früher „oft gestiegen“ sagt über das
   nächste Mal wenig. Große Häuser mit viel mehr Daten kämpfen um genau diesen Vorteil.
2. **Knock-outs und Kurslücken passen nicht zusammen.** Über Nacht gibt es keinen Stop. Bewegungen von 8–15 %
   sind bei Zahlen normal – ein KO mit ~10 % Barriereabstand ist dann bei falscher Richtung komplett weg.
   Das ist ein gehebelter Münzwurf mit Totalverlust-Seite.
3. **Der Optionsmarkt preist die Bewegung bereits ein.** Wer vorher Optionen kauft, zahlt diese Erwartung mit.

Was der Scanner stattdessen tut:

- **Earnings-Radar** im Report: wer in den nächsten Tagen berichtet, Ø-Reaktion der letzten Quartale,
  Verteilung ↑/↓, größte Bewegung, **vom Optionsmarkt erwartete Bewegung** (US), und wie oft ein
  KO mit Hebel 10 (long oder short) am Reaktionstag **ausgeknockt** worden wäre. Das zeigt je Titel konkret,
  wie teuer die „Vorab-Wette“ werden kann.
- **„Zuletzt berichtet“:** Reaktion vs. EPS-Überraschung – man sieht schnell, dass ein Beat nicht automatisch steigt.
- **Earnings-Gap-Setup:** handelt die Zahlen *danach*, mit definiertem Stop unter dem Reaktionstag – zunächst nur als Paper-Trade.
- **Schutzfilter:** Rücksetzer-Trades werden nie über Zahlen gehalten.

---

## 4. Einrichtung (ohne Programmieren)

Du brauchst: ein kostenloses GitHub-Konto, optional Telegram. Dauer ca. 20–30 Minuten.

**Schritt 1 – Repository anlegen.** Auf github.com oben rechts **+ → New repository**, Name z. B. `hebel-scanner`,
**Private** wählen, *Create repository*.

**Schritt 2 – Dateien hochladen.** ZIP entpacken. Im neuen Repository auf **uploading an existing file**
(bzw. *Add file → Upload files*) und den **Inhalt** des Ordners hineinziehen (nicht den Ordner selbst), dann *Commit changes*.
Wichtig ist der versteckte Ordner **`.github`** mit den vier Workflows (`daily-scan.yml`, `backtest.yml`,
`check.yml`, `tests.yml`):
- macOS: im Finder mit ⌘ + ⇧ + . versteckte Dateien einblenden, dann mit hochziehen.
- Fehlt er danach im Repository: *Add file → Create new file*, als Namen `.github/workflows/daily-scan.yml`
  eintippen, den Inhalt der Datei hineinkopieren, speichern. Genauso `backtest.yml`, `check.yml` und `tests.yml`.

**Schritt 3 – Schreibrechte für die Automatik.** *Settings → Actions → General → Workflow permissions →*
**Read and write permissions** → *Save*. (Sonst kann der Scan Report und Journal nicht speichern.)

**Schritt 4 – Geheimnisse hinterlegen.** *Settings → Secrets and variables → Actions → New repository secret*:

| Name | Inhalt |
|---|---|
| `ACCOUNT_EUR` | deine Kontogröße für Hebeltrades, z. B. `10000` |
| `TELEGRAM_BOT_TOKEN` | optional, siehe Schritt 5 |
| `TELEGRAM_CHAT_ID` | optional, siehe Schritt 5 |

**Schritt 5 – Telegram (optional, für Push aufs Handy).**
In Telegram **@BotFather** öffnen → `/newbot` → Namen vergeben → du bekommst einen Token.
Deinem neuen Bot eine beliebige Nachricht schicken. Dann im Browser
`https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen (Token einsetzen) und bei `"chat":{"id": …}`
die Zahl ablesen – das ist die Chat-ID. Beides als Secrets eintragen.

**Schritt 6 – Verbindungstest (1 Minute).** Reiter **Actions** (ggf. *I understand my workflows… enable* bestätigen)
→ **Verbindungstest** → *Run workflow*. Die Zusammenfassung des Laufs zeigt eine Ampel-Tabelle: Universum, Aktien- und
Indexkurse (Pflicht), EUR/USD, Earnings-Kalender, Optionsdaten, Secrets, Telegram (optional, mit Testnachricht) und ob
die Termin-Kalender aktuell sind. Steht oben **„Startklar“**, geht es weiter.

**Schritt 7 – Backtest starten.** *Actions → Backtest → Run workflow* → Jahre `8` → *Run*. Dauer ca. 5–15 Minuten.
Ergebnis: `results/backtest_report.md` (mit Equity-Kurve, Auswertung je Setup) und direkt in der Zusammenfassung des Laufs.

**Schritt 8 – Ersten Tages-Scan testen.** *Actions → Täglicher Scan → Run workflow*. Danach liegt der Report unter
`reports/latest.md`; mit Telegram kommt eine Nachricht. Ab jetzt läuft der Scan **automatisch Mo–Fr**.

Läuft etwas schief, schickt GitHub eine E-Mail, und im Lauf steht die Fehlermeldung. Häufigster Grund:
Yahoo drosselt kurzzeitig – der nächste Lauf versucht es erneut.

---

## 5. Tagesroutine: Report lesen und handeln

**Abends/morgens:** Telegram oder `reports/latest.md` (in der GitHub-App gut lesbar) öffnen.

Jeder Kandidat hat dieselben Blöcke: **Warum** (welche Regeln erfüllt sind) · **Wann** (Tag, Uhrzeit, Limit) ·
**Wie** (Stop, Ziel, Zeitstop, Nachziehen) · **Was** (Produkt, Barriere-Korridor, Hebel) · **Wo** (Handelsplatz) ·
**Wie stark** (Ziel und Stop in % – im Basiswert und im Zertifikat – plus Erfahrungswerte des Setups aus dem
Backtest: wie oft Ziel 1 erreicht wurde, Trefferquote, Ø-Ergebnis). Dazu Stückzahl-Tabelle, Checks und Schlagzeilen.

- **Kein Kandidat?** Nichts tun. Offene Positionen trotzdem prüfen.
- **Kandidat vorhanden:**
  1. Vor der Einstiegszeit Produkt suchen (Broker-Derivatesuche oder Emittenten-Seite): *Knock-out / Turbo Long, Open End*,
     Barriere im Korridor, möglichst nahe am Idealwert, Spread ≤ Tabellenwert.
  2. Ab der Einstiegszeit prüfen: Notiert der Basiswert **≤ Limit**? Dann Zertifikat per Limit-Order kaufen.
  3. Sofort **Stop-Order** aufs Zertifikat: Kaufkurs − „Stopabstand je Zert.“ aus der Tabelle.
     Erlaubt dein Broker Stops auf den Basiswert, nimm direkt den Basiswert-Stop.
  4. Kursalarm auf **Ziel 1** (Basiswert).
- **Offene Positionen:** Die Tabelle nennt den **Stop für heute** (nach Teilgewinn täglich nachgezogen) und das Zeitstop-Datum.

**Eigenes Journal:** Das Paper-Journal (`journal/picks.csv`) misst das *System*. Deine echten Käufe und Verkäufe
(Kurs, Schlupf, Abweichungen) notierst du separat – der Vergleich zeigt, wo die Ausführung Geld kostet.

---

## 6. Freigabe-Stufen: vom Test zum echten Geld

Der Abschnitt **System-Status** im Report rechnet das automatisch aus:

| Stufe | Bedingung | Risiko je Trade |
|---|---|---|
| 0 | Backtest fehlt oder verfehlt die Hürde (Erwartungswert ≥ +0,2 R **und** Profit-Faktor ≥ 1,3) | nicht live handeln |
| 1 | Backtest bestanden, < 30 abgeschlossene Paper-Trades | 0,25 % |
| 2 | ≥ 30 Trades, Forward-Erwartungswert ≥ +0,2 R | bis 0,5 % |
| 3 | ≥ 50 Trades, weiter im Rahmen | bis 1 % |
| ⛔ | Forward-Drawdown > 1,5 × Backtest-Drawdown | Pause, analysieren |

Zusätzlich für dein echtes Konto (das System sieht es nicht): −2 % an einem Tag → Schluss für heute ·
−10 % vom Höchststand → Risiko halbieren · −20 % → Pause. Nie verbilligen.

Risiko ändern: `risk_per_trade_pct` in `config.yaml`.

---

## 7. Anpassen

- **`config.yaml`** – alle Regeln. Nach jeder Änderung: Backtest neu laufen lassen. Nicht so lange drehen,
  bis die Vergangenheit perfekt aussieht – das ist Overfitting und fällt live auseinander.
- **`setups:`** – je Setup `trade`, `observe` oder `off`.
- **`events.csv`** – Zins- und Inflationstermine, geprüft gegen die offiziellen Kalender (Stand 22.09.2026):
  Fed bis Dezember 2027, EZB bis Dezember 2028, US-Inflation (CPI) bis Dezember 2026 – den CPI-Plan 2027
  veröffentlicht die BLS erst Ende 2026. **Der Report warnt automatisch**, sobald eine Terminart in weniger als
  35 Tagen ausläuft. Quellen: federalreserve.gov (FOMC), ecb.europa.eu (EZB), bls.gov/schedule (CPI).
- **`holidays.csv`** – Börsenfeiertage bis Ende 2028 (US laut NYSE-Kalender, Xetra 2028 nach Standardregeln) für
  korrekte Einstiegs- und Zeitstop-Daten. Auch hier warnt der Report, bevor die Liste ausläuft.
- **`universe/de.csv`** – deutsche Titel; gelegentlich mit der aktuellen DAX-Zusammensetzung abgleichen.
  US: die S&P-500-Liste wird automatisch geladen. Eigene Titel: `universe: extra:`.

---

## 8. Lokal statt GitHub ausführen

Python 3.11 oder neuer installieren, dann im Projektordner:

```
python -m venv .venv
.venv\Scripts\activate          (Windows)   bzw.   source .venv/bin/activate   (macOS/Linux)
pip install -r requirements.txt
python -m pytest -q             # Selbsttest
python run.py check             # Verbindungstest
python run.py backtest          # zuerst
python run.py daily             # Tages-Scan -> reports/latest.md
```

Kontogröße und Telegram lokal als Umgebungsvariablen setzen (`ACCOUNT_EUR`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
oder `account: size_eur` in `config.yaml` eintragen. Automatisch täglich: Windows-Aufgabenplanung bzw. cron,
Mo–Fr gegen 23:45 Uhr. Wird tagsüber gestartet, ignoriert der Scan die unfertige Tageskerze.

---

## 9. Grenzen – bitte lesen

- **Datenquelle:** yfinance/Yahoo Finance ist kostenlos, aber inoffiziell – Drosselung, Ausfälle und einzelne
  Datenfehler kommen vor. Fehlende Titel werden übersprungen, nicht geschätzt.
- **Survivorship-Bias:** Der Backtest nutzt das heutige Universum. Pleite- und Absteiger-Firmen fehlen –
  das Ergebnis ist zu schön. Faustregel: Erwartungswert gedanklich halbieren.
- **Keine Earnings-Sperre im Backtest** (historische Termine nicht zuverlässig verfügbar); live ist sie aktiv.
- **Ausführung:** Im Backtest werden Stops zum Stopkurs gefüllt (außer bei Kurslücken). In schnellen Märkten
  gibt es Schlupf. Emittenten-Kurse, Spreads und die tägliche Barriere-Anpassung sind nur als Kosten angenähert.
- **Wechselkurs:** Ergebnisse in R beziehen sich auf den Basiswert. Bei US-Titeln wirkt EUR/USD zusätzlich
  auf den Euro-Wert des Zertifikats.
- **Vergangenheit ≠ Zukunft.** Ein bestandener Backtest ist Voraussetzung, kein Beweis.

---

## 10. Häufige Fragen

**Warum oft kein Kandidat?** Weil alle Bedingungen gleichzeitig stimmen müssen und die Marktampel grün sein muss.
Wenige, klare Trades schlagen viele mittelmäßige – vor allem mit Hebel und Kosten.

**Warum nicht jeden Tag genau drei?** Eine feste Zahl würde an schwachen Tagen schwache Trades erzwingen.
Drei ist die Obergrenze, kein Ziel.

**Kann Claude die Kandidaten zusätzlich prüfen?** Ja: Kopiere den Report (oder `reports/latest.json`) in den Chat –
dann lassen sich Nachrichtenlage, Termine und Produktwahl gemeinsam durchgehen. Selbstständig täglich laufen kann
der Chat nicht; dafür ist diese Automatik da.

**Short-Trades?** Nicht eingebaut. Das Regelwerk handelt nur long und steht bei roter Marktampel an der Seitenlinie.

**Kontogröße ändern?** Secret `ACCOUNT_EUR` in GitHub anpassen.

---

## Neu in Version 1.1

- **Verbindungstest** (`Actions → Verbindungstest` bzw. `python run.py check`) mit Ampel-Tabelle und Telegram-Testnachricht.
- Report-Blöcke **„Wo“** und **„Wie stark“**: Ziel und Stop in % im Basiswert *und* im Zertifikat, Erfahrungswerte des
  Setups aus dem Backtest (Ziel-1-Quote, Trefferquote, Ø-Ergebnis). Backtest-Report mit Spalte „Ziel 1 erreicht“.
- Termine gegen offizielle Kalender geprüft und erweitert (Fed 2027, EZB 2028), Feiertage bis 2028,
  **automatische Warnung**, bevor ein Kalender ausläuft.
- GitHub-Workflows auf `actions/checkout@v5` und `actions/setup-python@v6` (Node 24) umgestellt, weil GitHub die
  Node-20-Laufzeit im September 2026 entfernt.
- Lufthansa im deutschen Universum ergänzt.

## Dateien

| Pfad | Inhalt |
|---|---|
| `run.py` | Start: `check`, `backtest` oder `daily` |
| `config.yaml` | Regelwerk |
| `scanner/strategy.py` | Indikatoren, Setups, Trade-Simulation (für Backtest und Live identisch) |
| `scanner/portfolio.py` | Auswahl, Korrelation, Kennzahlen |
| `scanner/live.py` | Tageslauf, Journal, Report, Telegram |
| `scanner/earnings.py` | Earnings-Radar, Reaktionsanalyse, Termin-Cache |
| `scanner/backtest.py` | Backtest je Setup und gesamt |
| `scanner/data.py` | Datenabruf (yfinance) |
| `scanner/check.py` | Verbindungstest (`python run.py check`) |
| `tests/` | 31 automatische Tests mit synthetischen Daten |
| `reports/` | Tagesreports (`latest.md`, Archiv) |
| `journal/picks.csv` | Paper-Journal aller Signale |
| `results/` | Backtest-Ergebnisse |
| `data/` | Termin-Cache und Laufzustand |
