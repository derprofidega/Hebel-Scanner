"""Backtest mit exakt den Regeln des täglichen Scans (inkl. Kosten, Portfolio-Grenzen, Korrelation)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from . import fmt
from .config import SETUP_NAMES, params_from_config, setup_modes
from .data import BENCH_NAMES, BENCHMARKS
from .portfolio import returns_matrix, select_backtest, trade_stats
from .strategy import add_indicators, build_candidates, rs_filter, simulate_trade

log = logging.getLogger("scanner.backtest")


def simulate_all(cands: pd.DataFrame, ind_frames: dict, p, r_eur: float | None) -> pd.DataFrame:
    rows = []
    arrays = {}
    for ticker, grp in cands.groupby("ticker", sort=False):
        ind = ind_frames[ticker]
        if ticker not in arrays:
            arrays[ticker] = (ind.index, ind["Open"].to_numpy(), ind["High"].to_numpy(),
                              ind["Low"].to_numpy(), ind["Close"].to_numpy())
        idx, o, h, l, c = arrays[ticker]
        pos = idx.get_indexer(pd.DatetimeIndex(grp["date"]))
        for (_, row), i in zip(grp.iterrows(), pos):
            t = simulate_trade(idx, o, h, l, c, int(i), row["entry"], row["stop"], row["ko_mid"],
                               row["risk"], p, r_eur)
            rows.append({**row.to_dict(), **t.__dict__})
    return pd.DataFrame(rows)


def cagr(series: pd.Series, start, end) -> float:
    s = series.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if len(s) < 2:
        return float("nan")
    years = (s.index[-1] - s.index[0]).days / 365.25
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) * 100 if years > 0 else float("nan")


def run_backtest(cfg: dict, loader, root: Path, years: int | None = None,
                 today: pd.Timestamp | None = None) -> dict:
    root = Path(root)
    p = params_from_config(cfg)
    bt_cfg = cfg.get("backtest") or {}
    years = int(years or bt_cfg.get("years", 8))
    acc = cfg.get("account") or {}
    r_eur = float(acc.get("size_eur", 10000)) * float(bt_cfg.get("risk_pct_for_fees", 1.0)) / 100.0
    today = pd.Timestamp(today or pd.Timestamp.now().normalize())
    test_start = today - pd.DateOffset(years=years)
    data_start = test_start - pd.DateOffset(years=2)       # Vorlauf für 200-Tage-Linie und RS

    uni = loader.universe()
    meta = uni.set_index("ticker")
    markets = [m for m in BENCHMARKS if m in set(uni["market"])]
    log.info("Backtest %s bis %s, %d Titel", test_start.date(), today.date(), len(uni))
    bench_raw = loader.ohlc([BENCHMARKS[m] for m in markets], start=data_start)
    bench_ind = {m: add_indicators(bench_raw[BENCHMARKS[m]], p) for m in markets if BENCHMARKS[m] in bench_raw}
    frames = loader.ohlc(uni["ticker"].tolist(), start=data_start)
    frames = {t: df for t, df in frames.items() if t in meta.index and meta.at[t, "market"] in bench_ind}

    modes = setup_modes(cfg)
    active = tuple(s for s, m in modes.items() if m != "off")
    cands, ind_frames, _ = build_candidates(frames, meta, bench_ind, p, active)
    cands = rs_filter(cands[pd.DatetimeIndex(cands["date"]) >= test_start], p)
    log.info("%d Signale nach RS-Filter", len(cands))
    sims = simulate_all(cands.reset_index(drop=True), ind_frames, p, r_eur) if len(cands) else pd.DataFrame()

    out_dir = root / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    if sims.empty:
        summary = {"n": 0, "note": "Keine Signale im Testzeitraum."}
        (out_dir / "backtest_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        (out_dir / "backtest_report.md").write_text("# Backtest\n\nKeine Signale im Testzeitraum.\n", encoding="utf-8")
        return summary

    rets = returns_matrix(ind_frames)
    limits = (int(acc.get("max_open_risk_positions", 4)), int(acc.get("max_new_per_day", 3)),
              float(acc.get("max_correlation", 0.7)))
    # Jedes Setup einzeln (eigenes Portfolio) – damit sich jedes selbst beweisen muss
    per_setup, per_setup_trades = {}, {}
    for name in active:
        sub = sims[sims["setup"] == name]
        if sub.empty:
            per_setup[name] = {"n": 0, "mode": modes[name]}
            continue
        tk = select_backtest(sub, rets, *limits)
        per_setup_trades[name] = tk
        closed_s = tk[tk["status"] == "closed"]
        st_s = trade_stats(closed_s)
        st_s["mode"] = modes[name]
        if len(closed_s):
            st_s["target_hit_rate"] = float(closed_s["partial_date"].notna().mean())
            st_s["avg_days_held"] = float(pd.to_numeric(closed_s["days_held"], errors="coerce").mean())
        per_setup[name] = st_s
    # Gesamtsystem = alle Setups im Modus "trade" gemeinsam (maßgeblich für die Freigabe-Hürde)
    trade_setups = [s for s in active if modes[s] == "trade"]
    pool = sims[sims["setup"].isin(trade_setups)]
    taken = select_backtest(pool, rets, *limits) if len(pool) else pool
    filled = taken[taken["status"].isin(["closed", "open"])]
    closed = taken[taken["status"] == "closed"].copy()
    stats = trade_stats(closed) if len(closed) else {"n": 0}
    if len(closed):
        stats["target_hit_rate"] = float(closed["partial_date"].notna().mean())
    if not stats.get("n"):
        stats["note"] = "Keine abgeschlossenen Trades der Setups im Modus 'trade'."
    stats["per_setup"] = per_setup
    stats.update({
        "signals_total": int(len(sims)),
        "signals_taken": int(len(taken)),
        "fill_rate": float(len(filled) / len(taken)) if len(taken) else 0.0,
        "universe_size": int(len(frames)),
        "risk_pct_for_fees": float(bt_cfg.get("risk_pct_for_fees", 1.0)),
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    })
    for m in bench_ind:
        stats[f"bench_cagr_{m}"] = cagr(bench_ind[m]["Close"], stats.get("start", test_start), stats.get("end", today))

    all_taken = pd.concat(list(per_setup_trades.values()), ignore_index=True) if per_setup_trades else taken
    cols = ["setup", "ticker", "date", "status", "fill_date", "fill_price", "entry", "stop", "risk", "ko_mid",
            "rs_pct", "partial_date", "exit_date", "exit_reason", "r_gross", "r_costs", "r_net", "days_held"]
    if len(all_taken):
        all_taken[cols].to_csv(out_dir / "backtest_trades.csv", index=False)
    if not stats.get("n"):
        (out_dir / "backtest_summary.json").write_text(json.dumps(stats, indent=1, default=str), encoding="utf-8")
        (out_dir / "backtest_report.md").write_text(_report_setups_only(stats, cfg), encoding="utf-8")
        return stats

    closed["exit_year"] = pd.DatetimeIndex(closed["exit_date"]).year
    by_year = closed.groupby("exit_year")["r_net"].agg(["count", "mean", "sum"])
    by_reason = closed.groupby("exit_reason")["r_net"].agg(["count", "mean"]).sort_values("count", ascending=False)
    stats["by_year"] = {int(y): {"n": int(r["count"]), "e": float(r["mean"]), "sum": float(r["sum"])}
                        for y, r in by_year.iterrows()}

    (out_dir / "backtest_summary.json").write_text(json.dumps(stats, indent=1, default=str), encoding="utf-8")
    _plot_equity(closed, out_dir / "backtest_equity.png")
    (out_dir / "backtest_report.md").write_text(_report(stats, by_year, by_reason, cfg, p), encoding="utf-8")
    log.info("Backtest fertig: %d Trades, E=%.3f R", stats.get("n", 0), stats.get("expectancy_r", float("nan")))
    return stats


def _plot_equity(closed: pd.DataFrame, path: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    c = closed.sort_values("exit_date")
    eq = c["r_net"].cumsum().to_numpy()
    dd = eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 5.5), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    x = pd.DatetimeIndex(c["exit_date"])
    a1.plot(x, eq, color="#1f4e79", lw=1.6)
    a1.axhline(0, color="#999", lw=0.8)
    a1.set_ylabel("Kumuliert (R)")
    a1.set_title("Backtest – Equity-Kurve in R (nach Kosten)")
    a1.grid(alpha=0.25)
    a2.fill_between(x, dd, 0, color="#c0392b", alpha=0.45, step="post")
    a2.set_ylabel("Drawdown (R)")
    a2.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _report(st: dict, by_year, by_reason, cfg: dict, p) -> str:
    g = cfg.get("gates") or {}
    min_e = float(g.get("min_backtest_expectancy_r", 0.2))
    min_pf = float(g.get("min_backtest_profit_factor", 1.3))
    ok = st["expectancy_r"] >= min_e and st["profit_factor"] >= min_pf
    L = ["# Backtest – Hebel-Scanner", ""]
    L.append(f"Zeitraum {fmt.date(st['start'])} – {fmt.date(st['end'])} · Universum {st['universe_size']} Titel · "
             f"erstellt {st['generated']}")
    L.append("")
    L.append("## Ergebnis (in R, nach Finanzierung, Spread und Gebühren)")
    L.append("")
    L.append("| Kennzahl | Wert |")
    L.append("|---|---|")
    rows = [
        ("Signale gesamt / davon gewählt", f"{st['signals_total']} / {st['signals_taken']}"),
        ("Einstiegsquote (Limit ausgeführt)", fmt.pct(st["fill_rate"] * 100, 0)),
        ("Abgeschlossene Trades", str(st["n"])),
        ("Trefferquote", fmt.pct(st["win_rate"] * 100, 0)),
        ("Ø Gewinner / Ø Verlierer", f"{fmt.r(st['avg_win_r'])} / {fmt.r(st['avg_loss_r'])}"),
        ("**Erwartungswert je Trade**", f"**{fmt.r(st['expectancy_r'])}**"),
        ("Median je Trade", fmt.r(st["median_r"])),
        ("Profit-Faktor", fmt.num(st["profit_factor"])),
        ("Max. Drawdown", f"{fmt.num(st['max_dd_r'], 1)} R"),
        ("Längste Verlustserie", f"{st['longest_losing_streak']} Trades"),
        ("Trades pro Monat", fmt.num(st["trades_per_month"], 1)),
        ("R pro Jahr", f"{fmt.num(st['r_per_year'], 1)} R ≙ ca. {fmt.pct(st['r_per_year'], 1)} p. a. bei 1 % Risiko "
                       f"(ohne Zinseszins, vor Steuern)"),
    ]
    for m in BENCHMARKS:
        if f"bench_cagr_{m}" in st:
            rows.append((f"{BENCH_NAMES[m]} im selben Zeitraum (Kursindex)", f"{fmt.pct(st[f'bench_cagr_{m}'], 1)} p. a."))
    for k, v in rows:
        L.append(f"| {k} | {v} |")
    L.append("")
    L.append(f"**Hürde** (Erwartungswert ≥ {fmt.r(min_e)} und Profit-Faktor ≥ {fmt.num(min_pf)}): "
             + ("bestanden ✅ → weiter mit dem Forward-Test." if ok else
                "verfehlt ❌ → nicht live handeln. Nicht so lange Parameter drehen, bis es passt (Overfitting)."))
    L.append("")
    L += _setup_table(st)
    L.append("## Nach Jahren (Ausstiegsjahr)")
    L.append("")
    L.append("| Jahr | Trades | Erwartungswert | Summe |")
    L.append("|---|---|---|---|")
    for y, r in by_year.iterrows():
        L.append(f"| {y} | {int(r['count'])} | {fmt.r(r['mean'])} | {fmt.num(r['sum'], 1)} R |")
    L.append("")
    L.append("## Nach Ausstiegsgrund")
    L.append("")
    L.append("| Grund | Trades | Ø Ergebnis |")
    L.append("|---|---|---|")
    for reason, r in by_reason.iterrows():
        L.append(f"| {reason} | {int(r['count'])} | {fmt.r(r['mean'])} |")
    L.append("")
    L.append("![Equity-Kurve](backtest_equity.png)")
    L.append("")
    L.append("## Einschränkungen – bitte ernst nehmen")
    L.append("- **Survivorship-Bias:** getestet wird das *heutige* Universum. Firmen, die in der Vergangenheit "
             "abgestürzt und aus dem Index geflogen sind, fehlen. Das schönt das Ergebnis – rechne gedanklich mit "
             "deutlich weniger (Faustregel: Erwartungswert halbieren).")
    L.append("- **Keine Earnings-Sperre im Backtest** (historische Termine sind nicht zuverlässig verfügbar). "
             "Live werden Titel mit Zahlen im Haltezeitraum ausgeschlossen – das nimmt Gap-Risiken heraus.")
    L.append("- **Kostenmodell vereinfacht:** Finanzierung "
             f"{fmt.pct(p.financing_pa_pct, 1)} p. a., Spread {fmt.pct(p.spread_roundtrip_pct, 2)} je Round-Trip, "
             f"{fmt.price(p.fee_per_order_eur, 'EUR')} je Order bei 1 % Risiko. Ausführungen zum Stopkurs "
             "(außer bei Kurslücken) sind im echten Handel nicht garantiert.")
    L.append("- **Vergangenheit ≠ Zukunft.** Ein bestandener Backtest ist Voraussetzung, kein Beweis. "
             "Deshalb folgt der Forward-Test mit kleinem Risiko.")
    return "\n".join(L)


def _setup_table(st: dict) -> list[str]:
    L = ["## Je Setup (jeweils eigenes Portfolio)", "",
         "| Setup | Modus | Trades | Trefferquote | Ziel 1 erreicht | Erwartungswert | Profit-Faktor | Max-DD |",
         "|---|---|---|---|---|---|---|---|"]
    for name, v in (st.get("per_setup") or {}).items():
        if not v.get("n"):
            L.append(f"| {SETUP_NAMES.get(name, name)} | {v.get('mode', '')} | 0 | – | – | – | – | – |")
            continue
        hit = v.get("target_hit_rate")
        L.append(f"| {SETUP_NAMES.get(name, name)} | {v['mode']} | {v['n']} | {fmt.pct(v['win_rate'] * 100, 0)} | "
                 f"{fmt.pct(hit * 100, 0) if hit is not None else '–'} | {fmt.r(v['expectancy_r'])} | "
                 f"{fmt.num(v['profit_factor'])} | {fmt.num(v['max_dd_r'], 1)} R |")
    L += ["", "*Modus „observe“: wird live nur als Paper-Trade mitgeschrieben. Auf „trade“ umstellen erst, wenn "
          "das Setup hier die Hürde schafft **und** im Forward-Test bestätigt – und nicht, weil ein einzelner "
          "Parametersatz gut aussieht.*", ""]
    return L


def _report_setups_only(st: dict, cfg: dict) -> str:
    L = ["# Backtest – Hebel-Scanner", "", st.get("note", ""), ""]
    L += _setup_table(st)
    return "\n".join(L)
