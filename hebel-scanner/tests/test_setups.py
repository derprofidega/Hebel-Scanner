import numpy as np
import pandas as pd
import pytest

from scanner import fmt
from scanner.config import Params, parse_amount
from scanner.data import normalize_download
from scanner.earnings import reaction_index, reactions, summarize
from scanner.portfolio import trade_stats
from scanner.strategy import add_indicators, align_bench, find_setups, rs_filter
from tests.synthetic import make_ohlc, plant_gap, plant_pullback

P = Params()


def _bench(index):
    b = make_ohlc(n=len(index), end=index[-1], seed=1, drift=0.0005, vol=0.008, p0=4000, volume=1e9)
    return align_bench(add_indicators(b, P), index)


def test_pullback_detected_on_planted_bar():
    df = plant_pullback(make_ohlc(n=600, seed=13, drift=0.0015, vol=0.007))
    ind = add_indicators(df, P)
    s = find_setups(ind, _bench(ind.index), P)
    last = s[s["date"] == ind.index[-1]]
    assert len(last) == 1 and last["setup"].iloc[0] == "pullback"
    row = last.iloc[0]
    assert row["down_days"] >= 3 and row["clv"] >= 0.5
    assert P.min_stop_atr <= row["risk_atr"] <= P.max_stop_atr
    assert row["ko_lo"] <= row["ko_mid"] <= row["ko_hi"] < row["stop"] < row["entry"]
    assert (row["entry"] - row["ko_lo"]) == pytest.approx(2 * row["risk"])      # KO-Verlust max. 2 R
    assert row["stop"] - row["ko_hi"] == pytest.approx(row["atr"])             # Barriere ≥ 1 ATR hinter Stop


def test_gap_detected_and_has_priority():
    df = plant_gap(make_ohlc(n=600, seed=12, drift=0.0015, vol=0.007))
    ind = add_indicators(df, P)
    s = find_setups(ind, _bench(ind.index), P)
    last = s[s["date"] == ind.index[-1]]
    assert len(last) == 1 and last["setup"].iloc[0] == "earnings_gap"
    assert last["vol_ratio"].iloc[0] >= P.gap_min_vol_ratio
    assert last["move_atr"].iloc[0] >= P.gap_min_move_atr


def test_no_longs_when_benchmark_below_sma200():
    df = plant_pullback(make_ohlc(n=600, seed=13, drift=0.0015, vol=0.007))
    ind = add_indicators(df, P)
    bear = make_ohlc(n=600, end=ind.index[-1], seed=3, drift=-0.0015, vol=0.008, p0=4000, volume=1e9)
    s = find_setups(ind, align_bench(add_indicators(bear, P), ind.index), P)
    assert s.empty or not (s["date"] == ind.index[-1]).any()


def test_rs_filter_per_setup():
    c = pd.DataFrame({"setup": ["pullback", "pullback", "earnings_gap"], "rs_pct": [65.0, 80.0, 10.0]})
    out = rs_filter(c, P)
    assert list(out["rs_pct"]) == [80.0, 10.0]


def test_normalize_download_both_layouts():
    idx = pd.date_range("2026-01-01", periods=3, tz="America/New_York")
    fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    data = np.arange(3 * 12, dtype=float).reshape(3, 12) + 10
    by_ticker = pd.DataFrame(data, index=idx, columns=pd.MultiIndex.from_product([["AAA", "BBB"], fields]))
    by_field = by_ticker.swaplevel(0, 1, axis=1).sort_index(axis=1)
    for raw in (by_ticker, by_field):
        out = normalize_download(raw, ["AAA", "BBB"])
        assert set(out) == {"AAA", "BBB"}
        df = out["AAA"]
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.tz is None
        assert (df["High"] >= df[["Open", "Close"]].max(axis=1)).all()


def test_german_formatting():
    assert fmt.num(1234.5) == "1.234,50"
    assert fmt.num(-0.25, 2, sign=True) == "−0,25"
    assert fmt.price(12.3, "USD") == "12,30 $"
    assert fmt.r(1.5) == "+1,50 R"
    assert parse_amount("10.000") == 10000
    assert parse_amount("12.500,50 €") == 12500.5
    assert parse_amount("8000") == 8000


def test_entry_time_respects_dst_gap():
    # Ende Oktober: Europa schon Winterzeit, USA noch Sommerzeit -> US-Eröffnung 14:30 Berliner Zeit
    assert fmt.entry_time_berlin("US", pd.Timestamp("2026-10-27")) == "14:45"
    assert fmt.entry_time_berlin("US", pd.Timestamp("2026-11-10")) == "15:45"
    assert fmt.entry_time_berlin("DE", pd.Timestamp("2026-11-10")) == "09:15"


def test_drop_incomplete_bar():
    df = make_ohlc(n=5, end="2026-09-18")
    during = pd.Timestamp("2026-09-18 15:00", tz="UTC")     # 11:00 New York
    after = pd.Timestamp("2026-09-18 21:00", tz="UTC")      # 17:00 New York
    assert len(fmt.drop_incomplete_bar(df, "US", during)) == 4
    assert len(fmt.drop_incomplete_bar(df, "US", after)) == 5


def test_reaction_day_bmo_amc_unknown():
    idx = pd.bdate_range("2026-01-05", periods=6)
    closes = np.array([100, 100, 110, 111, 111, 111], dtype=float)
    assert reaction_index(idx, closes, idx[2] + pd.Timedelta(hours=7)) == 2      # vor Börsenstart
    assert reaction_index(idx, closes, idx[1] + pd.Timedelta(hours=16)) == 2     # nach Börsenschluss
    assert reaction_index(idx, closes, idx[1]) == 2                              # Uhrzeit unbekannt


def test_reaction_summary_counts_knockouts():
    idx = pd.bdate_range("2026-01-05", periods=4)
    df = pd.DataFrame({"Open": [100, 100, 88, 90], "High": [101, 101, 92, 91], "Low": [99, 99, 87, 89],
                       "Close": [100, 100, 90, 90], "Volume": 1e6}, index=idx)
    rx = reactions(df, [idx[1] + pd.Timedelta(hours=16)])
    s = summarize(rx)
    assert s["n"] == 1 and s["down"] == 1
    assert s["ko_long"][10] == 1 and s["ko_short"][10] == 0     # Tief −13 % -> Long-KO mit 10 % Abstand raus


def test_trade_stats():
    df = pd.DataFrame({"r_net": [2.0, -1.0, -1.0, 1.5, -1.0],
                       "exit_date": pd.bdate_range("2026-01-05", periods=5),
                       "date": pd.bdate_range("2026-01-01", periods=5)})
    st = trade_stats(df)
    assert st["n"] == 5
    assert st["expectancy_r"] == pytest.approx(0.1)
    assert st["profit_factor"] == pytest.approx(3.5 / 3.0)
    assert st["max_dd_r"] == pytest.approx(2.0)
    assert st["longest_losing_streak"] == 2


def test_holidays_shift_entry_day():
    from scanner.config import ROOT
    fmt.load_holidays(ROOT / "holidays.csv")
    assert fmt.next_session("2026-11-25", "US") == pd.Timestamp("2026-11-27")   # Thanksgiving übersprungen
    assert fmt.next_session("2026-11-25", "DE") == pd.Timestamp("2026-11-26")
    assert fmt.next_session("2026-04-02", "DE") == pd.Timestamp("2026-04-07")   # Karfreitag + Ostermontag
    assert fmt.add_sessions("2026-12-23", 1, "DE") == pd.Timestamp("2026-12-28")
