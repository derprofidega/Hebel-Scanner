"""Täglicher Scan: Daten laden -> Paper-Journal fortschreiben -> 0–3 neue Kandidaten -> Report."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import fmt
from .config import SETUP_NAMES, Params, params_from_config, setup_modes
from .data import BENCH_NAMES, BENCHMARKS
from .earnings import EarningsCache, build_radar, next_earnings
from .portfolio import max_corr_against, rank_candidates, returns_matrix, trade_stats
from .strategy import Trade, add_indicators, build_candidates, rs_filter, simulate_row

log = logging.getLogger("scanner.live")

JOURNAL_COLUMNS = ["id", "signal_date", "setup", "mode", "ticker", "name", "market", "currency", "close_at_signal",
                   "entry", "stop", "risk", "atr", "ko_lo", "ko_hi", "ko_mid", "rs_pct", "clv", "status",
                   "fill_date", "fill_price", "target", "partial_date", "exit_date", "exit_reason", "r_gross",
                   "r_costs", "r_net", "days_held", "current_stop", "last_close", "unrealized_r", "risk_end_date",
                   "earnings_date", "note"]
ACTIVE = ("pending", "open")


# --------------------------------------------------------------------------- Hilfen

def fnum(v) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def dstr(d) -> str:
    return "" if d is None or (not isinstance(d, str) and pd.isna(d)) else pd.Timestamp(d).strftime("%Y-%m-%d")


def vstr(x, nd: int = 6) -> str:
    x = fnum(x)
    return "" if not math.isfinite(x) else str(round(x, nd))


def load_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in JOURNAL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df.loc[df["setup"] == "", "setup"] = "pullback"
    df.loc[df["mode"] == "", "mode"] = "trade"
    return df[JOURNAL_COLUMNS].to_dict("records")


def save_journal(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=JOURNAL_COLUMNS).to_csv(path, index=False)


def load_events(path: Path) -> dict:
    if not path.exists():
        return {}
    ev = pd.read_csv(path, comment="#", dtype=str, keep_default_na=False)
    out: dict = {}
    for _, row in ev.iterrows():
        try:
            d = pd.Timestamp(row["date"]).normalize()
        except (ValueError, TypeError):
            continue
        out[d] = f"{out[d]} + {row['event']}" if d in out else row["event"]
    return out


def apply_trade(row: dict, t: Trade) -> None:
    row["status"] = t.status
    row["fill_date"] = dstr(t.fill_date)
    row["fill_price"] = vstr(t.fill_price)
    row["target"] = vstr(t.target)
    row["partial_date"] = dstr(t.partial_date)
    row["exit_date"] = dstr(t.exit_date)
    row["exit_reason"] = t.exit_reason
    row["r_gross"] = vstr(t.r_gross, 3)
    row["r_costs"] = vstr(t.r_costs, 3)
    row["r_net"] = vstr(t.r_net, 3)
    row["days_held"] = str(t.days_held) if t.status in ("open", "closed") else ""
    row["current_stop"] = vstr(t.current_stop)
    row["last_close"] = vstr(t.last_close)
    row["unrealized_r"] = vstr(t.unrealized_r, 3)
    row["risk_end_date"] = dstr(t.risk_end_date)


def update_journal(rows: list[dict], ind_frames: dict, p: Params, r_eur: float | None) -> list[dict]:
    """Rechnet alle offenen Paper-Trades aus den Kursdaten neu durch (zustandslos, reproduzierbar).
    Abgeschlossene Trades bleiben eingefroren. Rückgabe: Liste der Veränderungen."""
    changes = []
    for row in rows:
        if row["status"] not in ACTIVE:
            continue
        ind = ind_frames.get(row["ticker"])
        if ind is None:
            row["note"] = "Keine aktuellen Kursdaten"
            continue
        sd = pd.Timestamp(row["signal_date"])
        pos = ind.index.get_indexer([sd])[0]
        if pos < 0:
            row["note"] = "Signaltag fehlt in den Kursdaten"
            continue
        ref = fnum(row["close_at_signal"])
        if math.isfinite(ref) and ref > 0 and abs(ind["Close"].iloc[pos] / ref - 1) > 0.03:
            row["note"] = "Kurshistorie weicht ab (Split?) – manuell prüfen"
            continue
        before = (row["status"], row["partial_date"])
        t = simulate_row(ind, sd, fnum(row["entry"]), fnum(row["stop"]), fnum(row["ko_mid"]),
                         fnum(row["risk"]), p, r_eur)
        if t is None:
            continue
        apply_trade(row, t)
        if (row["status"], row["partial_date"]) != before:
            changes.append(dict(row))
    return changes


def regimes(bench_ind: dict) -> dict:
    out = {}
    for m, b in bench_ind.items():
        last = b.iloc[-1]
        valid = pd.notna(last["sma_long"])
        out[m] = {"ok": bool(valid and last["Close"] > last["sma_long"]), "close": float(last["Close"]),
                  "date": b.index[-1], "dist": float((last["Close"] / last["sma_long"] - 1) * 100) if valid else np.nan}
    return out


def sizing(pk: dict, r_eur: float, fx: float | None, ratios: list[float]):
    fx_rate = fx if pk["currency"] == "USD" else 1.0
    if not fx_rate:
        return [], None
    out = []
    for br in ratios:
        per_cert = pk["risk"] * br / fx_rate
        n = int(math.floor(r_eur / per_cert)) if per_cert > 0 else 0
        cert_px = (pk["entry"] - pk["ko_mid"]) * br / fx_rate
        out.append({"ratio": br, "n": n, "cert_px": cert_px, "invest": n * cert_px,
                    "stop_dist": per_cert, "max_spread": 0.05 * per_cert})
    return out, r_eur * fx_rate / pk["risk"]


def earnings_trigger(ticker, signal_date, us_cal, loader) -> tuple[pd.Timestamp | None, float]:
    """Waren am/vor dem Signaltag Quartalszahlen? -> (Termin, EPS-Überraschung %)."""
    d = pd.Timestamp(signal_date)
    lo = d - pd.Timedelta(days=3)
    if us_cal is not None and len(us_cal):
        hit = us_cal[(us_cal["ticker"] == ticker) & (us_cal["date"] >= lo) & (us_cal["date"] <= d)]
        if len(hit):
            r = hit.sort_values("date").iloc[-1]
            return pd.Timestamp(r["date"]), float(r["surprise_pct"]) if pd.notna(r["surprise_pct"]) else np.nan
    hist = loader.earnings_history(ticker, limit=4)
    if hist is not None and len(hist):
        ev = hist[(hist["event"].dt.normalize() >= lo) & (hist["event"].dt.normalize() <= d)]
        if len(ev):
            r = ev.iloc[-1]
            return pd.Timestamp(r["event"]).normalize(), float(r["surprise_pct"]) if pd.notna(r["surprise_pct"]) else np.nan
    return None, np.nan


# --------------------------------------------------------------------------- Auswahl

def select_new(cands, meta, rets, as_of, rows, cfg, p: Params, loader, events, modes, us_cal,
               cache: EarningsCache, today: pd.Timestamp, fresh: set | None = None):
    """fresh = Märkte mit neuer Tageskerze seit dem letzten Lauf (an Feiertagen fällt ein Markt heraus)."""
    fresh = set(as_of) if fresh is None else set(fresh)
    acc, flt = cfg.get("account") or {}, cfg.get("filters") or {}
    max_pos = int(acc.get("max_open_risk_positions", 4))
    max_new = int(acc.get("max_new_per_day", 3))
    max_corr = float(acc.get("max_correlation", 0.7))
    max_obs = int((cfg.get("setups") or {}).get("max_observe_per_day", 3))
    block_days = int(flt.get("earnings_block_days", 16))
    max_lookups = int(flt.get("max_earnings_lookups", 12))

    existing = {r["id"] for r in rows}
    active = [r for r in rows if r["status"] in ACTIVE]
    active_trade = [r for r in active if r["mode"] == "trade"]
    risk_on = [r for r in active_trade if not r["risk_end_date"]]
    held = [r["ticker"] for r in active_trade]
    held_obs = {r["ticker"] for r in active if r["mode"] == "observe"}
    sig_dates = {dstr(d) for d in as_of.values()}
    already = sum(1 for r in rows if r["signal_date"] in sig_dates and r["mode"] == "trade")
    already_obs = sum(1 for r in rows if r["signal_date"] in sig_dates and r["mode"] == "observe")
    slots = max(0, min(max_pos - len(risk_on), max_new - already))

    entry_day = {m: fmt.next_session(d, m) for m, d in as_of.items()}
    blocked = {m: events[d] for m, d in entry_day.items() if d in events}

    if len(cands):
        markets = cands["ticker"].map(meta["market"])
        today_c = cands[[pd.Timestamp(d) == as_of.get(m) and m in fresh for d, m in zip(cands["date"], markets)]]
        today_c = rs_filter(today_c, p)
    else:
        today_c = cands
    trade_c = today_c[today_c["setup"].map(modes).eq("trade")] if len(today_c) else today_c
    obs_c = today_c[today_c["setup"].map(modes).eq("observe")] if len(today_c) else today_c

    def base(row, t, m, cid):
        pk = {k: (row[k].item() if hasattr(row[k], "item") else row[k]) for k in row.index}
        pk.update({"id": cid, "market": m, "name": meta.at[t, "name"], "currency": meta.at[t, "currency"],
                   "entry_day": entry_day[m], "earnings": None, "earnings_known": False,
                   "corr": np.nan, "corr_with": None, "trigger_event": None, "trigger_surprise": np.nan})
        if row["setup"] == "earnings_gap":
            pk["trigger_event"], pk["trigger_surprise"] = earnings_trigger(t, row["date"], us_cal, loader)
        return pk

    picks, watch, lookups = [], [], 0
    for _, row in (rank_candidates(trade_c) if len(trade_c) else trade_c).iterrows():
        t, m = row["ticker"], meta.at[row["ticker"], "market"]
        cid = f"{t}_{dstr(row['date'])}"
        if cid in existing:
            continue
        reason, corr, corr_with = None, np.nan, None
        earn, known = None, False
        if m in blocked:
            reason = f"Event am Einstiegstag ({blocked[m]})"
        elif t in held:
            reason = "Position bereits offen"
        elif len(picks) >= slots:
            reason = "Kapazität voll (Risiko-Slots/Tageslimit)"
        else:
            corr, corr_with = max_corr_against(rets, t, held + [x["ticker"] for x in picks], row["date"])
            if np.isfinite(corr) and corr > max_corr:
                reason = f"Korrelation {fmt.num(corr)} mit {corr_with}"
        if reason is None and row["setup"] == "pullback":
            allow = lookups < max_lookups
            earn, known = next_earnings(t, entry_day[m], today, us_cal, cache, loader, allow_fetch=allow)
            lookups += 1
            if known and earn is not None and earn <= entry_day[m] + pd.Timedelta(days=block_days):
                reason = f"Earnings am {fmt.date(earn)} im Haltezeitraum"
        if reason:
            if len(watch) < 8:
                watch.append({"ticker": t, "name": meta.at[t, "name"], "rs_pct": row["rs_pct"],
                              "setup": row["setup"], "reason": reason})
            continue
        pk = base(row, t, m, cid)
        pk.update({"earnings": earn, "earnings_known": known, "corr": corr, "corr_with": corr_with, "mode": "trade"})
        picks.append(pk)

    observed = []
    for _, row in (rank_candidates(obs_c) if len(obs_c) else obs_c).iterrows():
        t, m = row["ticker"], meta.at[row["ticker"], "market"]
        cid = f"{t}_{dstr(row['date'])}"
        if cid in existing or t in held_obs or len(observed) + already_obs >= max_obs:
            continue
        pk = base(row, t, m, cid)
        pk["mode"] = "observe"
        observed.append(pk)
    return picks, observed, watch, blocked, slots


def pick_to_row(pk: dict) -> dict:
    row = {c: "" for c in JOURNAL_COLUMNS}
    note = []
    if pk["setup"] == "pullback" and not pk.get("earnings_known"):
        note.append("Earnings-Termin unbekannt")
    if pk["setup"] == "earnings_gap" and pk.get("trigger_event") is None:
        note.append("Anlass der Kurslücke unbekannt")
    row.update({
        "id": pk["id"], "signal_date": dstr(pk["date"]), "setup": pk["setup"], "mode": pk["mode"],
        "ticker": pk["ticker"], "name": pk["name"], "market": pk["market"], "currency": pk["currency"],
        "close_at_signal": vstr(pk["close"]), "entry": vstr(pk["entry"]), "stop": vstr(pk["stop"]),
        "risk": vstr(pk["risk"]), "atr": vstr(pk["atr"]), "ko_lo": vstr(pk["ko_lo"]), "ko_hi": vstr(pk["ko_hi"]),
        "ko_mid": vstr(pk["ko_mid"]), "rs_pct": vstr(pk["rs_pct"], 1), "clv": vstr(pk["clv"], 3),
        "status": "pending", "earnings_date": dstr(pk["earnings"]) if pk.get("earnings_known") else "",
        "note": "; ".join(note),
    })
    return row


# --------------------------------------------------------------------------- System-Status

def system_status(bt: dict | None, fwd: dict, cfg: dict) -> dict:
    g = cfg.get("gates") or {}
    min_e = float(g.get("min_backtest_expectancy_r", 0.2))
    min_pf = float(g.get("min_backtest_profit_factor", 1.3))
    n1 = int(g.get("forward_trades_stage1", 30))
    n2 = int(g.get("forward_trades_stage2", 50))
    abort_f = float(g.get("abort_drawdown_factor", 1.5))
    st = {"backtest_ok": None, "stage": "", "abort": False, "lines": []}
    if not bt or not bt.get("n"):
        st["stage"] = "Nur beobachten – Backtest fehlt noch"
        st["lines"].append("Backtest noch nicht gelaufen → in GitHub unter *Actions → Backtest → Run workflow* starten.")
        return st
    st["backtest_ok"] = bt["expectancy_r"] >= min_e and bt["profit_factor"] >= min_pf
    st["lines"].append(
        f"Backtest {bt['start'][:4]}–{bt['end'][:4]} (gehandelte Setups): {bt['n']} Trades, Erwartungswert "
        f"{fmt.r(bt['expectancy_r'])}, Profit-Faktor {fmt.num(bt['profit_factor'])}, Max-Drawdown "
        f"{fmt.num(bt['max_dd_r'], 1)} R → " + ("Hürde bestanden ✅" if st["backtest_ok"] else
                                              f"Hürde verfehlt ❌ (nötig ≥ {fmt.r(min_e)} und PF ≥ {fmt.num(min_pf)})"))
    for s, v in (bt.get("per_setup") or {}).items():
        if v.get("n"):
            st["lines"].append(f"Backtest je Setup – {SETUP_NAMES.get(s, s)}: {v['n']} Trades, "
                               f"{fmt.r(v['expectancy_r'])}, PF {fmt.num(v['profit_factor'])}")
    n = fwd.get("n", 0)
    if n:
        st["lines"].append(
            f"Forward-Test (Paper, gehandelte Setups): {n} Trades, Erwartungswert {fmt.r(fwd['expectancy_r'])}, "
            f"Trefferquote {fmt.pct(fwd['win_rate'] * 100, 0)}, Max-Drawdown {fmt.num(fwd['max_dd_r'], 1)} R.")
        limit = abort_f * bt["max_dd_r"]
        if fwd["max_dd_r"] > limit:
            st["abort"] = True
            st["lines"].append(f"⛔ ABBRUCHKRITERIUM: Forward-Drawdown {fmt.num(fwd['max_dd_r'], 1)} R > "
                               f"{fmt.num(limit, 1)} R ({fmt.num(abort_f, 1)} × Backtest). System pausieren und analysieren.")
    else:
        st["lines"].append("Forward-Test (Paper): noch keine abgeschlossenen Trades.")
    if not st["backtest_ok"]:
        st["stage"] = "Nicht live handeln – Backtest-Hürde verfehlt"
    elif st["abort"]:
        st["stage"] = "Pause – Abbruchkriterium erreicht"
    elif n < n1:
        st["stage"] = f"0,25 % pro Trade (noch {n1 - n} Paper-Trades bis zur nächsten Prüfung)"
    elif fwd["expectancy_r"] < min_e:
        st["stage"] = "0,25 % pro Trade – Forward-Erwartungswert unter der Hürde"
    elif n < n2:
        st["stage"] = f"bis 0,5 % pro Trade (noch {n2 - n} Paper-Trades bis zur nächsten Prüfung)"
    else:
        st["stage"] = "bis 1 % pro Trade – Forward-Test im Rahmen des Backtests"
    return st


# --------------------------------------------------------------------------- Report-Bausteine

def esc(text) -> str:
    return str(text).replace("[", "(").replace("]", ")").replace("|", "/").replace("\n", " ")


def why_lines(pk: dict, p: Params) -> list[str]:
    bench = BENCH_NAMES.get(pk["market"], "Index")
    rs = (f"- Relative Stärke: stärker als {fmt.num(pk['rs_pct'], 0)} % des Universums (≈ 6 Monate "
          f"{fmt.pct(pk['ret_rs'] * 100, sign=True)} vs. {bench} {fmt.pct(pk['bench_ret_rs'] * 100, sign=True)}).")
    if pk["setup"] == "earnings_gap":
        if pk.get("trigger_event") is not None:
            sur = pk.get("trigger_surprise")
            sur_txt = f", EPS {fmt.pct(sur, 1, sign=True)} vs. Schätzung" if np.isfinite(fnum(sur)) else ""
            anlass = f"- Anlass: Quartalszahlen vom {fmt.date(pk['trigger_event'])}{sur_txt}."
        else:
            anlass = ("- Anlass: **keine Quartalszahlen gefunden** – Nachricht prüfen. "
                      "Übernahmeangebot → kein Trade (Kurs klebt am Angebotspreis).")
        return [f"- Kursreaktion: Eröffnung {fmt.num(pk['gap_open_atr'], 1)} ATR über dem Vortagesschluss, "
                f"Tagesplus {fmt.pct(pk['move_pct'], 1, sign=True)} ({fmt.num(pk['move_atr'], 1)} ATR) bei "
                f"{fmt.num(pk['vol_ratio'], 1)}× Volumen, Schluss bei {fmt.num(pk['clv'] * 100, 0)} % der Tagesspanne.",
                anlass,
                f"- Trend: Schluss {fmt.pct(pk['pct_above_sma200'])} über der 200-Tage-Linie.",
                rs,
                "- Hypothese: Starke, bestätigte Reaktionen laufen manchmal nach (Post-Earnings-Drift). Bei "
                "Großkonzernen ist dieser Effekt laut Forschung stark geschrumpft – nur mit eigenem Testnachweis handeln."]
    ema_word = "unter" if pk["ema_dist_atr"] < 0 else "über"
    return [f"- Trend intakt: Schluss {fmt.pct(pk['pct_above_sma200'])} über der 200-Tage-Linie, 50-Tage-Linie "
            f"{fmt.pct(pk['sma_mid_slope_pct'], sign=True)} in {p.sma_mid_slope_days} Handelstagen.",
            rs,
            f"- Rücksetzer: {int(pk['down_days'])} Tage in Folge tiefer geschlossen, Tief "
            f"{fmt.num(abs(pk['ema_dist_atr']), 1)} ATR {ema_word} der 20-Tage-EMA.",
            f"- Umkehr: Schluss über dem Vortageshoch bei {fmt.num(pk['clv'] * 100, 0)} % der Tagesspanne, "
            f"Volumen {fmt.num(pk['vol_ratio'], 1)}× Durchschnitt."]


def pick_markdown(i: int, pk: dict, p: Params, r_eur: float, fx: float | None, ratios: list[float],
                  bt_setup: dict | None = None) -> str:
    cur = pk["currency"]

    def P(x):
        return fmt.price(x, cur)

    land = "USA" if pk["market"] == "US" else "Deutschland"
    target = pk["entry"] + p.target_r * pk["risk"]
    time_stop = fmt.add_sessions(pk["entry_day"], p.time_stop_days - 1, pk["market"])
    lev = pk["entry"] / (pk["entry"] - pk["ko_mid"])
    ko_r = (pk["entry"] - pk["ko_mid"]) / pk["risk"]
    stop_basis = "unter dem Tief des Reaktionstags" if pk["setup"] == "earnings_gap" else "unter dem Rücksetzer-Tief"
    L = [f"### {i}. {pk['ticker']} – {esc(pk['name'])} ({land}) · Setup: {SETUP_NAMES[pk['setup']]}", ""]
    L.append("**Warum (regelbasiert – keine Kursprognose):**")
    L += why_lines(pk, p)
    L.append("")
    L.append(f"**Wann:** {fmt.weekday_date(pk['entry_day'])} ab {fmt.entry_time_berlin(pk['market'], pk['entry_day'])} "
             f"Uhr (Berliner Zeit), nur per Limit. Kaufen nur, solange {pk['ticker']} **≤ {P(pk['entry'])}** notiert. "
             "Läuft der Kurs ohne Rücksetzer darüber weg, verfällt der Trade – nicht hinterherlaufen.")
    L.append("")
    L.append(f"**Wie (Ausstieg steht vorher fest):** Stop **{P(pk['stop'])}** {stop_basis} "
             f"({fmt.pct(-pk['risk'] / pk['entry'] * 100)}, {fmt.num(pk['risk_atr'], 1)} ATR). "
             f"Ziel 1 **{P(target)}** (+{fmt.num(p.target_r, 0)} R bei Einstieg zum Limit): "
             f"{fmt.num(p.partial_fraction * 100, 0)} % verkaufen, Rest-Stop auf Einstand, danach unter das "
             f"{p.trail_low_days}-Tages-Tief nachziehen. Zeitstop: Schlusskurs am {fmt.date(time_stop)}, "
             "falls Ziel 1 bis dahin nicht erreicht ist.")
    L.append("")
    dist = (pk["entry"] - pk["ko_mid"]) / pk["entry"] * 100
    L.append(f"**Was (Produkt):** Knock-out Long (Turbo/Mini-Future) auf {pk['ticker']}, Barriere zwischen "
             f"**{P(pk['ko_lo'])} und {P(pk['ko_hi'])}**, ideal um {P(pk['ko_mid'])} (≈ {fmt.pct(dist)} unter dem "
             f"Limit) → Hebel ≈ {fmt.num(lev, 1)}. Worst Case (Knock-out per Kurslücke): ≈ {fmt.num(ko_r, 1)} R.")
    if dist < 5:
        L.append(f"⚠️ Barriere nur ≈ {fmt.pct(dist)} entfernt: Schon eine normale Kurslücke kann ausknocken – "
                 "der Verlust bleibt durch die Stückzahl auf ≈ " + f"{fmt.num(ko_r, 1)} R begrenzt, tritt aber häufiger ein.")
    L.append("")
    L.append("**Wo:** Knock-out mit deutscher WKN/ISIN – außerbörslich direkt beim Emittenten über deinen Broker "
             "oder an einer Börse wie Stuttgart oder gettex. Den Spread vor dem Kauf mit dem Tabellenwert unten "
             "vergleichen." + (" Bei US-Titeln sind die Kurse vor dem US-Börsenstart oft breit gestellt – deshalb "
                               "erst ab der genannten Uhrzeit handeln." if pk["market"] == "US" else ""))
    L.append("")
    up_pct = (target / pk["entry"] - 1) * 100
    dn_pct = pk["risk"] / pk["entry"] * 100
    L.append(f"**Wie stark (Potenzial vs. Risiko):** Ziel 1 liegt **{fmt.pct(up_pct, 1, sign=True)}** über dem Limit – "
             f"im Zertifikat mit Hebel ≈ {fmt.num(lev, 1)} rund **{fmt.pct(up_pct * lev, 0, sign=True)}**. "
             f"Der Stop kostet {fmt.pct(-dn_pct, 1)} bzw. ≈ {fmt.pct(-dn_pct * lev, 0)} im Zertifikat (= 1 R). "
             f"Chance-Risiko bis Ziel 1 = {fmt.num(p.target_r, 0)} : 1; die zweite Hälfte läuft danach ohne feste "
             "Obergrenze mit nachgezogenem Stop.")
    if bt_setup and bt_setup.get("n"):
        hit = bt_setup.get("target_hit_rate")
        hit_txt = f"Ziel 1 in {fmt.pct(hit * 100, 0)} der Fälle erreicht, " if hit is not None else ""
        L.append(f"Erfahrungswerte dieses Setups im Backtest ({bt_setup['n']} Trades): {hit_txt}Trefferquote "
                 f"{fmt.pct(bt_setup['win_rate'] * 100, 0)}, Ø {fmt.r(bt_setup['expectancy_r'])} je Trade – ein "
                 "Durchschnitt über viele Trades, keine Prognose für diesen einen.")
    else:
        L.append("Erfahrungswerte (Trefferquote, wie oft Ziel 1 erreicht wird) erscheinen hier nach dem ersten Backtest-Lauf.")
    sz, shares = sizing(pk, r_eur, fx, ratios)
    L.append("")
    if sz:
        L.append(f"Positionsgröße für 1 R = {fmt.price(r_eur, 'EUR')} (entspricht ≈ {fmt.num(shares, 1)} Aktien):")
        L.append("")
        L.append("| Bezugsverh. | Stück | Einsatz ≈ | Stopabstand je Zert. | max. Spread |")
        L.append("|---|---|---|---|---|")
        for s in sz:
            if s["n"] < 1:
                L.append(f"| {fmt.num(s['ratio'], 2)} | – | 1 Stück wäre mehr als 1 R | "
                         f"{fmt.price(s['stop_dist'], 'EUR', 3)} | – |")
            else:
                L.append(f"| {fmt.num(s['ratio'], 2)} | {s['n']} | {fmt.price(s['invest'], 'EUR')} | "
                         f"{fmt.price(s['stop_dist'], 'EUR', 3)} | {fmt.price(s['max_spread'], 'EUR', 3)} |")
    else:
        L.append("⚠️ EUR/USD-Kurs fehlte – Stückzahl bitte selbst berechnen (Formel im README).")
    L.append("")
    checks = []
    if pk["setup"] == "pullback":
        if pk.get("earnings_known") and pk.get("earnings") is not None:
            runner_end = fmt.add_sessions(pk["entry_day"], p.runner_max_days - 1, pk["market"])
            extra = (" ⚠️ fällt in den Runner-Zeitraum → Rest spätestens am Vortag schließen"
                     if pk["earnings"] <= runner_end else "")
            checks.append(f"nächste Zahlen {fmt.date(pk['earnings'])} ✓{extra}")
        else:
            checks.append("nächster Earnings-Termin unbekannt ⚠️ vor dem Kauf selbst prüfen")
    if pk.get("corr_with"):
        checks.append(f"max. Korrelation zu offenen Positionen {fmt.num(pk['corr'])} ({pk['corr_with']}) ✓")
    elif pk.get("mode") == "trade":
        checks.append("keine offenen Positionen zum Abgleich ✓")
    checks.append("Event-Kalender frei ✓")
    L.append("**Checks:** " + " · ".join(checks))
    news = pk.get("news") or []
    if news:
        L.append("")
        L.append("**Schlagzeilen (ungeprüft – nur als Veto-Check: Übernahme, Klage, Gewinnwarnung):**")
        for it in news:
            src = " – ".join(x for x in [esc(it.get("provider", "")), it.get("date", "")] if x)
            tail = f" ({src})" if src else ""
            L.append(f"- [{esc(it['title'])}]({it['url']}){tail}" if it.get("url") else f"- {esc(it['title'])}{tail}")
    return "\n".join(L)


def observe_markdown(pk: dict) -> str:
    cur = pk["currency"]
    if pk.get("trigger_event") is not None:
        sur = fnum(pk.get("trigger_surprise"))
        anlass = f"Zahlen vom {fmt.date_short(pk['trigger_event'])}" + (
            f", EPS {fmt.pct(sur, 0, sign=True)}" if math.isfinite(sur) else "")
    else:
        anlass = "Anlass unbekannt"
    return (f"- **{pk['ticker']}** ({esc(pk['name'])}): {fmt.pct(pk['move_pct'], 1, sign=True)} bei "
            f"{fmt.num(pk['vol_ratio'], 1)}× Volumen, {anlass} · Paper-Einstieg ≤ {fmt.price(pk['entry'], cur)}, "
            f"Stop {fmt.price(pk['stop'], cur)}, KO-Korridor {fmt.num(pk['ko_lo'])}–{fmt.price(pk['ko_hi'], cur)}")


def position_line(row: dict, p: Params) -> str:
    cur = row["currency"]

    def P(x):
        return fmt.price(fnum(x), cur)

    tag = SETUP_NAMES.get(row["setup"], row["setup"]) + (" · Beobachtung" if row["mode"] == "observe" else "")
    if row["status"] == "pending":
        return (f"| {row['ticker']} | {tag} | wartet auf Einstieg | Limit ≤ {P(row['entry'])} | {P(row['stop'])} | "
                f"Barriere {P(row['ko_lo'])}–{P(row['ko_hi'])} | – |")
    fill_date = row["fill_date"]
    if row["partial_date"]:
        end = fmt.add_sessions(fill_date, p.runner_max_days - 1, row["market"])
        info = f"Runner – Teilgewinn am {fmt.date_short(row['partial_date'])}, spätestens raus ≈ {fmt.date_short(end)}"
    else:
        ts = fmt.add_sessions(fill_date, p.time_stop_days - 1, row["market"])
        info = f"Ziel 1 {P(row['target'])} · Zeitstop ≈ {fmt.date_short(ts)}"
    return (f"| {row['ticker']} | {tag} | offen seit {fmt.date_short(fill_date)} zu {P(row['fill_price'])} | "
            f"– | **{P(row['current_stop'])}** | {info} | {fmt.r(fnum(row['unrealized_r']))} |")


def radar_markdown(upcoming: list[dict], recent: list[dict], cand_ids: set) -> list[str]:
    L = ["## Earnings-Radar – nächste Tage", ""]
    L.append("Kein Positionierungs-Tipp: Vor den Zahlen ist die **Größe** der Bewegung grob abschätzbar, die "
             "**Richtung** nicht verlässlich. Ein Knock-out über die Zahlen zu halten, ist ein Münzwurf mit "
             "Totalverlust-Risiko. Gehandelt wird – wenn überhaupt – erst danach (Setup „Earnings-Gap“).")
    L.append("")
    if upcoming:
        L.append("| Termin | Titel | Ø Reaktion | ↑ / ↓ | größte | Optionen erwarten | KO-Hebel 10 wäre raus (long · short) | EPS > Schätzung |")
        L.append("|---|---|---|---|---|---|---|---|")
        for u in upcoming:
            s = u.get("stats")
            when = f"{fmt.WEEKDAYS[pd.Timestamp(u['date']).weekday()]} {fmt.date_short(u['date'])} {u['timing']}"
            implied = f"±{fmt.num(u['implied'][0], 1)} %" if u.get("implied") else "–"
            if s:
                beats = f"{s['beats']}/{s['beat_n']}" if s.get("beat_n") else "–"
                L.append(f"| {when} | {u['ticker']} {esc(u['name'])} | ±{fmt.num(s['avg_abs'], 1)} % | "
                         f"{s['up']} / {s['down']} | {fmt.num(s['max_abs'], 1)} % | {implied} | "
                         f"{s['ko_long'][10]}/{s['n']} · {s['ko_short'][10]}/{s['n']} | {beats} |")
            else:
                L.append(f"| {when} | {u['ticker']} {esc(u['name'])} | – | – | – | {implied} | – | – |")
        L.append("")
        L.append("*↑/↓ = Richtung der letzten Reaktionen (Schluss Reaktionstag vs. Vortag). Die Verteilung früherer "
                 "Richtungen sagt die nächste kaum voraus. „KO-Hebel 10 wäre raus“ = Tagestief bzw. -hoch lag ≥ 10 % "
                 "vom Vortagesschluss entfernt → ein Knock-out mit ~10 % Barriereabstand wäre wertlos gewesen.*")
    else:
        L.append("Keine Termine im Universum gefunden (oder Kalender nicht abrufbar).")
    if recent:
        L.append("")
        L.append("**Zuletzt berichtet** – Beat heißt nicht automatisch steigender Kurs:")
        L.append("")
        L.append("| Titel | Reaktionstag | EPS vs. Schätzung | Reaktion |")
        L.append("|---|---|---|---|")
        for r in recent:
            sur = f"{fmt.pct(r['surprise'], 1, sign=True)}" if math.isfinite(fnum(r["surprise"])) else "–"
            mark = " · Earnings-Gap-Setup ✅" if f"{r['ticker']}_{dstr(r['day'])}" in cand_ids else ""
            L.append(f"| {r['ticker']} {esc(r['name'])} | {fmt.date_short(r['day'])} | {sur} | "
                     f"{fmt.pct(r['move'], 1, sign=True)}{mark} |")
    L.append("")
    return L


def build_report(ctx: dict) -> str:
    p: Params = ctx["p"]
    L = [f"# Hebel-Scanner · {fmt.weekday_date(ctx['report_date'])}", ""]
    asof_txt = " · ".join(f"{BENCH_NAMES[m]} {fmt.date(d)}" for m, d in ctx["as_of"].items())
    fx_txt = f"EUR/USD {fmt.num(ctx['fx'], 4)}" if ctx["fx"] else "EUR/USD fehlt"
    L.append(f"Datenstand: {asof_txt} · {fx_txt}  ")
    L.append(f"Konto {fmt.price(ctx['size'], 'EUR', 0)} · Risiko {fmt.pct(ctx['risk_pct'], 2)} je Trade → "
             f"**1 R = {fmt.price(ctx['r_eur'], 'EUR')}** · Stufe laut Regelwerk: {ctx['status']['stage']}")
    for w in ctx["warnings"]:
        L.append(f"\n> ⚠️ {w}")
    L.append("")
    L.append("## Marktampel")
    for m, g in ctx["regimes"].items():
        state = "Longs erlaubt ✅" if g["ok"] else "keine neuen Longs ⛔"
        L.append(f"- **{BENCH_NAMES[m]}** {fmt.num(g['close'], 0)} · {fmt.pct(g['dist'], 1, sign=True)} "
                 f"zur 200-Tage-Linie → {state}")
    L.append("")
    picks = ctx["picks"]
    L.append(f"## Neue Kandidaten ({len(picks)} von max. {ctx['max_new']})")
    L.append("")
    if picks:
        L.append("**Ablauf am Einstiegstag:** (1) Produkt suchen: Knock-out Long mit Barriere im Korridor und "
                 "Spread ≤ Tabellenwert. (2) Ab der genannten Uhrzeit Limit-Order – nur solange der Basiswert "
                 "unter dem Limit notiert. (3) Sofort danach Stop-Order: Kaufkurs minus „Stopabstand je Zert.“. "
                 "(4) Kursalarm auf Ziel 1 setzen.")
        L.append("")
        per_setup = (ctx.get("bt") or {}).get("per_setup") or {}
        for i, pk in enumerate(picks, 1):
            L.append(pick_markdown(i, pk, p, ctx["r_eur"], ctx["fx"], ctx["ratios"], per_setup.get(pk["setup"])))
            L.append("")
    else:
        L.append("Heute kein Trade. " + ctx["no_pick_reason"] + " Kein Signal ist ein gültiges Ergebnis – "
                 "das System wartet auf Setups mit Vorteil, statt Trades zu erzwingen.")
        L.append("")
    if ctx["observed"]:
        L.append("## Beobachtung: Earnings-Gap-Signale (nur Paper – nicht handeln)")
        L.append("")
        L.append("Diese Signale werden automatisch im Journal mitgeschrieben, bis Backtest und Forward-Test zeigen, "
                 "ob das Setup einen Vorteil hat. Freischalten: in `config.yaml` `earnings_gap: trade`.")
        L.append("")
        for pk in ctx["observed"]:
            L.append(observe_markdown(pk))
        L.append("")
    shown = {pk["id"] for pk in picks} | {pk["id"] for pk in ctx["observed"]}
    active = [r for r in ctx["rows"] if r["status"] in ACTIVE and r["id"] not in shown]
    L.append(f"## Offene Paper-Positionen ({len(active)})")
    if active:
        L.append("")
        L.append("| Titel | Setup | Status | Limit | Stop heute | Info | R |")
        L.append("|---|---|---|---|---|---|---|")
        for row in active:
            L.append(position_line(row, p))
    else:
        L.append("Keine.")
    L.append("")
    if ctx["changes"]:
        L.append("## Veränderungen seit dem letzten Lauf")
        for e in ctx["changes"]:
            cur = e["currency"]
            tag = " (Beobachtung)" if e["mode"] == "observe" else ""
            if e["status"] == "closed":
                L.append(f"- {e['ticker']}{tag}: geschlossen am {fmt.date(e['exit_date'])} ({e['exit_reason']}) → "
                         f"**{fmt.r(fnum(e['r_net']))}** nach Kosten")
            elif e["status"] == "not_filled":
                L.append(f"- {e['ticker']}{tag}: kein Einstieg ({e['exit_reason']})")
            elif e["partial_date"]:
                L.append(f"- {e['ticker']}{tag}: Ziel 1 erreicht am {fmt.date(e['partial_date'])} – Teilgewinn, "
                         f"Rest-Stop jetzt {fmt.price(fnum(e['current_stop']), cur)}")
            elif e["status"] == "open":
                L.append(f"- {e['ticker']}{tag}: eingestiegen am {fmt.date(e['fill_date'])} zu "
                         f"{fmt.price(fnum(e['fill_price']), cur)}")
        L.append("")
    if ctx["watch"]:
        L.append("## Nicht gewählt (Transparenz)")
        for w in ctx["watch"]:
            L.append(f"- {w['ticker']} ({esc(w['name'])}, {SETUP_NAMES[w['setup']]}, RS {fmt.num(w['rs_pct'], 0)} %): "
                     f"{w['reason']}")
        L.append("")
    L += radar_markdown(ctx["radar"], ctx["recent"], ctx["cand_ids"])
    L.append("## System-Status")
    for line in ctx["status"]["lines"]:
        L.append(f"- {line}")
    obs = ctx["obs_stats"]
    if obs.get("n"):
        L.append(f"- Beobachtung Earnings-Gap (Paper): {obs['n']} Trades, Erwartungswert {fmt.r(obs['expectancy_r'])}, "
                 f"Trefferquote {fmt.pct(obs['win_rate'] * 100, 0)}.")
    L.append(f"- **Risikostufe laut Regelwerk: {ctx['status']['stage']}**")
    L.append("")
    L.append("---")
    L.append("Regelbasierte Kandidaten aus öffentlichen Kursdaten – keine Anlageberatung und keine Garantie. "
             "Hebelprodukte können wertlos verfallen. Daten ohne Gewähr (Yahoo Finance).")
    return "\n".join(L)


def build_telegram(ctx: dict) -> str:
    L = [f"📊 Hebel-Scanner · {fmt.weekday_date(ctx['report_date'])}"]
    L.append("Ampel: " + " · ".join(f"{BENCH_NAMES[m]} {'✅' if g['ok'] else '⛔'}" for m, g in ctx["regimes"].items()))
    picks = ctx["picks"]
    if picks:
        L.append(f"Neu ({len(picks)}):")
        for i, pk in enumerate(picks, 1):
            cur = pk["currency"]
            L.append(f"{i}) {pk['ticker']} {pk['name']} [{SETUP_NAMES[pk['setup']]}] – "
                     f"Einstieg {fmt.weekday_date(pk['entry_day'])[:10]}")
            L.append(f"   Limit ≤ {fmt.price(pk['entry'], cur)} · Stop {fmt.price(pk['stop'], cur)}")
            L.append(f"   KO-Barriere {fmt.num(pk['ko_lo'])}–{fmt.price(pk['ko_hi'], cur)} · RS {fmt.num(pk['rs_pct'], 0)} %")
    else:
        L.append("Kein neuer Kandidat. " + ctx["no_pick_reason"])
    if ctx["observed"]:
        L.append("Beobachtung (nur Paper): " + ", ".join(pk["ticker"] for pk in ctx["observed"]))
    closed = [e for e in ctx["changes"] if e["status"] == "closed" and e["mode"] == "trade"]
    active = [r for r in ctx["rows"] if r["status"] in ACTIVE and r["mode"] == "trade"]
    line = f"Offen/wartend: {len(active)}"
    if closed:
        line += " · Geschlossen: " + ", ".join(f"{e['ticker']} {fmt.r(fnum(e['r_net']))}" for e in closed)
    L.append(line)
    if ctx["radar"]:
        top = ctx["radar"][:5]
        L.append("Zahlen bald: " + ", ".join(
            f"{u['ticker']} {fmt.WEEKDAYS[pd.Timestamp(u['date']).weekday()]}"
            + (f" (Ø ±{fmt.num(u['stats']['avg_abs'], 0)} %)" if u.get("stats") else "") for u in top))
    fwd = ctx["fwd"]
    if fwd.get("n"):
        L.append(f"Forward: {fwd['n']} Trades, E {fmt.r(fwd['expectancy_r'])}")
    L.append(f"Stufe: {ctx['status']['stage']}")
    if ctx.get("report_url"):
        L.append(ctx["report_url"])
    return "\n".join(L)


def send_telegram(text: str) -> bool | None:
    """True = gesendet, False = Fehler, None = nicht eingerichtet."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            log.warning("Telegram: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("Telegram fehlgeschlagen: %s", exc)
        return False


EVENT_KINDS = {"Fed": ("Fed-Zinsentscheid", "federalreserve.gov"),
               "EZB": ("EZB-Zinsentscheid", "ecb.europa.eu"),
               "Inflation": ("US-Inflation", "bls.gov/schedule")}


def calendar_warnings(events_path: Path, holidays_path: Path, today: pd.Timestamp,
                      event_days: int = 35, holiday_days: int = 60) -> list[str]:
    """Warnt, bevor events.csv oder holidays.csv auslaufen (sonst fehlen Sperrtage still und leise)."""
    out = []
    try:
        ev = pd.read_csv(events_path, comment="#", dtype=str, keep_default_na=False)
        ev["d"] = pd.to_datetime(ev["date"], errors="coerce")
        for key, (label, src) in EVENT_KINDS.items():
            last = ev.loc[ev["event"].str.contains(key, case=False), "d"].max()
            if pd.isna(last) or last < today + pd.Timedelta(days=event_days):
                when = f"nach dem {fmt.date(last)}" if pd.notna(last) else "überhaupt"
                out.append(f"events.csv: keine Termine „{label}“ {when} eingetragen – bitte ergänzen ({src}).")
    except (OSError, ValueError, KeyError):
        out.append("events.csv fehlt oder ist nicht lesbar – Event-Sperre inaktiv.")
    try:
        hol = pd.read_csv(holidays_path, comment="#", dtype=str)
        hol["d"] = pd.to_datetime(hol["date"], errors="coerce")
        for m, grp in hol.groupby("market"):
            last = grp["d"].max()
            if last < today + pd.Timedelta(days=holiday_days):
                out.append(f"holidays.csv: Börsenfeiertage {m} nur bis {fmt.date(last)} – bitte ergänzen.")
    except (OSError, ValueError, KeyError):
        out.append("holidays.csv fehlt oder ist nicht lesbar – Einstiegsdaten ohne Feiertage berechnet.")
    return out


# --------------------------------------------------------------------------- Hauptablauf

def _stats_for(rows: list[dict], mode: str) -> dict:
    closed = [r for r in rows if r["status"] == "closed" and r["mode"] == mode]
    if not closed:
        return {"n": 0}
    df = pd.DataFrame([{"r_net": fnum(r["r_net"]), "exit_date": pd.Timestamp(r["exit_date"]),
                        "date": pd.Timestamp(r["signal_date"])} for r in closed])
    return trade_stats(df)


def run_daily(cfg: dict, loader, root: Path, now: pd.Timestamp | None = None) -> int:
    root = Path(root)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    today = now.tz_convert("Europe/Berlin").normalize().tz_localize(None)
    fmt.load_holidays(root / "holidays.csv")
    p = params_from_config(cfg)
    modes = setup_modes(cfg)
    active_setups = tuple(s for s, m in modes.items() if m != "off")
    acc = cfg.get("account") or {}
    size = float(acc.get("size_eur", 10000))
    risk_pct = float(acc.get("risk_per_trade_pct", 0.25))
    r_eur = size * risk_pct / 100.0
    ratios = [float(x) for x in acc.get("ratios", [0.1, 1.0])]
    warnings: list[str] = []

    uni = loader.universe()
    meta = uni.set_index("ticker")
    markets = [m for m in BENCHMARKS if m in set(uni["market"])]
    log.info("Universum: %d Titel", len(uni))

    bench_raw = loader.ohlc([BENCHMARKS[m] for m in markets], period="2y")
    frames = loader.ohlc(uni["ticker"].tolist(), period="2y")
    failed = list(loader.failed)
    fx = loader.fx_eurusd()
    if failed:
        warnings.append(f"{len(failed)} Titel ohne Kursdaten übersprungen (z. B. {', '.join(failed[:5])}).")
    if fx is None and "US" in markets:
        warnings.append("EUR/USD-Kurs fehlt – Stückzahlen für US-Titel nicht berechnet.")

    bench_ind = {}
    for m in markets:
        b = fmt.drop_incomplete_bar(bench_raw.get(BENCHMARKS[m]), m, now)
        if b is not None and len(b) > p.sma_long:
            bench_ind[m] = add_indicators(b, p)
        else:
            warnings.append(f"Keine Indexdaten für {BENCH_NAMES[m]} – dieser Markt wird heute nicht gescannt.")
    if not bench_ind:
        log.error("Keine Indexdaten – Abbruch.")
        _write_outputs(root, today, "# Hebel-Scanner\n\n⚠️ Datenfehler: keine Indexdaten abrufbar. "
                                    "Der nächste Lauf versucht es erneut.\n", None)
        send_telegram("⚠️ Hebel-Scanner: Datenfehler (keine Indexdaten). Der nächste Lauf versucht es erneut.")
        return 1

    frames = {t: fmt.drop_incomplete_bar(df, meta.at[t, "market"], now) for t, df in frames.items()
              if t in meta.index and meta.at[t, "market"] in bench_ind}
    cands, ind_frames, _ = build_candidates(frames, meta, bench_ind, p, active_setups)
    rets = returns_matrix(ind_frames)
    as_of = {m: b.index[-1] for m, b in bench_ind.items()}
    report_date = max(as_of.values())
    if (today - report_date).days > 4:
        warnings.append(f"Letzte Kursdaten vom {fmt.date(report_date)} – Datenquelle prüfen.")

    ec = cfg.get("earnings_radar") or {}
    us_cal = None
    if "US" in bench_ind:
        us_cal = loader.earnings_calendar(min(as_of.values()) - pd.Timedelta(days=5), today + pd.Timedelta(days=21),
                                          min_mcap=float(ec.get("min_market_cap_usd", 5e9)))
        if us_cal is None or us_cal.empty:
            warnings.append("US-Earnings-Kalender nicht abrufbar – Termine werden einzeln geprüft.")
    cache = EarningsCache(root / "data" / "earnings_cache.json")

    jpath = root / "journal" / "picks.csv"
    rows = load_journal(jpath)
    changes = update_journal(rows, ind_frames, p, r_eur)

    events_path = root / ((cfg.get("filters") or {}).get("events_file", "events.csv"))
    ev = load_events(events_path)
    warnings += calendar_warnings(events_path, root / "holidays.csv", today)
    reg = regimes(bench_ind)
    state_path = root / "data" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    last_asof = state.get("as_of", {})
    fresh = {m for m, d in as_of.items() if last_asof.get(m) != dstr(d)}
    for m in set(as_of) - fresh:
        warnings.append(f"{BENCH_NAMES[m]}: keine neue Tageskerze seit dem letzten Lauf (Feiertag?) – "
                        "für diesen Markt heute keine neuen Signale.")
    picks, observed, watch, blocked, slots = select_new(cands, meta, rets, as_of, rows, cfg, p, loader, ev, modes,
                                                        us_cal, cache, today, fresh)
    n_news = int((cfg.get("filters") or {}).get("news_items", 3))
    for pk in picks:
        pk["news"] = loader.news(pk["ticker"], n_news) if n_news > 0 else []
    for pk in picks + observed:
        rows.append(pick_to_row(pk))
    save_journal(jpath, rows)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"as_of": {m: dstr(d) for m, d in as_of.items()},
                                      "last_run_utc": now.strftime("%Y-%m-%d %H:%M")}, indent=1), encoding="utf-8")

    try:
        radar, recent = build_radar(uni, ind_frames, us_cal, cache, loader, as_of, today, cfg)
    except Exception as exc:                       # der Radar darf den Tageslauf nie verhindern
        log.warning("Earnings-Radar fehlgeschlagen: %s", exc)
        radar, recent = [], []
    cache.save()

    fwd = _stats_for(rows, "trade")
    obs_stats = _stats_for(rows, "observe")
    bt_path = root / "results" / "backtest_summary.json"
    bt = json.loads(bt_path.read_text(encoding="utf-8")) if bt_path.exists() else None
    status = system_status(bt, fwd, cfg)

    fee_r = 3 * p.fee_per_order_eur / r_eur if r_eur > 0 else 0
    if fee_r > 0.05:
        warnings.append(f"Ordergebühren kosten bei 1 R = {fmt.price(r_eur, 'EUR')} rund {fmt.num(fee_r, 2)} R pro "
                        "Trade – bei kleinem Risiko je Trade frisst das einen großen Teil des Vorteils.")

    if picks:
        no_pick_reason = ""
    elif not any(g["ok"] for g in reg.values()):
        no_pick_reason = "Alle Heimatindizes liegen unter ihrer 200-Tage-Linie (Marktampel rot)."
    elif blocked:
        no_pick_reason = ("Einstiegstag ist ein Event-Tag: "
                          + "; ".join(f"{BENCH_NAMES[m]}: {v}" for m, v in blocked.items()) + ".")
    elif slots <= 0:
        no_pick_reason = "Alle Risiko-Slots sind belegt bzw. das Tageslimit ist erreicht."
    elif watch:
        no_pick_reason = "Es gab Setups, aber keines hat alle Filter bestanden (siehe unten)."
    else:
        no_pick_reason = "Kein Titel erfüllt heute alle Regeln."

    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    cand_ids = {f"{t}_{dstr(d)}" for t, d in zip(cands["ticker"], cands["date"])} if len(cands) else set()
    ctx = dict(p=p, report_date=report_date, as_of=as_of, fx=fx, size=size, risk_pct=risk_pct, r_eur=r_eur,
               ratios=ratios, warnings=warnings, regimes=reg, picks=picks, observed=observed,
               max_new=int(acc.get("max_new_per_day", 3)), no_pick_reason=no_pick_reason, rows=rows,
               changes=changes, watch=watch, status=status, fwd=fwd, obs_stats=obs_stats, radar=radar,
               recent=recent, cand_ids=cand_ids, bt=bt,
               report_url=f"{server}/{repo}/blob/main/reports/latest.md" if repo else "")
    md = build_report(ctx)
    _write_outputs(root, report_date, md, picks + observed)
    send_telegram(build_telegram(ctx))
    log.info("Fertig: %d Kandidaten, %d Beobachtungen, %d offene Positionen", len(picks), len(observed),
             sum(1 for r in rows if r["status"] in ACTIVE))
    return 0


def _write_outputs(root: Path, report_date: pd.Timestamp, md: str, picks) -> None:
    rep = root / "reports"
    (rep / f"{report_date:%Y}").mkdir(parents=True, exist_ok=True)
    (rep / "latest.md").write_text(md, encoding="utf-8")
    (rep / f"{report_date:%Y}" / f"{report_date:%Y-%m-%d}.md").write_text(md, encoding="utf-8")
    if picks is not None:
        slim = [{k: (dstr(v) if isinstance(v, pd.Timestamp) else v) for k, v in pk.items() if k != "news"}
                for pk in picks]
        (rep / "latest.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
