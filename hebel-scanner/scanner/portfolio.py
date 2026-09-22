"""Auswahl unter mehreren Signalen und Kennzahlen – gemeinsam für Backtest und Live."""
from __future__ import annotations

import numpy as np
import pandas as pd


def returns_matrix(ind_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.DataFrame({t: f["Close"].pct_change() for t, f in ind_frames.items()}).sort_index()


def pair_corr(rets: pd.DataFrame, a: str, b: str, until, window: int = 60) -> float:
    if a not in rets.columns or b not in rets.columns:
        return np.nan
    sub = rets.loc[:pd.Timestamp(until), [a, b]].dropna().tail(window)
    if len(sub) < 20:
        return np.nan
    x, y = sub[a].to_numpy(), sub[b].to_numpy()
    if x.std() == 0 or y.std() == 0:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def max_corr_against(rets, ticker, others, until, window=60) -> tuple[float, str | None]:
    best, who = -1.0, None
    for o in others:
        if o == ticker:
            continue
        r = pair_corr(rets, ticker, o, until, window)
        if np.isfinite(r) and r > best:
            best, who = r, o
    return (best if who else np.nan), who


def rank_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Rangfolge: relative Stärke zuerst, dann Qualität der Umkehrkerze."""
    return df.sort_values(["rs_pct", "clv"], ascending=[False, False], kind="mergesort")


def select_backtest(trades: pd.DataFrame, rets: pd.DataFrame, max_positions: int, max_new: int,
                    max_corr: float, window: int = 60) -> pd.DataFrame:
    """Nimmt pro Tag die bestplatzierten Signale, solange Risiko-Slots frei sind und keine zu hohe
    Korrelation zu offenen Positionen besteht. `trades` enthält bereits die simulierten Ergebnisse."""
    trades = trades.sort_values("date", kind="mergesort")
    taken: list[int] = []
    open_pos: list[dict] = []       # {ticker, risk_end, end}
    far_future = pd.Timestamp("2262-01-01")
    for day, grp in trades.groupby("date", sort=True):
        open_pos = [o for o in open_pos if o["end"] > day]
        risk_on = [o for o in open_pos if o["risk_end"] > day]
        slots = min(max_positions - len(risk_on), max_new)
        if slots <= 0:
            continue
        held = [o["ticker"] for o in open_pos]
        chosen: list[str] = []
        for idx, row in rank_candidates(grp).iterrows():
            if len(chosen) >= slots:
                break
            if row["ticker"] in held or row["ticker"] in chosen:
                continue
            c, _ = max_corr_against(rets, row["ticker"], held + chosen, day, window)
            if np.isfinite(c) and c > max_corr:
                continue
            chosen.append(row["ticker"])
            taken.append(idx)
            end = row["exit_date"] if pd.notna(row["exit_date"]) else (
                row["fill_attempt_date"] if row["status"] == "not_filled" else far_future)
            risk_end = row["risk_end_date"] if pd.notna(row["risk_end_date"]) else end
            open_pos.append({"ticker": row["ticker"], "end": pd.Timestamp(end), "risk_end": pd.Timestamp(risk_end)})
    return trades.loc[taken]


def trade_stats(closed: pd.DataFrame) -> dict:
    """Kennzahlen in R. Erwartet Spalten r_net, exit_date (und date für den Zeitraum)."""
    closed = closed.dropna(subset=["r_net"]).sort_values("exit_date", kind="mergesort")
    n = len(closed)
    if n == 0:
        return {"n": 0}
    r = closed["r_net"].to_numpy(dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.concatenate([[0.0], np.cumsum(r)])
    drawdown = equity - np.maximum.accumulate(equity)
    streak = longest = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0
        longest = max(longest, streak)
    start = pd.Timestamp(closed["date"].min()) if "date" in closed else pd.Timestamp(closed["exit_date"].min())
    end = pd.Timestamp(closed["exit_date"].max())
    years = max((end - start).days / 365.25, 1 / 12)
    loss_sum = float(-losses.sum())
    return {
        "n": int(n),
        "win_rate": float(len(wins) / n),
        "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_r": float(losses.mean()) if len(losses) else 0.0,
        "expectancy_r": float(r.mean()),
        "median_r": float(np.median(r)),
        "profit_factor": float(wins.sum() / loss_sum) if loss_sum > 0 else float("inf"),
        "sum_r": float(r.sum()),
        "max_dd_r": float(-drawdown.min()),
        "longest_losing_streak": int(longest),
        "years": float(years),
        "trades_per_month": float(n / (years * 12)),
        "r_per_year": float(r.sum() / years),
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
    }
