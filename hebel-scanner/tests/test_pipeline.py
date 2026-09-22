import json
import shutil
from pathlib import Path

import pandas as pd
import yaml

from scanner.backtest import run_backtest
from scanner.config import ROOT
from scanner.live import load_journal, run_daily
from tests.synthetic import FakeLoader, build_world, extend


def _setup_dir(tmp_path: Path) -> tuple[Path, dict]:
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "events.csv", tmp_path / "events.csv")
    shutil.copy(ROOT / "holidays.csv", tmp_path / "holidays.csv")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    return tmp_path, cfg


def test_daily_run_end_to_end(tmp_path):
    root, cfg = _setup_dir(tmp_path)
    frames, bench, uni, cal = build_world()
    loader = FakeLoader(frames, bench, uni, cal)
    now = pd.Timestamp("2026-09-19 12:00", tz="UTC")

    assert run_daily(cfg, loader, root, now=now) == 0
    report = (root / "reports" / "latest.md").read_text(encoding="utf-8")
    rows = load_journal(root / "journal" / "picks.csv")
    ids = {r["id"]: r for r in rows}

    assert "T00_2026-09-18" in ids and ids["T00_2026-09-18"]["mode"] == "trade"
    assert ids["T00_2026-09-18"]["setup"] == "pullback"
    assert "T01_2026-09-18" in ids and ids["T01_2026-09-18"]["mode"] == "observe"
    assert "Neue Kandidaten" in report and "T00" in report
    assert "Beobachtung: Earnings-Gap-Signale" in report and "Zahlen vom 18.09." in report
    assert "Earnings-Radar" in report and "T05" in report and "±6,5 %" in report
    assert "Zuletzt berichtet" in report and "Earnings-Gap-Setup ✅" in report
    assert "Backtest fehlt" in report
    assert "**Wo:**" in report and "**Wie stark (Potenzial vs. Risiko):**" in report
    assert "im Zertifikat mit Hebel" in report
    assert "(Titel)" in report and "[Titel]" not in report          # Sonderzeichen entschärft

    # Zweiter Lauf mit denselben Daten: keine Doppel-Einträge, Hinweis auf fehlende neue Kerze
    assert run_daily(cfg, loader, root, now=now) == 0
    assert len(load_journal(root / "journal" / "picks.csv")) == len(rows)
    assert "keine neue Tageskerze" in (root / "reports" / "latest.md").read_text(encoding="utf-8")

    # Einige Tage später: Einstieg + Kursanstieg -> Journal wird fortgeschrieben
    frames2 = dict(frames)
    for t in frames2:
        frames2[t] = extend(frames[t], 8, 0.012 if t == "T00" else 0.0005, seed=sum(map(ord, t)))
    bench2 = {k: extend(v, 8, 0.001) for k, v in bench.items()}
    loader2 = FakeLoader(frames2, bench2, uni, cal)
    assert run_daily(cfg, loader2, root, now=pd.Timestamp("2026-10-01 12:00", tz="UTC")) == 0
    rows2 = {r["id"]: r for r in load_journal(root / "journal" / "picks.csv")}
    t00 = rows2["T00_2026-09-18"]
    assert t00["status"] in ("open", "closed") and t00["fill_date"]
    assert t00["partial_date"], "bei +1,2 % pro Tag muss Ziel 1 erreicht sein"
    report2 = (root / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "Veränderungen seit dem letzten Lauf" in report2
    assert (root / "data" / "earnings_cache.json").exists()


def test_backtest_end_to_end(tmp_path):
    root, cfg = _setup_dir(tmp_path)
    cfg["backtest"]["years"] = 3
    frames, bench, uni, cal = build_world(n=1400, n_us=25, n_de=4)
    loader = FakeLoader(frames, bench, uni, cal)
    stats = run_backtest(cfg, loader, root, today=pd.Timestamp("2026-09-18"))
    assert "per_setup" in stats and set(stats["per_setup"]) == {"pullback", "earnings_gap"}
    summary = json.loads((root / "results" / "backtest_summary.json").read_text(encoding="utf-8"))
    assert summary["per_setup"]["pullback"]["mode"] == "trade"
    report = (root / "results" / "backtest_report.md").read_text(encoding="utf-8")
    assert "Je Setup" in report
    if stats["per_setup"]["pullback"].get("n"):
        assert 0.0 <= stats["per_setup"]["pullback"]["target_hit_rate"] <= 1.0
        assert "Ziel 1 erreicht" in report
    if stats.get("n"):
        assert (root / "results" / "backtest_equity.png").exists()
        assert (root / "results" / "backtest_trades.csv").exists()
