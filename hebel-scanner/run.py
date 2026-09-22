"""Einstiegspunkt.

    python run.py check              # Verbindungstest (Daten, Kalender, Telegram) – zuerst ausführen
    python run.py backtest [--jahre 8]
    python run.py daily              # täglicher Scan -> reports/latest.md (+ Telegram, falls eingerichtet)
"""
from __future__ import annotations

import argparse
import logging
import sys

from scanner.config import ROOT, load_config


def main() -> int:
    ap = argparse.ArgumentParser(description="Hebel-Scanner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Verbindungstest ausführen")
    sub.add_parser("daily", help="Täglichen Scan ausführen")
    bt = sub.add_parser("backtest", help="Backtest ausführen")
    bt.add_argument("--jahre", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    from scanner.data import YahooLoader

    cfg = load_config()
    loader = YahooLoader(cfg, ROOT)
    if args.cmd == "check":
        from scanner.check import run_check
        return run_check(cfg, loader, ROOT)
    if args.cmd == "daily":
        from scanner.live import run_daily
        return run_daily(cfg, loader, ROOT)
    from scanner.backtest import run_backtest
    stats = run_backtest(cfg, loader, ROOT, years=args.jahre)
    return 0 if stats.get("n") or stats.get("per_setup") else 1


if __name__ == "__main__":
    sys.exit(main())
