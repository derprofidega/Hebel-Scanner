"""Earnings-Radar.

Grundidee (ehrlich): Vor den Zahlen lässt sich die *Größe* einer Bewegung grob abschätzen
(frühere Reaktionen, Optionsmarkt) – die *Richtung* nicht verlässlich. Darum zeigt der Radar
Risiko und Kontext, aber kein Richtungssignal. Gehandelt wird erst NACH den Zahlen
(Setup „Earnings-Gap“), wenn der Markt seine Richtung selbst gezeigt hat.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("scanner.earnings")

TIMING = {"BMO": "vor Börsenstart", "AMC": "nach Börsenschluss"}


# --------------------------------------------------------------------------- Termin-Cache

class EarningsCache:
    """Merkt sich den nächsten Termin je Titel ein paar Tage lang (schont das Rate-Limit)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (ValueError, OSError):
            self.data = {}

    def get(self, ticker: str, today: pd.Timestamp, max_age_days: int = 5):
        e = self.data.get(ticker)
        if not e:
            return None
        if (today - pd.Timestamp(e["fetched"])).days > max_age_days:
            return None
        if e.get("date") and pd.Timestamp(e["date"]) < today - pd.Timedelta(days=1):
            return None                                   # Termin vorbei -> neu abfragen
        return (pd.Timestamp(e["date"]) if e.get("date") else None), bool(e.get("known"))

    def put(self, ticker: str, date, known: bool, today: pd.Timestamp) -> None:
        old = self.data.get(ticker) or {}
        last = old.get("last", "")
        if old.get("date") and pd.Timestamp(old["date"]) < today:
            last = old["date"]                            # vergangenen Termin für "zuletzt berichtet" behalten
        self.data[ticker] = {"date": pd.Timestamp(date).strftime("%Y-%m-%d") if date is not None else "",
                             "known": bool(known), "fetched": today.strftime("%Y-%m-%d"), "last": last}

    def last_reported(self, ticker: str):
        e = self.data.get(ticker) or {}
        return pd.Timestamp(e["last"]) if e.get("last") else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0, sort_keys=True), encoding="utf-8")


def next_earnings(ticker, after, today, us_cal, cache: EarningsCache, loader, allow_fetch=True):
    """(Datum, bekannt?) – zuerst US-Kalender, dann Cache, dann Einzelabfrage."""
    after = pd.Timestamp(after).normalize()
    if us_cal is not None and len(us_cal):
        hit = us_cal[(us_cal["ticker"] == ticker) & (us_cal["date"] >= after - pd.Timedelta(days=1))]
        if len(hit):
            return pd.Timestamp(hit["date"].min()), True
    cached = cache.get(ticker, today)
    if cached is not None:
        return cached
    if not allow_fetch:
        return None, False
    d, known = loader.next_earnings(ticker, after)
    cache.put(ticker, d, known, today)
    return d, known


# --------------------------------------------------------------------------- Reaktionen

def reaction_index(index: pd.DatetimeIndex, closes: np.ndarray, event: pd.Timestamp) -> int | None:
    """Handelstag, an dem der Markt auf die Zahlen reagiert (BMO: gleicher Tag, AMC: nächster Tag)."""
    event = pd.Timestamp(event)
    day = event.normalize()
    k = int(index.searchsorted(day))
    if k >= len(index):
        return None
    same_day = index[k] == day
    has_time = event.hour != 0 or event.minute != 0
    if has_time and event.hour >= 12:
        k = k + 1 if same_day else k
    elif not has_time and same_day and k + 1 < len(index) and k >= 1:
        move_a = abs(closes[k] / closes[k - 1] - 1)       # Uhrzeit unbekannt: größere Bewegung gewinnt
        move_b = abs(closes[k + 1] / closes[k] - 1)
        k = k + 1 if move_b > move_a else k
    if k < 1 or k >= len(index):
        return None
    return k


def reactions(ind: pd.DataFrame, events) -> list[dict]:
    o, h, l, c = (ind[x].to_numpy() for x in ("Open", "High", "Low", "Close"))
    out, seen = [], set()
    for ev in events:
        k = reaction_index(ind.index, c, ev)
        if k is None or k in seen:
            continue
        seen.add(k)
        prev = c[k - 1]
        out.append({"event": pd.Timestamp(ev), "day": ind.index[k], "move": c[k] / prev - 1,
                    "gap": o[k] / prev - 1, "low": l[k] / prev - 1, "high": h[k] / prev - 1})
    return out


def summarize(rx: list[dict], hist: pd.DataFrame | None = None, levers=(5, 10)) -> dict | None:
    if not rx:
        return None
    moves = np.array([r["move"] for r in rx])
    s = {"n": len(rx), "avg_abs": float(np.mean(np.abs(moves)) * 100), "max_abs": float(np.max(np.abs(moves)) * 100),
         "up": int((moves > 0).sum()), "down": int((moves < 0).sum()),
         "last": [float(m * 100) for m in moves[-4:]],
         "ko_long": {L: int(sum(r["low"] <= -1.0 / L for r in rx)) for L in levers},
         "ko_short": {L: int(sum(r["high"] >= 1.0 / L for r in rx)) for L in levers}}
    if hist is not None and len(hist):
        past = hist.dropna(subset=["eps_est", "eps_act"])
        if len(past):
            s["beats"] = int((past["eps_act"] > past["eps_est"]).sum())
            s["beat_n"] = int(len(past))
    return s


# --------------------------------------------------------------------------- Radar

def build_radar(uni: pd.DataFrame, ind_frames: dict, us_cal: pd.DataFrame, cache: EarningsCache, loader,
                as_of: dict, today: pd.Timestamp, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Liefert (kommende Termine mit Reaktionsstatistik, zuletzt berichtete Titel mit Reaktion)."""
    rc = cfg.get("earnings_radar") or {}
    if not rc.get("enabled", True):
        return [], []
    days = int(rc.get("days_ahead", 7))
    max_items = int(rc.get("max_items", 12))
    de_refresh = int(rc.get("de_lookups_per_run", 15))
    want_implied = bool(rc.get("implied_move", True))
    meta = uni.set_index("ticker")
    horizon = today + pd.Timedelta(days=days)

    upcoming = []
    if us_cal is not None and len(us_cal):
        sub = us_cal[(us_cal["date"] > today) & (us_cal["date"] <= horizon) & us_cal["ticker"].isin(ind_frames)]
        for _, row in sub.iterrows():
            upcoming.append({"ticker": row["ticker"], "date": row["date"], "event": row["event"],
                             "timing": TIMING.get(row["timing"], "Uhrzeit offen"), "market": "US"})
    fetched = 0
    for t in uni.loc[uni["market"] == "DE", "ticker"]:
        if t not in ind_frames:
            continue
        cached = cache.get(t, today)
        if cached is None:
            if fetched >= de_refresh:
                continue
            fetched += 1
            d, known = loader.next_earnings(t, today)
            cache.put(t, d, known, today)
            cached = (d, known)
        d, known = cached
        if known and d is not None and today < d <= horizon:
            upcoming.append({"ticker": t, "date": d, "event": d, "timing": "Uhrzeit offen", "market": "DE"})

    for u in upcoming:
        u["dollar_vol"] = float(ind_frames[u["ticker"]]["dollar_vol"].iloc[-1])
        u["name"] = meta.at[u["ticker"], "name"] if u["ticker"] in meta.index else u["ticker"]
    upcoming = sorted(upcoming, key=lambda x: -x["dollar_vol"])[:max_items]
    upcoming.sort(key=lambda x: (x["date"], -x["dollar_vol"]))

    for u in upcoming:
        ind = ind_frames[u["ticker"]]
        hist = loader.earnings_history(u["ticker"], limit=12)
        past = hist[hist["event"] < today] if hist is not None else None
        rx = reactions(ind, past["event"].tolist()) if past is not None and len(past) else []
        u["stats"] = summarize(rx[-8:], past.tail(8) if past is not None else None)
        u["implied"] = None
        if want_implied and u["market"] == "US":
            react_day = u["date"] + pd.offsets.BDay(1) if "nach" in u["timing"] else u["date"]
            u["implied"] = loader.implied_move(u["ticker"], react_day, float(ind["Close"].iloc[-1]))

    recent = []
    window_start = min(as_of.values()) - pd.Timedelta(days=4)
    rows = []
    if us_cal is not None and len(us_cal):
        sub = us_cal[(us_cal["date"] >= window_start) & (us_cal["date"] <= max(as_of.values()))
                     & us_cal["ticker"].isin(ind_frames)]
        rows += [(r["ticker"], r["event"], r["surprise_pct"]) for _, r in sub.iterrows()]
    for t in uni.loc[uni["market"] == "DE", "ticker"]:
        last = cache.last_reported(t)
        if last is not None and t in ind_frames and window_start <= last <= max(as_of.values()):
            rows.append((t, last, np.nan))
    for t, ev, surprise in rows:
        rx = reactions(ind_frames[t], [ev])
        if rx:
            recent.append({"ticker": t, "name": meta.at[t, "name"] if t in meta.index else t, "event": ev,
                           "day": rx[0]["day"], "move": rx[0]["move"] * 100, "surprise": surprise})
    recent.sort(key=lambda x: -abs(x["move"]))
    return upcoming, recent[:10]
