import pandas as pd

from scanner.check import run_check
from scanner.config import ROOT
from scanner.live import calendar_warnings
from tests.synthetic import FakeLoader, make_ohlc

NOW = pd.Timestamp("2026-09-22 12:00", tz="UTC")


def _uni(n=60):
    rows = [{"ticker": f"X{i:02d}", "name": f"X {i}", "market": "US" if i % 3 else "DE"} for i in range(n)]
    uni = pd.DataFrame(rows)
    uni["currency"] = uni["market"].map({"US": "USD", "DE": "EUR"})
    return uni


def _loader(with_data=True):
    frames = {t: make_ohlc(n=30, end="2026-09-21", seed=i) for i, t in enumerate(["AAPL", "MSFT", "SAP.DE", "SIE.DE"])}
    bench = {"^GSPC": make_ohlc(n=30, end="2026-09-21", seed=8, p0=6000),
             "^GDAXI": make_ohlc(n=30, end="2026-09-21", seed=9, p0=25000)}
    return FakeLoader(frames if with_data else {}, bench if with_data else {}, _uni())


def test_check_ready(tmp_path):
    (tmp_path / "events.csv").write_text((ROOT / "events.csv").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "holidays.csv").write_text((ROOT / "holidays.csv").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = {"account": {"size_eur": 10000}}
    assert run_check(cfg, _loader(), tmp_path, now=NOW) == 0
    text = (tmp_path / "reports" / "verbindungstest.md").read_text(encoding="utf-8")
    assert "Startklar" in text and "Aktienkurse US" in text and "Telegram" in text


def test_check_fails_without_prices(tmp_path):
    assert run_check({}, _loader(with_data=False), tmp_path, now=NOW) == 1
    assert "Nicht startklar" in (tmp_path / "reports" / "verbindungstest.md").read_text(encoding="utf-8")


def test_calendar_warnings_current_files_are_fresh():
    assert calendar_warnings(ROOT / "events.csv", ROOT / "holidays.csv", pd.Timestamp("2026-09-22")) == []


def test_calendar_warnings_before_running_out(tmp_path):
    w = calendar_warnings(ROOT / "events.csv", ROOT / "holidays.csv", pd.Timestamp("2026-11-20"))
    assert any("US-Inflation" in x for x in w)              # CPI-Plan 2027 fehlt noch -> Warnung rechtzeitig
    assert not any("Fed" in x for x in w)
    ev = tmp_path / "events.csv"
    ev.write_text("date,event\n2020-01-01,Fed-Zinsentscheid\n", encoding="utf-8")
    w2 = calendar_warnings(ev, tmp_path / "fehlt.csv", pd.Timestamp("2026-09-22"))
    assert any("Fed-Zinsentscheid" in x for x in w2) and any("holidays.csv" in x for x in w2)
