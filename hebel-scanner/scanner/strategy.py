"""Das Regelwerk als Code. Backtest und täglicher Scan nutzen exakt dieselben Funktionen –
was getestet wird, ist auch das, was gehandelt wird."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Params


# --------------------------------------------------------------------------- Indikatoren

def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    c, h, l = df["Close"], df["High"], df["Low"]
    out = df.copy()
    prev_c = c.shift(1)
    out["sma_long"] = c.rolling(p.sma_long, min_periods=p.sma_long).mean()
    out["sma_mid"] = c.rolling(p.sma_mid, min_periods=p.sma_mid).mean()
    out["ema_fast"] = c.ewm(span=p.ema_fast, adjust=False, min_periods=p.ema_fast).mean()
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1.0 / p.atr_len, adjust=False, min_periods=p.atr_len).mean()
    vol_prior = df["Volume"].shift(1).rolling(20, min_periods=20).mean()   # Ø der 20 Vortage
    out["dollar_vol"] = (c * df["Volume"]).rolling(20, min_periods=20).mean()
    out["vol_ratio"] = df["Volume"] / vol_prior.where(vol_prior > 0)
    out["ret_rs"] = c.shift(p.rs_skip) / c.shift(p.rs_skip + p.rs_lookback) - 1.0
    down = (c < prev_c).astype(int)
    out["down_run"] = down.groupby((down == 0).cumsum()).cumsum()
    return out


def align_bench(bench_ind: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    return bench_ind.reindex(index, method="ffill", limit=5)


# --------------------------------------------------------------------------- Setups

SETUP_COLUMNS = ["date", "setup", "close", "entry", "stop", "risk", "atr", "risk_atr", "ko_lo", "ko_hi",
                 "ko_mid", "clv", "vol_ratio", "pct_above_sma200", "ema_dist_atr", "down_days",
                 "sma_mid_slope_pct", "gap_open_atr", "move_atr", "move_pct", "ret_rs", "bench_ret_rs"]
ALL_SETUPS = ("pullback", "earnings_gap")


def find_setups(ind: pd.DataFrame, bench: pd.DataFrame, p: Params, setups=ALL_SETUPS) -> pd.DataFrame:
    """Alle Tage, an denen eines der Setups inkl. Qualitätsfiltern erfüllt ist.
    `bench` = Indikatoren des Heimatindex, bereits auf ind.index ausgerichtet.
    Die relative Stärke (Perzentil im Universum) wird danach universumsweit ergänzt."""
    o, c, h, l, atr = ind["Open"], ind["Close"], ind["High"], ind["Low"], ind["atr"]
    prev_c, prev_atr = c.shift(1), atr.shift(1)

    regime = bench["Close"] > bench["sma_long"]                    # Marktampel: Heimatindex > SMA200
    liquid = (c >= p.min_price) & (ind["dollar_vol"] >= p.min_dollar_volume_m * 1e6)
    calm = (atr / c * 100.0) <= p.max_atr_pct
    rng = (h - l).where((h - l) > 0)
    clv = ((c - l) / rng).fillna(0.5)                              # Schlusslage in der Tagesspanne (0–1)
    pull_low = l.rolling(p.pullback_days + 1, min_periods=p.pullback_days + 1).min()

    extra = {
        "clv": clv,
        "vol_ratio": ind["vol_ratio"],
        "pct_above_sma200": (c / ind["sma_long"] - 1) * 100,
        "ema_dist_atr": (pull_low - ind["ema_fast"].shift(1)) / prev_atr,
        "down_days": ind["down_run"].shift(1),
        "sma_mid_slope_pct": (ind["sma_mid"] / ind["sma_mid"].shift(p.sma_mid_slope_days) - 1) * 100,
        "gap_open_atr": (o - prev_c) / prev_atr,
        "move_atr": (c - prev_c) / prev_atr,
        "move_pct": (c / prev_c - 1) * 100,
        "ret_rs": ind["ret_rs"],
        "bench_ret_rs": bench["ret_rs"],
    }
    parts = []

    if "pullback" in setups:
        trend = (c > ind["sma_long"]) & (ind["sma_mid"] > ind["sma_mid"].shift(p.sma_mid_slope_days))
        if p.require_mid_above_long:
            trend &= ind["sma_mid"] > ind["sma_long"]
        pull = ind["down_run"].shift(1) >= p.pullback_days         # n Tage in Folge tiefer geschlossen
        if p.pullback_above_mid:
            pull &= prev_c >= ind["sma_mid"].shift(1)
        near_ema = pull_low <= ind["ema_fast"].shift(1) + p.pullback_ema_tol_atr * prev_atr
        trig = clv >= p.trigger_min_clv
        if p.trigger_close_above_prior_high:
            trig &= c > h.shift(1)
        stop = l.rolling(p.stop_low_days, min_periods=p.stop_low_days).min() - p.stop_buffer_atr * atr
        mask = regime & trend & pull & near_ema & trig & liquid & calm
        parts.append(_setup_rows(ind, mask, stop, "pullback", extra, p))

    if "earnings_gap" in setups:
        gap = ((extra["gap_open_atr"] >= p.gap_min_open_atr) & (extra["move_atr"] >= p.gap_min_move_atr)
               & (ind["vol_ratio"] >= p.gap_min_vol_ratio) & (clv >= p.gap_min_clv)
               & (extra["move_pct"] <= p.gap_max_move_pct))
        if p.gap_above_sma_long:
            gap &= c > ind["sma_long"]
        stop = l - p.gap_stop_buffer_atr * atr                     # unter dem Tief des Reaktionstags
        mask = regime & gap & liquid & calm
        parts.append(_setup_rows(ind, mask, stop, "earnings_gap", extra, p))

    parts = [x for x in parts if len(x)]
    if not parts:
        return pd.DataFrame(columns=SETUP_COLUMNS)
    out = pd.concat(parts, ignore_index=True)
    # Beide Setups am selben Tag: das Ereignis (Earnings-Gap) hat Vorrang
    out["_prio"] = (out["setup"] != "earnings_gap").astype(int)
    out = out.sort_values(["date", "_prio"], kind="mergesort").drop_duplicates("date", keep="first")
    return out.drop(columns="_prio").reset_index(drop=True)[SETUP_COLUMNS]


def _setup_rows(ind, mask, stop, name, extra, p: Params) -> pd.DataFrame:
    c, atr = ind["Close"], ind["atr"]
    entry = c + p.entry_limit_atr * atr
    risk = entry - stop
    risk_atr = risk / atr
    ko_hi = stop - p.ko_buffer_atr * atr                           # mind. 1 ATR hinter dem Stop
    ko_lo = entry - p.ko_max_multiple * risk                       # KO-Totalverlust höchstens 2 R
    ok = mask & (risk_atr >= p.min_stop_atr) & (risk_atr <= p.max_stop_atr) & (ko_lo <= ko_hi)
    m = ok.to_numpy(dtype=bool)
    if not m.any():
        return pd.DataFrame(columns=SETUP_COLUMNS)
    data = {"date": ind.index[m], "setup": name, "close": c.to_numpy()[m], "entry": entry.to_numpy()[m],
            "stop": stop.to_numpy()[m], "risk": risk.to_numpy()[m], "atr": atr.to_numpy()[m],
            "risk_atr": risk_atr.to_numpy()[m], "ko_lo": ko_lo.to_numpy()[m], "ko_hi": ko_hi.to_numpy()[m]}
    data["ko_mid"] = (data["ko_lo"] + data["ko_hi"]) / 2
    for k, series in extra.items():
        data[k] = series.to_numpy()[m]
    return pd.DataFrame(data)


def build_candidates(frames: dict[str, pd.DataFrame], meta: pd.DataFrame,
                     bench_ind: dict[str, pd.DataFrame], p: Params, setups=ALL_SETUPS):
    """Setups für alle Titel + universumsweiter RS-Rang.
    Rückgabe: (Kandidaten, Indikator-Frames je Titel, RS-Perzentil-Matrix)."""
    ind_frames: dict[str, pd.DataFrame] = {}
    rows, rs_cols = [], {}
    min_len = max(p.sma_long, p.rs_lookback + p.rs_skip) + 5
    empty = pd.DataFrame(columns=["ticker"] + SETUP_COLUMNS + ["rs_pct"])
    for ticker, df in frames.items():
        if ticker not in meta.index:
            continue
        bench = bench_ind.get(meta.at[ticker, "market"])
        if bench is None or df is None or len(df) < min_len:
            continue
        ind = add_indicators(df, p)
        ind_frames[ticker] = ind
        b = align_bench(bench, ind.index)
        rs_cols[ticker] = ind["ret_rs"] - b["ret_rs"]
        found = find_setups(ind, b, p, setups) if setups else pd.DataFrame()
        if len(found):
            found.insert(0, "ticker", ticker)
            rows.append(found)

    if not rs_cols:
        return empty, ind_frames, pd.DataFrame()
    rs = pd.DataFrame(rs_cols).sort_index().ffill(limit=5)
    rs_pct = rs.rank(axis=1, pct=True) * 100.0
    if not rows:
        return empty, ind_frames, rs_pct

    cands = pd.concat(rows, ignore_index=True)
    col_pos = {t: i for i, t in enumerate(rs_pct.columns)}
    row_pos = rs_pct.index.get_indexer(pd.DatetimeIndex(cands["date"]))
    values = rs_pct.to_numpy()
    cands["rs_pct"] = [values[r, col_pos[t]] if r >= 0 else np.nan for r, t in zip(row_pos, cands["ticker"])]
    return cands, ind_frames, rs_pct


def rs_filter(cands: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Mindest-RS je Setup: Rücksetzer nur in starken Titeln, Earnings-Gaps separat einstellbar."""
    if cands.empty:
        return cands
    need = np.where(cands["setup"] == "earnings_gap", p.gap_rs_min_pct, p.rs_min_pct)
    return cands[cands["rs_pct"].fillna(-1).to_numpy() >= need]


# --------------------------------------------------------------------------- Trade-Simulation

@dataclass
class Trade:
    status: str                      # pending | not_filled | open | closed
    fill_date: pd.Timestamp | None = None
    fill_price: float = np.nan
    target: float = np.nan
    partial_date: pd.Timestamp | None = None
    exit_date: pd.Timestamp | None = None
    exit_reason: str = ""
    r_gross: float = np.nan
    r_costs: float = np.nan
    r_net: float = np.nan
    days_held: int = 0
    current_stop: float = np.nan
    last_close: float = np.nan
    unrealized_r: float = np.nan
    risk_end_date: pd.Timestamp | None = None   # ab hier bindet die Position kein Risiko mehr
    fill_attempt_date: pd.Timestamp | None = None


def simulate_trade(dates: pd.DatetimeIndex, o, h, l, c, i_sig: int, entry: float, stop: float,
                   barrier: float, risk: float, p: Params, r_eur: float | None = None) -> Trade:
    """Spielt einen Trade Tag für Tag durch – konservativ:
    * Einstieg nur am Tag nach dem Signal (Limit); Eröffnung unter dem Stop -> kein Einstieg.
    * Am Einstiegstag zählen nur Stops, keine Ziele.
    * Stop und Ziel am selben Tag -> Stop zuerst.
    * Eröffnungslücke unter die KO-Barriere -> Verlust = Einstieg - Barriere (Totalverlust des Zertifikats).
    * Ziel 1 (+2 R): Teilverkauf; Rest mit Stop mind. auf Einstand, dann unter dem n-Tages-Tief.
    * Zeitstop, falls Ziel 1 nicht erreicht; Runner mit Maximal-Haltedauer.
    Ergebnisse in R (1 R = geplantes Risiko je Stück = Limit - Stop)."""
    n = len(dates)
    j = i_sig + 1
    if j >= n:
        return Trade(status="pending")
    if o[j] <= stop:
        return Trade(status="not_filled", exit_reason="Eröffnung unter Stop", risk_end_date=dates[j],
                     fill_attempt_date=dates[j])
    if o[j] <= entry:
        fill = float(o[j])
    elif l[j] <= entry:
        fill = float(entry)
    else:
        return Trade(status="not_filled", exit_reason="Limit nicht erreicht", risk_end_date=dates[j],
                     fill_attempt_date=dates[j])

    target = fill + p.target_r * risk
    pos, pnl = 1.0, 0.0
    cur_stop = float(stop)
    partial_k: int | None = None
    exit_k: int | None = None
    reason = ""
    k = j
    while k < n:
        if k > j:
            if o[k] <= barrier:                                     # Gap durch die Barriere = Knock-out
                pnl += pos * (barrier - fill)
                pos, exit_k, reason = 0.0, k, "KO (Gap)"
                break
            if o[k] <= cur_stop:                                    # Gap unter den Stop
                pnl += pos * (o[k] - fill)
                pos, exit_k = 0.0, k
                reason = "Stop (Gap)" if partial_k is None else "Trailing-Stop (Gap)"
                break
            if partial_k is None and o[k] >= target:                # Gap über Ziel 1
                pnl += p.partial_fraction * (o[k] - fill)
                pos -= p.partial_fraction
                partial_k = k
        if pos > 1e-9 and l[k] <= cur_stop:                         # Stop intraday (vor dem Ziel)
            pnl += pos * (cur_stop - fill)
            pos, exit_k = 0.0, k
            if partial_k is None:
                reason = "Stop"
            else:
                reason = "Trailing-Stop" if cur_stop > fill + 1e-9 else "Einstand-Stop"
            break
        if partial_k is None and k > j and h[k] >= target:          # Ziel 1 intraday
            pnl += p.partial_fraction * (target - fill)
            pos -= p.partial_fraction
            partial_k = k
        held = k - j + 1
        if partial_k is None:
            if held >= p.time_stop_days:
                pnl += pos * (c[k] - fill)
                pos, exit_k, reason = 0.0, k, "Zeitstop"
                break
        else:
            if pos <= 1e-9:
                exit_k, reason = k, "Ziel"
                break
            low_n = float(np.min(l[max(j, k - p.trail_low_days + 1): k + 1]))
            new_stop = max(cur_stop, low_n)
            if p.runner_breakeven:
                new_stop = max(new_stop, fill)
            cur_stop = new_stop
            if held >= p.runner_max_days:
                pnl += pos * (c[k] - fill)
                pos, exit_k, reason = 0.0, k, "Runner-Maximaldauer"
                break
        k += 1

    last_k = exit_k if exit_k is not None else n - 1
    d_fill, d_last = dates[j], dates[last_k]
    days_full = ((dates[partial_k] if partial_k is not None else d_last) - d_fill).days
    days_runner = (d_last - dates[partial_k]).days if partial_k is not None else 0
    financing = p.financing_pa_pct / 100.0 * fill * (days_full + (1 - p.partial_fraction) * days_runner) / 365.0
    spread = p.spread_roundtrip_pct / 100.0 * fill
    costs_r = (financing + spread) / risk
    if r_eur:
        n_orders = 2 + (1 if partial_k is not None else 0)
        costs_r += n_orders * p.fee_per_order_eur / r_eur

    risk_free = dates[partial_k] if (partial_k is not None and p.runner_breakeven) else None
    t = Trade(status="closed" if exit_k is not None else "open", fill_date=d_fill, fill_price=fill,
              target=target, partial_date=dates[partial_k] if partial_k is not None else None,
              days_held=last_k - j + 1, current_stop=cur_stop, last_close=float(c[last_k]),
              fill_attempt_date=d_fill)
    if exit_k is not None:
        t.exit_date, t.exit_reason = dates[exit_k], reason
        t.r_gross = pnl / risk
        t.r_costs = costs_r
        t.r_net = t.r_gross - costs_r
        t.risk_end_date = risk_free if risk_free is not None else dates[exit_k]
    else:
        t.unrealized_r = (pnl + pos * (c[last_k] - fill)) / risk - costs_r
        t.risk_end_date = risk_free
    return t


def simulate_row(ind: pd.DataFrame, signal_date, entry, stop, barrier, risk, p: Params,
                 r_eur: float | None = None) -> Trade | None:
    idx = ind.index
    pos = idx.get_indexer([pd.Timestamp(signal_date)])[0]
    if pos < 0:
        return None
    return simulate_trade(idx, ind["Open"].to_numpy(), ind["High"].to_numpy(), ind["Low"].to_numpy(),
                          ind["Close"].to_numpy(), pos, entry, stop, barrier, risk, p, r_eur)
