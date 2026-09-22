"""Verbindungstest: prüft in ~1 Minute, ob alle Datenquellen und Telegram funktionieren.

    python run.py check

Pflicht (sonst Exit-Code 1): Universum, Aktienkurse, Indexkurse.
Optional (nur Warnung): EUR/USD, Earnings-Kalender, Earnings-Historie, Optionsdaten, Secrets, Telegram, Kalender.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from . import fmt
from .data import BENCHMARKS
from .live import calendar_warnings, send_telegram

log = logging.getLogger("scanner.check")

PROBES = {"US": ["AAPL", "MSFT"], "DE": ["SAP.DE", "SIE.DE"]}


def run_check(cfg: dict, loader, root: Path, now: pd.Timestamp | None = None) -> int:
    root = Path(root)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    today = now.tz_convert("Europe/Berlin").normalize().tz_localize(None)
    rows: list[tuple[str, str, str]] = []          # (Status, Prüfung, Details)
    hard_fail = False

    def safe(fn, default=None):
        """Optionale Prüfungen dürfen den Test nie abbrechen."""
        try:
            return fn()
        except Exception as exc:
            log.warning("Prüfung fehlgeschlagen: %s", exc)
            return default

    def add(ok, name, detail, hard=False):
        nonlocal hard_fail
        status = "✅" if ok else ("❌" if hard else "⚠️")
        hard_fail |= (hard and not ok)
        rows.append((status, name, detail))

    try:
        uni = loader.universe()
        counts = uni["market"].value_counts().to_dict()
        add(len(uni) >= 50, "Universum", f"{len(uni)} Titel (US {counts.get('US', 0)}, DE {counts.get('DE', 0)})", hard=True)
    except Exception as exc:
        add(False, "Universum", f"Fehler: {exc}", hard=True)

    probes = [t for ts in PROBES.values() for t in ts]
    frames = safe(lambda: loader.ohlc(probes, period="1mo"), {}) or {}
    for market, tickers in PROBES.items():
        got = [t for t in tickers if t in frames and frames[t] is not None and len(frames[t])]
        last = max((frames[t].index[-1] for t in got), default=None)
        detail = (f"{', '.join(got)} geladen, letzte Kerze {fmt.date(last)}" if got
                  else "keine Kursdaten – Yahoo gesperrt oder gedrosselt?")
        add(bool(got), f"Aktienkurse {market}", detail, hard=True)

    bench = safe(lambda: loader.ohlc(list(BENCHMARKS.values()), period="1mo"), {}) or {}
    for m, t in BENCHMARKS.items():
        ok = t in bench and bench[t] is not None and len(bench[t]) > 0
        add(ok, f"Index {t}", f"letzter Schluss {fmt.num(float(bench[t]['Close'].iloc[-1]), 0)}" if ok
            else "keine Daten", hard=True)

    fx = safe(loader.fx_eurusd)
    add(fx is not None, "EUR/USD", fmt.num(fx, 4) if fx else "fehlt – Stückzahlen für US-Titel würden fehlen")

    ec = cfg.get("earnings_radar") or {}
    cal = safe(lambda: loader.earnings_calendar(today, today + pd.Timedelta(days=7),
                                                min_mcap=float(ec.get("min_market_cap_usd", 5e9))))
    add(cal is not None and len(cal) > 0, "US-Earnings-Kalender",
        f"{len(cal)} Termine in den nächsten 7 Tagen" if cal is not None and len(cal) else
        "leer oder nicht abrufbar – Termine würden einzeln geprüft (langsamer)")

    hist = safe(lambda: loader.earnings_history("AAPL", limit=8))
    add(hist is not None and len(hist) > 0, "Earnings-Historie (AAPL)",
        f"{len(hist)} Termine" if hist is not None and len(hist) else "nicht abrufbar – Radar ohne Reaktionsstatistik")

    spot = float(frames["AAPL"]["Close"].iloc[-1]) if "AAPL" in frames and len(frames["AAPL"]) else None
    im = safe(lambda: loader.implied_move("AAPL", today + pd.Timedelta(days=1), spot, max_days_after=10)) if spot else None
    add(im is not None, "Optionsdaten (AAPL)", f"Straddle ≈ ±{fmt.num(im[0], 1)} % bis {fmt.date(im[1])}" if im
        else "nicht abrufbar – Radar ohne „Optionen erwarten“")

    acc = os.environ.get("ACCOUNT_EUR")
    add(bool(acc), "Secret ACCOUNT_EUR", "gesetzt" if acc else
        f"fehlt – es wird size_eur aus config.yaml genutzt ({fmt.num(float((cfg.get('account') or {}).get('size_eur', 0)), 0)} €)")

    sent = safe(lambda: send_telegram("✅ Hebel-Scanner: Verbindungstest erfolgreich – Telegram funktioniert."), False)
    add(sent is True, "Telegram", {True: "Testnachricht gesendet – bitte im Chat prüfen",
                                   False: "Senden fehlgeschlagen – Token/Chat-ID prüfen",
                                   None: "nicht eingerichtet (optional)"}[sent])

    cal_warn = calendar_warnings(root / ((cfg.get("filters") or {}).get("events_file", "events.csv")),
                                 root / "holidays.csv", today)
    add(not cal_warn, "Termin- und Feiertagskalender", "aktuell" if not cal_warn else " · ".join(cal_warn))

    verdict = ("❌ **Nicht startklar** – Pflichtprüfung fehlgeschlagen. In ein paar Minuten erneut versuchen; "
               "bleibt es dabei, die Fehlermeldung aus dem Log schicken." if hard_fail else
               "✅ **Startklar.** Nächster Schritt: *Actions → Backtest → Run workflow*.")
    md = ["# Hebel-Scanner – Verbindungstest", "", f"{fmt.weekday_date(today)} · {verdict}", "",
          "| | Prüfung | Ergebnis |", "|---|---|---|"]
    md += [f"| {s} | {n} | {d.replace('|', '/')} |" for s, n, d in rows]
    md += ["", "⚠️ = optional, der Tages-Scan läuft trotzdem (mit eingeschränkten Angaben)."]
    text = "\n".join(md) + "\n"
    out = root / "reports" / "verbindungstest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 1 if hard_fail else 0
