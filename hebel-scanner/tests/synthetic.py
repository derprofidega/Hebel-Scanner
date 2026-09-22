"""Synthetische Daten für Tests (kein Internet nötig)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from scanner.config import Params
from scanner.strategy import add_indicators


def make_ohlc(n=600, end="2026-09-18", seed=0, drift=0.0006, vol=0.013, p0=100.0, volume=3e6):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=n)
    r = rng.normal(drift, vol, n)
    close = p0 * np.exp(np.cumsum(r))
    prev = np.concatenate([[p0], close[:-1]])
    open_ = prev * np.exp(rng.normal(0, vol * 0.3, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol * 0.4, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol * 0.4, n)))
    vol_ = rng.uniform(0.8, 1.2, n) * volume
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol_}, index=dates)


def _set(df, k, o, h, l, c, v=None):
    df.iloc[k, df.columns.get_loc("Open")] = o
    df.iloc[k, df.columns.get_loc("High")] = max(h, o, c)
    df.iloc[k, df.columns.get_loc("Low")] = min(l, o, c)
    df.iloc[k, df.columns.get_loc("Close")] = c
    if v is not None:
        df.iloc[k, df.columns.get_loc("Volume")] = v


def plant_pullback(df: pd.DataFrame, at: int | None = None) -> pd.DataFrame:
    """3 tiefere Schlusskurse bis knapp an die 20-EMA, dann Schluss über dem Vortageshoch (Bar `at`)."""
    df = df.copy()
    n = len(df) if at is None else at + 1
    ind = add_indicators(df.iloc[: n - 4], Params())
    atr = float(ind["atr"].iloc[-1])
    ema = float(ind["ema_fast"].iloc[-1])
    base = float(df["Close"].iloc[n - 5])
    goal = min(base - 1.2 * atr, ema + 0.1 * atr)
    closes = np.linspace(base, goal, 4)[1:]
    for k, c in zip(range(n - 4, n - 1), closes):
        o = float(df["Close"].iloc[k - 1])
        _set(df, k, o, max(o, c) + 0.15 * atr, min(o, c) - 0.15 * atr, c)
    prev_close, prev_high = float(df["Close"].iloc[n - 2]), float(df["High"].iloc[n - 2])
    c = prev_high + 0.3 * atr
    _set(df, n - 1, prev_close + 0.05 * atr, c + 0.1 * atr, prev_close - 0.2 * atr, c)
    return df


def plant_gap(df: pd.DataFrame, at: int | None = None, vol_mult: float = 5.0) -> pd.DataFrame:
    """Kurslücke nach oben mit hohem Volumen und starkem Schluss (Bar `at`)."""
    df = df.copy()
    k = len(df) - 1 if at is None else at
    ind = add_indicators(df.iloc[:k], Params())
    atr = float(ind["atr"].iloc[-1])
    c0 = float(df["Close"].iloc[k - 1])
    avg_v = float(df["Volume"].iloc[max(0, k - 20):k].mean())
    o, c = c0 + 1.0 * atr, c0 + 3.0 * atr
    _set(df, k, o, c + 0.2 * atr, o - 0.3 * atr, c, avg_v * vol_mult)
    return df


def extend(df: pd.DataFrame, n: int, step: float, seed: int = 1) -> pd.DataFrame:
    """Hängt n Handelstage mit gleichmäßiger Bewegung (step = relative Änderung je Tag) an."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(df.index[-1] + pd.offsets.BDay(1), periods=n)
    rows, c = [], float(df["Close"].iloc[-1])
    for _ in range(n):
        o = c * (1 + rng.normal(0, 0.001))
        c = o * (1 + step)
        h, l = max(o, c) * 1.004, min(o, c) * 0.996
        rows.append((o, h, l, c, float(df["Volume"].iloc[-20:].mean())))
    ext = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    return pd.concat([df, ext])


class FakeLoader:
    def __init__(self, frames: dict, bench: dict, uni: pd.DataFrame, cal: pd.DataFrame | None = None):
        self.frames, self.bench, self.uni = frames, bench, uni
        self.cal = cal if cal is not None else pd.DataFrame(
            columns=["ticker", "company", "event", "date", "timing", "eps_est", "eps_act", "surprise_pct", "mcap"])
        self.failed: list[str] = []
        self.calls = {"next_earnings": 0, "history": 0}

    def universe(self):
        return self.uni.copy()

    def ohlc(self, tickers, start=None, period="2y"):
        out = {}
        for t in tickers:
            src = self.bench.get(t, self.frames.get(t))
            if src is None:
                continue
            out[t] = src[src.index >= pd.Timestamp(start)].copy() if start is not None else src.copy()
        self.failed = [t for t in tickers if t not in out]
        return out

    def fx_eurusd(self):
        return 1.10

    def next_earnings(self, ticker, after):
        self.calls["next_earnings"] += 1
        return pd.Timestamp(after) + pd.Timedelta(days=40), True

    def earnings_calendar(self, start, end, min_mcap=5e9, max_pages=10):
        return self.cal.copy()

    def earnings_history(self, ticker, limit=12):
        self.calls["history"] += 1
        src = self.frames.get(ticker)
        if src is None:
            return None
        ev = [src.index[k] + pd.Timedelta(hours=16) for k in range(max(1, len(src) - 400), len(src) - 5, 63)]
        return pd.DataFrame({"event": ev, "eps_est": 1.0, "eps_act": 1.1, "surprise_pct": 10.0})

    def implied_move(self, ticker, event_day, spot, max_days_after=8):
        return 6.5, pd.Timestamp(event_day) + pd.Timedelta(days=2)

    def news(self, ticker, n=3):
        return [{"title": "Test [Titel] | mit Sonderzeichen", "provider": "Wire", "url": "https://example.com/x",
                 "date": "2026-09-18"}][:n]


def build_world(end="2026-09-18", n_us=30, n_de=5, n=620, seed=7):
    """Universum mit Benchmarks im Aufwärtstrend; T00 = Rücksetzer-Setup, T01 = Earnings-Gap am letzten Tag."""
    frames, rows = {}, []
    rng = np.random.default_rng(seed)
    for i in range(n_us):
        t = f"T{i:02d}"
        if i < 2:   # zwei ruhige Aufwärtstrends für die eingepflanzten Setups
            frames[t] = make_ohlc(n=n, end=end, seed=(13, 21)[i], drift=0.0015, vol=0.007)
        else:
            frames[t] = make_ohlc(n=n, end=end, seed=100 + i, drift=rng.uniform(-0.0004, 0.0008), vol=0.016)
        rows.append({"ticker": t, "name": f"Test {i}", "market": "US"})
    for i in range(n_de):
        t = f"D{i:02d}.DE"
        frames[t] = make_ohlc(n=n, end=end, seed=300 + i, drift=rng.uniform(-0.0003, 0.0008))
        rows.append({"ticker": t, "name": f"Deutsch {i}", "market": "DE"})
    frames["T00"] = plant_pullback(frames["T00"])
    frames["T01"] = plant_gap(frames["T01"])
    bench = {"^GSPC": make_ohlc(n=n, end=end, seed=1, drift=0.0005, vol=0.008, p0=4000, volume=1e9),
             "^GDAXI": make_ohlc(n=n, end=end, seed=2, drift=0.0004, vol=0.009, p0=15000, volume=1e8)}
    uni = pd.DataFrame(rows)
    uni["currency"] = uni["market"].map({"US": "USD", "DE": "EUR"})
    last = frames["T01"].index[-1]
    cal = pd.DataFrame([
        {"ticker": "T01", "company": "Test 1", "event": last + pd.Timedelta(hours=7), "date": last,
         "timing": "BMO", "eps_est": 1.0, "eps_act": 1.2, "surprise_pct": 20.0, "mcap": 5e10},
        {"ticker": "T05", "company": "Test 5", "event": last + pd.Timedelta(days=4, hours=16),
         "date": last + pd.Timedelta(days=4), "timing": "AMC", "eps_est": 2.0, "eps_act": np.nan,
         "surprise_pct": np.nan, "mcap": 8e10},
    ])
    return frames, bench, uni, cal
