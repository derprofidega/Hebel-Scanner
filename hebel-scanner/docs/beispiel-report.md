> **Beispiel mit synthetischen Testdaten** – T00, T01 und T05 sind erfundene Titel, auch die Backtest-Zahlen im System-Status sind erfunden. So sieht der Report aus, den du nach der Einrichtung täglich bekommst.

# Hebel-Scanner · Fr, 18.09.2026

Datenstand: S&P 500 18.09.2026 · DAX 18.09.2026 · EUR/USD 1,1000  
Konto 10.000 € · Risiko 0,25 % je Trade → **1 R = 25,00 €** · Stufe laut Regelwerk: Nicht live handeln – Backtest-Hürde verfehlt

> ⚠️ Ordergebühren kosten bei 1 R = 25,00 € rund 0,12 R pro Trade – bei kleinem Risiko je Trade frisst das einen großen Teil des Vorteils.

## Marktampel
- **S&P 500** 4.564 · +4,3 % zur 200-Tage-Linie → Longs erlaubt ✅
- **DAX** 13.900 · −4,3 % zur 200-Tage-Linie → keine neuen Longs ⛔

## Neue Kandidaten (1 von max. 3)

**Ablauf am Einstiegstag:** (1) Produkt suchen: Knock-out Long mit Barriere im Korridor und Spread ≤ Tabellenwert. (2) Ab der genannten Uhrzeit Limit-Order – nur solange der Basiswert unter dem Limit notiert. (3) Sofort danach Stop-Order: Kaufkurs minus „Stopabstand je Zert.“. (4) Kursalarm auf Ziel 1 setzen.

### 1. T00 – Test 0 (USA) · Setup: Trend-Rücksetzer

**Warum (regelbasiert – keine Kursprognose):**
- Trend intakt: Schluss 20,4 % über der 200-Tage-Linie, 50-Tage-Linie +2,2 % in 10 Handelstagen.
- Relative Stärke: stärker als 91 % des Universums (≈ 6 Monate +27,2 % vs. S&P 500 −0,8 %).
- Rücksetzer: 3 Tage in Folge tiefer geschlossen, Tief 0,9 ATR unter der 20-Tage-EMA.
- Umkehr: Schluss über dem Vortageshoch bei 91 % der Tagesspanne, Volumen 1,1× Durchschnitt.

**Wann:** Mo, 21.09.2026 ab 15:45 Uhr (Berliner Zeit), nur per Limit. Kaufen nur, solange T00 **≤ 293,42 $** notiert. Läuft der Kurs ohne Rücksetzer darüber weg, verfällt der Trade – nicht hinterherlaufen.

**Wie (Ausstieg steht vorher fest):** Stop **288,19 $** unter dem Rücksetzer-Tief (−1,8 %, 1,8 ATR). Ziel 1 **303,86 $** (+2 R bei Einstieg zum Limit): 50 % verkaufen, Rest-Stop auf Einstand, danach unter das 2-Tages-Tief nachziehen. Zeitstop: Schlusskurs am 02.10.2026, falls Ziel 1 bis dahin nicht erreicht ist.

**Was (Produkt):** Knock-out Long (Turbo/Mini-Future) auf T00, Barriere zwischen **282,97 $ und 285,37 $**, ideal um 284,17 $ (≈ 3,2 % unter dem Limit) → Hebel ≈ 31,7. Worst Case (Knock-out per Kurslücke): ≈ 1,8 R.
⚠️ Barriere nur ≈ 3,2 % entfernt: Schon eine normale Kurslücke kann ausknocken – der Verlust bleibt durch die Stückzahl auf ≈ 1,8 R begrenzt, tritt aber häufiger ein.

**Wo:** Knock-out mit deutscher WKN/ISIN – außerbörslich direkt beim Emittenten über deinen Broker oder an einer Börse wie Stuttgart oder gettex. Den Spread vor dem Kauf mit dem Tabellenwert unten vergleichen. Bei US-Titeln sind die Kurse vor dem US-Börsenstart oft breit gestellt – deshalb erst ab der genannten Uhrzeit handeln.

**Wie stark (Potenzial vs. Risiko):** Ziel 1 liegt **+3,6 %** über dem Limit – im Zertifikat mit Hebel ≈ 31,7 rund **+113 %**. Der Stop kostet −1,8 % bzw. ≈ −56 % im Zertifikat (= 1 R). Chance-Risiko bis Ziel 1 = 2 : 1; die zweite Hälfte läuft danach ohne feste Obergrenze mit nachgezogenem Stop.
Erfahrungswerte dieses Setups im Backtest (208 Trades): Ziel 1 in 36 % der Fälle erreicht, Trefferquote 48 %, Ø +0,13 R je Trade – ein Durchschnitt über viele Trades, keine Prognose für diesen einen.

Positionsgröße für 1 R = 25,00 € (entspricht ≈ 5,3 Aktien):

| Bezugsverh. | Stück | Einsatz ≈ | Stopabstand je Zert. | max. Spread |
|---|---|---|---|---|
| 0,10 | 52 | 43,70 € | 0,475 € | 0,024 € |
| 1,00 | 5 | 42,02 € | 4,747 € | 0,237 € |

**Checks:** nächste Zahlen 31.10.2026 ✓ · keine offenen Positionen zum Abgleich ✓ · Event-Kalender frei ✓

**Schlagzeilen (ungeprüft – nur als Veto-Check: Übernahme, Klage, Gewinnwarnung):**
- [Test (Titel) / mit Sonderzeichen](https://example.com/x) (Wire – 2026-09-18)

## Beobachtung: Earnings-Gap-Signale (nur Paper – nicht handeln)

Diese Signale werden automatisch im Journal mitgeschrieben, bis Backtest und Forward-Test zeigen, ob das Setup einen Vorteil hat. Freischalten: in `config.yaml` `earnings_gap: trade`.

- **T01** (Test 1): +3,5 % bei 5,0× Volumen, Zahlen vom 18.09., EPS +20 % · Paper-Einstieg ≤ 280,64 $, Stop 271,51 $, KO-Korridor 262,38–267,84 $

## Offene Paper-Positionen (0)
Keine.

## Earnings-Radar – nächste Tage

Kein Positionierungs-Tipp: Vor den Zahlen ist die **Größe** der Bewegung grob abschätzbar, die **Richtung** nicht verlässlich. Ein Knock-out über die Zahlen zu halten, ist ein Münzwurf mit Totalverlust-Risiko. Gehandelt wird – wenn überhaupt – erst danach (Setup „Earnings-Gap“).

| Termin | Titel | Ø Reaktion | ↑ / ↓ | größte | Optionen erwarten | KO-Hebel 10 wäre raus (long · short) | EPS > Schätzung |
|---|---|---|---|---|---|---|---|
| Di 22.09. nach Börsenschluss | T05 Test 5 | ±1,3 % | 6 / 1 | 3,8 % | ±6,5 % | 0/7 · 0/7 | 7/7 |

*↑/↓ = Richtung der letzten Reaktionen (Schluss Reaktionstag vs. Vortag). Die Verteilung früherer Richtungen sagt die nächste kaum voraus. „KO-Hebel 10 wäre raus“ = Tagestief bzw. -hoch lag ≥ 10 % vom Vortagesschluss entfernt → ein Knock-out mit ~10 % Barriereabstand wäre wertlos gewesen.*

**Zuletzt berichtet** – Beat heißt nicht automatisch steigender Kurs:

| Titel | Reaktionstag | EPS vs. Schätzung | Reaktion |
|---|---|---|---|
| T01 Test 1 | 18.09. | +20,0 % | +3,5 % · Earnings-Gap-Setup ✅ |

## System-Status
- Backtest 2020–2026 (gehandelte Setups): 208 Trades, Erwartungswert +0,13 R, Profit-Faktor 1,31, Max-Drawdown 11,6 R → Hürde verfehlt ❌ (nötig ≥ +0,20 R und PF ≥ 1,30)
- Backtest je Setup – Trend-Rücksetzer: 208 Trades, +0,13 R, PF 1,31
- Forward-Test (Paper): noch keine abgeschlossenen Trades.
- **Risikostufe laut Regelwerk: Nicht live handeln – Backtest-Hürde verfehlt**

---
Regelbasierte Kandidaten aus öffentlichen Kursdaten – keine Anlageberatung und keine Garantie. Hebelprodukte können wertlos verfallen. Daten ohne Gewähr (Yahoo Finance).