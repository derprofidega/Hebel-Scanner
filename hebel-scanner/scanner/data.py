"""Datenbeschaffung über yfinance: Universum, Tageskurse, EUR/USD, Earnings-Termine, Schlagzeilen.

Hinweis: yfinance ist eine inoffizielle Schnittstelle zu Yahoo Finance. Sie ist kostenlos,
kann aber drosseln (Rate-Limit) oder zeitweise ausfallen. Deshalb: kleine Pakete, Pausen,
Wiederholungen – und fehlende Titel werden übersprungen statt zu raten.
"""
from __future__ import annotations

import io
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("scanner.data")

BENCHMARKS = {"US": "^GSPC", "DE": "^GDAXI"}
BENCH_NAMES = {"US": "S&P 500", "DE": "DAX"}
CURRENCY = {"US": "USD", "DE": "EUR"}
FIELDS = ["Open", "High", "Low", "Close", "Volume"]
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hebel-scanner/1.0 (private research tool)"}


# --------------------------------------------------------------------------- Normalisierung

def _clean(sub: pd.DataFrame) -> pd.DataFrame | None:
    cols = [c for c in FIELDS if c in sub.columns]
    if not {"Open", "High", "Low", "Close"} <= set(cols):
        return None
    df = sub[cols].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[(df["Close"] > 0) & (df["Open"] > 0)]
    if df.empty:
        return None
    if "Volume" not in df.columns:
        df["Volume"] = 0.0
    df["Volume"] = df["Volume"].fillna(0.0).astype(float)
    idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    ohlc = df[["Open", "High", "Low", "Close"]]
    df["High"] = ohlc.max(axis=1)   # Datenfehler glätten: High/Low müssen O und C einschließen
    df["Low"] = ohlc.min(axis=1)
    return df


def normalize_download(raw: pd.DataFrame | None, requested: list[str]) -> dict[str, pd.DataFrame]:
    """Macht aus dem yfinance-Ergebnis (egal ob Spalten (Ticker, Feld) oder (Feld, Ticker)) ein Dict."""
    out: dict[str, pd.DataFrame] = {}
    if raw is None or len(raw) == 0:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = {str(v) for v in raw.columns.get_level_values(0)}
        field_level = 0 if "Close" in lvl0 else 1
        tick_level = 1 - field_level
        for ticker in dict.fromkeys(raw.columns.get_level_values(tick_level)):
            df = _clean(raw.xs(ticker, axis=1, level=tick_level))
            if df is not None:
                out[str(ticker).upper()] = df
    elif len(requested) == 1:
        df = _clean(raw)
        if df is not None:
            out[requested[0].upper()] = df
    return out


# --------------------------------------------------------------------------- Loader

class YahooLoader:
    def __init__(self, cfg: dict, root: Path):
        self.cfg = cfg
        self.root = Path(root)
        d = cfg.get("data") or {}
        self.chunk = int(d.get("chunk_size", 50))
        self.pause = float(d.get("pause_seconds", 4))
        self.retries = int(d.get("retries", 3))
        self.failed: list[str] = []

    # ---- Universum
    def universe(self) -> pd.DataFrame:
        u = self.cfg.get("universe") or {}
        parts = []
        if u.get("sp500", True):
            parts.append(self._sp500())
        if u.get("dax", True):
            de = pd.read_csv(self.root / "universe" / "de.csv", comment="#")
            de["market"] = "DE"
            parts.append(de)
        extra = [{"ticker": e["ticker"], "name": e.get("name", e["ticker"]), "market": e.get("market", "US")}
                 for e in (u.get("extra") or [])]
        if extra:
            parts.append(pd.DataFrame(extra))
        uni = pd.concat(parts, ignore_index=True)
        uni["ticker"] = uni["ticker"].astype(str).str.strip().str.upper()
        uni["name"] = uni["name"].astype(str).str.strip()
        uni = uni.drop_duplicates("ticker").reset_index(drop=True)
        uni["currency"] = uni["market"].map(CURRENCY)
        exclude = {str(t).upper() for t in (u.get("exclude") or [])}
        return uni[~uni["ticker"].isin(exclude)].reset_index(drop=True)

    def _sp500(self) -> pd.DataFrame:
        cache = self.root / "universe" / "us_sp500_cache.csv"
        try:
            r = requests.get(SP500_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            table = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
            df = pd.DataFrame({
                "ticker": table["Symbol"].astype(str).str.replace(".", "-", regex=False),
                "name": table["Security"].astype(str),
            })
            if len(df) >= 400:
                df.to_csv(cache, index=False)
                df["market"] = "US"
                return df
        except Exception as exc:  # Netzwerk, Seitenstruktur geändert …
            log.warning("S&P-500-Liste nicht abrufbar (%s) – nutze Cache bzw. Fallback-Liste", exc)
        src = cache if cache.exists() else self.root / "universe" / "us_fallback.csv"
        df = pd.read_csv(src, comment="#")
        df["market"] = "US"
        return df

    # ---- Kurse
    def ohlc(self, tickers: list[str], start=None, period: str = "2y") -> dict[str, pd.DataFrame]:
        import yfinance as yf

        todo = list(dict.fromkeys(t.upper() for t in tickers))
        result: dict[str, pd.DataFrame] = {}
        for attempt in range(self.retries):
            missing: list[str] = []
            for i in range(0, len(todo), self.chunk):
                batch = todo[i:i + self.chunk]
                kwargs = dict(interval="1d", auto_adjust=False, group_by="ticker",
                              threads=True, progress=False, timeout=30)
                if start is not None:
                    kwargs["start"] = pd.Timestamp(start).strftime("%Y-%m-%d")
                else:
                    kwargs["period"] = period
                try:
                    raw = yf.download(batch, **kwargs)
                except Exception as exc:
                    log.warning("Download-Fehler (%s): %s", batch[:3], exc)
                    raw = None
                got = normalize_download(raw, batch)
                result.update(got)
                missing += [t for t in batch if t not in got]
                time.sleep(self.pause)
            if not missing:
                break
            todo = missing
            if attempt < self.retries - 1:
                wait = self.pause * 10 * (attempt + 1)
                log.info("%d Titel ohne Daten – neuer Versuch in %.0f s", len(missing), wait)
                time.sleep(wait)
        self.failed = [t for t in tickers if t.upper() not in result]
        return result

    def fx_eurusd(self) -> float | None:
        data = self.ohlc(["EURUSD=X"], period="1mo")
        df = data.get("EURUSD=X")
        return float(df["Close"].iloc[-1]) if df is not None and len(df) else None

    # ---- Termine & Schlagzeilen (nur für die wenigen Endkandidaten)
    def next_earnings(self, ticker: str, after: pd.Timestamp) -> tuple[pd.Timestamp | None, bool]:
        """(Datum, bekannt?) des nächsten Earnings-Termins ab `after`."""
        import yfinance as yf

        after = pd.Timestamp(after).normalize()
        tk = yf.Ticker(ticker)
        try:
            cal = tk.calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                future = sorted(pd.Timestamp(d) for d in dates if pd.Timestamp(d) >= after - pd.Timedelta(days=1))
                if future:
                    return future[0], True
        except Exception as exc:
            log.debug("calendar %s: %s", ticker, exc)
        try:
            ed = tk.get_earnings_dates(limit=8)
            if ed is not None and len(ed):
                idx = pd.DatetimeIndex(ed.index)
                if idx.tz is not None:
                    idx = idx.tz_localize(None)
                future = sorted(d.normalize() for d in idx if d.normalize() >= after - pd.Timedelta(days=1))
                if future:
                    return future[0], True
        except Exception as exc:
            log.debug("earnings_dates %s: %s", ticker, exc)
        return None, False

    def earnings_calendar(self, start, end, min_mcap: float = 5e9, max_pages: int = 10) -> pd.DataFrame:
        """US-Earnings-Kalender (ein Abruf je 100 Termine) inkl. EPS-Schätzung/-Ist und Überraschung."""
        import yfinance as yf

        cols = ["ticker", "company", "event", "date", "timing", "eps_est", "eps_act", "surprise_pct", "mcap"]
        s, e = pd.Timestamp(start).strftime("%Y-%m-%d"), pd.Timestamp(end).strftime("%Y-%m-%d")
        frames = []
        try:
            cal = yf.Calendars(start=s, end=e)
            for page in range(max_pages):
                df = cal.get_earnings_calendar(market_cap=min_mcap, filter_most_active=False, start=s, end=e,
                                               limit=100, offset=page * 100, force=True)
                if df is None or df.empty:
                    break
                frames.append(df.reset_index())
                if len(df) < 100:
                    break
                time.sleep(1.0)
        except Exception as exc:
            log.warning("Earnings-Kalender nicht abrufbar: %s", exc)
        if not frames:
            return pd.DataFrame(columns=cols)
        raw = pd.concat(frames, ignore_index=True)

        def col(*names):
            for n in names:
                if n in raw.columns:
                    return raw[n]
            return pd.Series([None] * len(raw))

        ev = pd.to_datetime(col("Event Start Date"), errors="coerce", utc=True)
        ev = ev.dt.tz_convert("America/New_York").dt.tz_localize(None)
        out = pd.DataFrame({
            "ticker": col("Symbol", "index").astype(str).str.upper().str.replace(".", "-", regex=False),
            "company": col("Company", "Company Name").astype(str),
            "event": ev,
            "timing": col("Timing").astype(str).str.upper(),
            "eps_est": pd.to_numeric(col("EPS Estimate"), errors="coerce"),
            "eps_act": pd.to_numeric(col("Reported EPS"), errors="coerce"),
            "surprise_pct": pd.to_numeric(col("Surprise(%)", "Surprise (%)"), errors="coerce"),
            "mcap": pd.to_numeric(col("Marketcap", "Market Cap (Intraday)"), errors="coerce"),
        })
        out = out.dropna(subset=["event"])
        out["date"] = out["event"].dt.normalize()
        return out.drop_duplicates(["ticker", "date"]).reset_index(drop=True)[cols]

    def earnings_history(self, ticker: str, limit: int = 12) -> pd.DataFrame | None:
        """Vergangene und kommende Termine (lokale Börsenzeit) mit EPS-Schätzung, -Ist und Überraschung."""
        import yfinance as yf

        try:
            ed = yf.Ticker(ticker).get_earnings_dates(limit=limit)
        except Exception as exc:
            log.debug("earnings_history %s: %s", ticker, exc)
            return None
        if ed is None or len(ed) == 0:
            return None
        idx = pd.DatetimeIndex(ed.index)
        tz = "Europe/Berlin" if ticker.upper().endswith(".DE") else "America/New_York"
        if idx.tz is not None:
            idx = idx.tz_convert(tz).tz_localize(None)

        def col(name):
            return pd.to_numeric(ed[name], errors="coerce").to_numpy() if name in ed.columns else np.full(len(ed), np.nan)

        out = pd.DataFrame({"event": idx, "eps_est": col("EPS Estimate"), "eps_act": col("Reported EPS"),
                            "surprise_pct": col("Surprise(%)")})
        return out.sort_values("event").reset_index(drop=True)

    def implied_move(self, ticker: str, event_day: pd.Timestamp, spot: float,
                     max_days_after: int = 8) -> tuple[float, pd.Timestamp] | None:
        """Vom Optionsmarkt erwartete Bewegung ≈ ATM-Straddle / Kurs (nächster Verfall nach dem Termin)."""
        import yfinance as yf

        try:
            tk = yf.Ticker(ticker)
            exps = sorted(pd.Timestamp(x) for x in (tk.options or []))
            exps = [x for x in exps if x >= pd.Timestamp(event_day).normalize()]
            if not exps or (exps[0] - pd.Timestamp(event_day)).days > max_days_after:
                return None
            chain = tk.option_chain(exps[0].strftime("%Y-%m-%d"))
            calls, puts = chain.calls, chain.puts
            strikes = sorted(set(calls["strike"]) & set(puts["strike"]))
            if not strikes:
                return None
            k = min(strikes, key=lambda x: abs(x - spot))

            def mid(df):
                r = df[df["strike"] == k].iloc[0]
                bid, ask = float(r.get("bid", 0) or 0), float(r.get("ask", 0) or 0)
                return (bid + ask) / 2 if bid > 0 and ask > 0 else float(r.get("lastPrice", 0) or 0)

            straddle = mid(calls) + mid(puts)
            return (straddle / spot * 100, exps[0]) if straddle > 0 and spot > 0 else None
        except Exception as exc:
            log.debug("implied_move %s: %s", ticker, exc)
            return None

    def news(self, ticker: str, n: int = 3) -> list[dict]:
        import yfinance as yf

        try:
            items = yf.Ticker(ticker).news or []
        except Exception as exc:
            log.debug("news %s: %s", ticker, exc)
            return []
        out = []
        for it in items:
            c = it.get("content", it) if isinstance(it, dict) else {}
            title = c.get("title") or it.get("title")
            if not title:
                continue
            provider = (c.get("provider") or {}).get("displayName") or it.get("publisher") or ""
            url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
                   or it.get("link") or "")
            date = c.get("pubDate") or c.get("displayTime") or ""
            if not date and it.get("providerPublishTime"):
                date = pd.Timestamp(it["providerPublishTime"], unit="s").isoformat()
            out.append({"title": str(title).strip(), "provider": provider, "url": url, "date": str(date)[:10]})
            if len(out) >= n:
                break
        return out
