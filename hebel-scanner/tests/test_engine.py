import numpy as np
import pandas as pd
import pytest

from scanner.config import Params
from scanner.strategy import simulate_trade

P0 = Params(financing_pa_pct=0.0, spread_roundtrip_pct=0.0, fee_per_order_eur=0.0)


def run(bars, entry=100.0, stop=95.0, barrier=90.0, p=P0, r_eur=None):
    """bars: Liste (O, H, L, C); Bar 0 = Signaltag."""
    arr = np.array(bars, dtype=float)
    dates = pd.bdate_range("2026-01-05", periods=len(arr))
    return simulate_trade(dates, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], 0, entry, stop, barrier,
                          entry - stop, p, r_eur)


SIGNAL = (98, 99.5, 97, 99)


def test_pending_without_next_bar():
    assert run([SIGNAL]).status == "pending"


def test_no_fill_when_price_runs_away():
    t = run([SIGNAL, (101, 103, 100.5, 102)])
    assert t.status == "not_filled" and t.exit_reason == "Limit nicht erreicht"


def test_no_entry_when_opening_below_stop():
    t = run([SIGNAL, (94, 96, 93, 95)])
    assert t.status == "not_filled" and "Stop" in t.exit_reason


def test_fill_at_open_below_limit_and_stop_same_day():
    t = run([SIGNAL, (99, 100, 94, 95)])
    assert t.status == "closed" and t.fill_price == 99
    assert t.exit_reason == "Stop"
    assert t.r_net == pytest.approx((95 - 99) / 5)


def test_fill_at_limit_intraday():
    t = run([SIGNAL, (101, 102, 99.5, 101.5), (101.5, 102, 101, 101.8)])
    assert t.fill_price == 100 and t.status == "open"


def test_gap_below_stop_fills_at_open():
    t = run([SIGNAL, (99.5, 100, 98, 99), (93, 94, 92, 93)])
    assert t.exit_reason == "Stop (Gap)"
    assert t.r_net == pytest.approx((93 - 99.5) / 5)


def test_gap_through_barrier_is_capped_knockout():
    t = run([SIGNAL, (99.5, 100, 98, 99), (85, 86, 84, 85)])
    assert t.exit_reason == "KO (Gap)"
    assert t.r_net == pytest.approx((90 - 99.5) / 5)     # Totalverlust = Einstieg - Barriere, nicht mehr


def test_stop_before_target_on_same_bar():
    t = run([SIGNAL, (99.5, 100, 98, 99), (99, 115, 94, 110)])
    assert t.exit_reason == "Stop"                       # konservativ: Stop zuerst


def test_no_target_on_entry_day():
    t = run([SIGNAL, (99.5, 115, 99, 112), (112, 113, 111, 112)])
    assert t.partial_date == pd.Timestamp("2026-01-07")  # erst am Folgetag gezählt


def test_partial_then_trailing_stop_with_breakeven_floor():
    bars = [SIGNAL, (99.5, 100, 98, 99.5),
            (100, 110, 99.8, 109),        # Ziel 1 = 99,5 + 2 × 5 = 109,5 -> Teilverkauf
            (109, 112, 108, 111),
            (111, 113, 109, 112),         # 2-Tages-Tief jetzt 108 -> Rest-Stop 108
            (112, 112.5, 104, 105)]       # Stop 108 wird gerissen
    t = run(bars)
    assert t.partial_date is not None
    assert t.exit_reason == "Trailing-Stop"
    expected = (0.5 * (109.5 - 99.5) + 0.5 * (108 - 99.5)) / 5
    assert t.r_net == pytest.approx(expected)
    assert t.risk_end_date == t.partial_date             # nach Teilverkauf kein Risiko mehr


def test_time_stop_after_ten_days():
    bars = [SIGNAL] + [(99.5, 100.5, 98.5, 100)] * 12
    t = run(bars)
    assert t.exit_reason == "Zeitstop" and t.days_held == 10
    assert t.r_net == pytest.approx((100 - 99.5) / 5)


def test_runner_max_days():
    p = Params(financing_pa_pct=0.0, spread_roundtrip_pct=0.0, fee_per_order_eur=0.0, runner_max_days=5)
    bars = [SIGNAL, (99.5, 100, 99, 99.5), (100, 112, 100, 111)] + [(111 + i, 113 + i, 110.5 + i, 112 + i) for i in range(6)]
    t = run(bars, p=p)
    assert t.exit_reason == "Runner-Maximaldauer"


def test_costs_reduce_result():
    p = Params(financing_pa_pct=10.0, spread_roundtrip_pct=0.2, fee_per_order_eur=1.0)
    bars = [SIGNAL] + [(99.5, 100.5, 98.5, 100)] * 12
    t = run(bars, p=p, r_eur=50.0)
    assert t.r_costs > 0 and t.r_net < t.r_gross
    fin = 0.10 * 99.5 * (t.exit_date - t.fill_date).days / 365
    spread = 0.002 * 99.5
    assert t.r_costs == pytest.approx((fin + spread) / 5 + 2 * 1.0 / 50.0)
