"""Derived fields, the export validator, and the backend-to-frontend numbers.

Run: python3 tests/test_derive_and_export.py
"""

import copy
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import compute_filter_bands
import compute_sector_bands
import export_for_frontend
from src.data.derive import derive_trailing_pe, drop_placeholder_margins, reconcile_margin
from src.data.export_checks import ExportValidationError, validate_export
from src.data.loaders import load_companies
from src.data.publish_gate import evaluate_gate
from src.data.sanitize import sanitize
from src.logic.scoring import score_companies
from src.logic.valuation import valuation_range

failures = 0
checks = 0


def check(name, cond, detail=""):
    global failures, checks
    checks += 1
    if not cond:
        failures += 1
        print(f"  FAIL  {name}{('  -- ' + detail) if detail else ''}")
    else:
        print(f"  ok    {name}")


def near(a, b):
    if a is None or pd.isna(a):
        return b is None or pd.isna(b)
    return b is not None and not pd.isna(b) and abs(a - b) <= 1e-6 * max(1.0, abs(a))


print("=== Placeholder margins ===")
out = drop_placeholder_margins(pd.DataFrame({"ebitda": [np.nan, 0.0, 5e7, np.nan], "ebitda_margin_pct": [0.0, 0.0, 12.0, 18.9]}))
m = out.ebitda_margin_pct
check("0% with no EBITDA is null", pd.isna(m[0]))
check("a real zero EBITDA keeps 0%", m[1] == 0)
check("a real margin is untouched", m[2] == 12)
check("a margin without INR EBITDA (USD reporters) is untouched", m[3] == 18.9)

print("=== Margin reconciliation ===")
out = reconcile_margin(pd.DataFrame({"ebitda": [-36.0, 20.0, -400.0], "revenue": [15.0, 100.0, 100.0], "ebitda_margin_pct": [-50.0, 18.0, -100.0]}))
m = out.ebitda_margin_pct
check("within tolerance is untouched", m[1] == 18)
check("contradiction uses EBITDA / revenue", near(-240.0, m[0]), str(m[0]))
check("an impossible result is null", pd.isna(m[2]))

print("=== P/E ===")
out = derive_trailing_pe(pd.DataFrame({
    "name": ["Profit", "Loss", "Tiny", "USD", "Twin", "Twin"],
    "revenue": [1, 2, 3, 4, 5, 5],
    "market_cap": [2e10, 2e10, 2e10, 2e10, 1e10, 2e9],
    "net_income": [1e9, -1e9, 5e5, np.nan, 1e9, 1e9],
    "trailing_pe": [15.0, 9.0, 800.0, 13.3, 7.0, 4.0],
}))
pe = out.trailing_pe
check("follows today's market cap", pe[0] == 20)
check("loss-maker has none", pd.isna(pe[1]))
check("near-zero earnings have none", pd.isna(pe[2]))
check("no INR income keeps the feed's", pe[3] == 13.3)
check("share classes keep the feed's", pe[4] == 7 and pe[5] == 4)

print("=== The gate accepts dropping placeholders ===")
n = 100
live = pd.DataFrame({"symbol": [f"S{i}" for i in range(n)], "ebitda": [np.nan] * 8 + [1e8] * 92,
                     "revenue": [1e9] * n, "ebitda_margin_pct": [0.0] * 8 + [10.0] * 92, "as_of_date": ["2026-10-01"] * n})
res = evaluate_gate(drop_placeholder_margins(live), live)
check("not a coverage collapse", not any(f.startswith("coverage") for f in res.failures), str(res.failures))
res = evaluate_gate(live, live)
check("a surviving placeholder is critical", any(f.startswith("critical_flags") for f in res.failures), str(res.failures))

print("=== Export validator fails loudly ===")
GOOD = {"ticker": "A", "name": "A Ltd", "sector": "Chemicals", "as_of_date": "2026-10-01", "net_income": 1e9,
        "trailing_pe": 20.0, "ebitda": 1e8, "ebitda_margin_pct": 10.0}
DEAL = {"target": "T", "sector_v2": "Chemicals", "report_year": 2024}
TODAY = date(2026, 11, 22)


def rejected(company=None, deal=None, rows=None):
    rows = rows or [{**copy.deepcopy(GOOD), **(company or {})}]
    try:
        validate_export(rows, [{**DEAL, **(deal or {})}], TODAY)
    except ExportValidationError:
        return True
    return False


check("good data passes", not rejected())
for name, change in {
    "blank name": {"name": None},
    "placeholder margin": {"ebitda": None, "ebitda_margin_pct": 0},
    "P/E on a loss": {"net_income": -1},
    "negative revenue": {"revenue": -1},
    "percentile over 100": {"factor_roce": 101},
    "NaN": {"market_cap": float("nan")},
    "inverted range": {"ev_ebitda_low": 5, "ev_ebitda_high": 2},
    "scored Unclassified": {"sector": "Unclassified", "factor_roce": 50},
    "bank leverage factor": {"sector": "Financial Services", "factor_debt_level": 50},
    "future date": {"as_of_date": "2027-03-01"},
}.items():
    check(f"rejects {name}", rejected(company=change))
check("rejects duplicate tickers", rejected(rows=[GOOD, GOOD]))
check("rejects an unknown deal sector", rejected(deal={"sector_v2": "Nowhere"}))
check("rejects a stake over 100%", rejected(deal={"stake_pct": 120}))

print("=== A real export reproduces the scoring engine, company by company ===")
tmp = Path(tempfile.mkdtemp())
saved = (export_for_frontend.OUT_DIR, compute_filter_bands.OUT_PATH, compute_sector_bands.OUT_PATH)
try:
    export_for_frontend.OUT_DIR = tmp
    compute_filter_bands.OUT_PATH = tmp / "filter-bands.json"
    compute_sector_bands.OUT_PATH = tmp / "sector-bands.json"
    export_for_frontend.main()
finally:
    export_for_frontend.OUT_DIR, compute_filter_bands.OUT_PATH, compute_sector_bands.OUT_PATH = saved

rows = {r["ticker"]: r for r in json.loads((tmp / "companies.json").read_text())}
clean, _ = sanitize(load_companies())
engine = valuation_range(score_companies(clean, {}, "sector_v2"), "sector_v2")
FIELDS = {
    "revenue": "revenue", "ebitda": "ebitda", "ebitda_margin_pct": "ebitda_margin_pct",
    "revenue_growth_pct": "revenue_growth_pct", "roce_pct": "return_on_capital_employed_pct",
    "total_debt": "total_debt", "market_cap": "market_cap", "net_income": "net_income", "trailing_pe": "trailing_pe",
    "factor_revenue_growth": "pctl_revenue_growth_pct", "factor_ebitda_margin": "pctl_ebitda_margin_pct",
    "factor_roce": "pctl_return_on_capital_employed_pct", "factor_debt_level": "pctl_leverage_ratio",
    "ev_ebitda_low": "ev_ebitda_low", "ev_ebitda_high": "ev_ebitda_high",
    "pe_implied_low": "pe_implied_low", "pe_implied_high": "pe_implied_high",
}
check("same universe", len(rows) == len(engine))
for out_key, col in FIELDS.items():
    bad = [r["symbol"] for _, r in engine.iterrows() if not near(r[col], rows[r["symbol"]][out_key])]
    check(f"{out_key} matches for every company", not bad, str(bad[:5]))

filter_bands = json.loads((tmp / "filter-bands.json").read_text())["fields"]
check("bands count only real margins", filter_bands["ebitda_margin_pct"]["sample_size"] == int(clean.ebitda_margin_pct.notna().sum()))
check("bands use the published P/E", filter_bands["trailing_pe"]["sample_size"] == int(clean.trailing_pe.notna().sum()))

print(f"\n{checks - failures}/{checks} checks passed")
if failures:
    sys.exit(1)
