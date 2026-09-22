"""Deutsche Zahlen- und Datumsformate, Handelszeiten."""
from __future__ import annotations

import math
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
CUR_SYMBOL = {"USD": "$", "EUR": "€"}
MARKET_TZ = {"US": "America/New_York", "DE": "Europe/Berlin"}
MARKET_OPEN = {"US": (9, 30), "DE": (9, 0)}
MARKET_CLOSE_DONE = {"US": (16, 20), "DE": (17, 50)}   # ab dann gilt die Tageskerze als fertig


def num(x, nd: int = 2, sign: bool = False) -> str:
    if x is None or (isinstance(x, (float, np.floating)) and not math.isfinite(float(x))):
        return "–"
    s = f"{float(x):+,.{nd}f}" if sign else f"{float(x):,.{nd}f}"
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return s.replace("-", "−")


def price(x, currency: str, nd: int = 2) -> str:
    return f"{num(x, nd)} {CUR_SYMBOL.get(currency, currency)}"


def pct(x, nd: int = 1, sign: bool = False) -> str:
    return f"{num(x, nd, sign)} %"


def r(x, nd: int = 2) -> str:
    return f"{num(x, nd, sign=True)} R"


def date(d) -> str:
    if d is None or (not isinstance(d, str) and pd.isna(d)):
        return "–"
    return pd.Timestamp(d).strftime("%d.%m.%Y")


def date_short(d) -> str:
    if d is None or (not isinstance(d, str) and pd.isna(d)):
        return "–"
    return pd.Timestamp(d).strftime("%d.%m.")


def weekday_date(d) -> str:
    d = pd.Timestamp(d)
    return f"{WEEKDAYS[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


HOLIDAYS: dict[str, list] = {}


def load_holidays(path) -> None:
    """Börsenfeiertage aus holidays.csv (market,date,name) laden."""
    HOLIDAYS.clear()
    try:
        df = pd.read_csv(path, comment="#", dtype=str)
    except (OSError, ValueError):
        return
    for m, grp in df.groupby("market"):
        HOLIDAYS[str(m)] = sorted(np.datetime64(pd.Timestamp(d).date()) for d in grp["date"])


def _hol(market):
    return HOLIDAYS.get(market, []) if market else []


def next_session(d, market: str | None = None) -> pd.Timestamp:
    """Nächster Handelstag nach d (Wochenenden und hinterlegte Börsenfeiertage übersprungen)."""
    return pd.Timestamp(np.busday_offset(pd.Timestamp(d).date(), 1, roll="forward", holidays=_hol(market)))


def add_sessions(d, n: int, market: str | None = None) -> pd.Timestamp:
    return pd.Timestamp(np.busday_offset(pd.Timestamp(d).date(), n, roll="forward", holidays=_hol(market)))


def entry_time_berlin(market: str, day, delay_min: int = 15) -> str:
    """Uhrzeit (Berlin), ab der eingestiegen werden darf: Börseneröffnung + Wartezeit."""
    hh, mm = MARKET_OPEN[market]
    local = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hh, minute=mm,
                         tz=ZoneInfo(MARKET_TZ[market]))
    t = (local + pd.Timedelta(minutes=delay_min)).tz_convert(ZoneInfo("Europe/Berlin"))
    return t.strftime("%H:%M")


def drop_incomplete_bar(df: pd.DataFrame | None, market: str, now_utc: pd.Timestamp):
    """Entfernt die heutige Kerze, falls der Markt noch nicht geschlossen hat (manueller Lauf tagsüber)."""
    if df is None or df.empty:
        return df
    local_now = now_utc.tz_convert(ZoneInfo(MARKET_TZ[market]))
    last = df.index[-1].date()
    if last > local_now.date():
        return df.iloc[:-1]
    if last == local_now.date() and (local_now.hour, local_now.minute) < MARKET_CLOSE_DONE[market]:
        return df.iloc[:-1]
    return df
