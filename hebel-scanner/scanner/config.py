"""Konfiguration laden: config.yaml + Umgebungsvariablen (GitHub-Secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Params:
    """Alle Regeln des Systems. Werte kommen aus config.yaml (Abschnitte strategy + costs)."""

    # Trend
    sma_long: int = 200
    sma_mid: int = 50
    sma_mid_slope_days: int = 10
    require_mid_above_long: bool = False
    ema_fast: int = 20
    atr_len: int = 14
    # Relative Stärke (Überrendite ggü. Heimatindex, Perzentil im Universum)
    rs_lookback: int = 126
    rs_skip: int = 5
    rs_min_pct: float = 70.0
    # Rücksetzer & Umkehr-Trigger
    pullback_days: int = 3
    pullback_ema_tol_atr: float = 0.5
    pullback_above_mid: bool = True
    trigger_close_above_prior_high: bool = True
    trigger_min_clv: float = 0.5
    # Qualitätsfilter
    min_price: float = 10.0
    min_dollar_volume_m: float = 20.0
    max_atr_pct: float = 5.0
    # Einstieg / Stop / KO-Korridor
    entry_limit_atr: float = 0.25
    stop_low_days: int = 5
    stop_buffer_atr: float = 0.5
    min_stop_atr: float = 1.0
    max_stop_atr: float = 4.0
    ko_buffer_atr: float = 1.0
    ko_max_multiple: float = 2.0
    # Ausstieg
    target_r: float = 2.0
    partial_fraction: float = 0.5
    time_stop_days: int = 10
    runner_max_days: int = 30
    runner_breakeven: bool = True
    trail_low_days: int = 2
    # Earnings-Gap (Handel NACH den Zahlen: starke, bestätigte Kursreaktion)
    gap_min_open_atr: float = 0.5
    gap_min_move_atr: float = 2.0
    gap_min_vol_ratio: float = 2.5
    gap_min_clv: float = 0.5
    gap_max_move_pct: float = 25.0
    gap_above_sma_long: bool = True
    gap_stop_buffer_atr: float = 0.25
    gap_rs_min_pct: float = 0.0
    # Kosten
    financing_pa_pct: float = 4.5
    spread_roundtrip_pct: float = 0.10
    fee_per_order_eur: float = 1.0


SETUP_NAMES = {"pullback": "Trend-Rücksetzer", "earnings_gap": "Earnings-Gap"}
MODES = ("trade", "observe", "off")


def setup_modes(cfg: dict) -> dict[str, str]:
    """{'pullback': 'trade', 'earnings_gap': 'observe'} – trade = handeln, observe = nur Paper-Tracking."""
    raw = cfg.get("setups") or {}
    out = {"pullback": str(raw.get("pullback", "trade")).lower(),
           "earnings_gap": str(raw.get("earnings_gap", "observe")).lower()}
    for k, v in out.items():
        if v not in MODES:
            raise ValueError(f"setups.{k} muss trade, observe oder off sein (ist: {v})")
    return out


def parse_amount(text: str) -> float:
    """'10.000', '10000', '10.000,50', '10 000 €' -> float."""
    s = text.replace("€", "").replace(" ", "").strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") >= 1 and all(len(part) == 3 for part in s.split(".")[1:]):
        s = s.replace(".", "")
    return float(s)


def load_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    acc = os.environ.get("ACCOUNT_EUR", "").strip()
    if acc:
        try:
            cfg.setdefault("account", {})["size_eur"] = parse_amount(acc)
        except ValueError:
            pass
    return cfg


def params_from_config(cfg: dict) -> Params:
    p = Params()
    for section in ("strategy", "costs"):
        for key, value in (cfg.get(section) or {}).items():
            if not hasattr(p, key):
                raise KeyError(f"Unbekannter Parameter in config.yaml [{section}]: {key}")
            current = getattr(p, key)
            setattr(p, key, bool(value) if isinstance(current, bool) else type(current)(value))
    return p
