"""Make fields agree with the inputs they are defined by.

Applied by load_companies(), so the export, the band files and the Streamlit
app all see the same numbers. Nothing here invents a value: a field is either
recomputed from stored inputs of the same row, or nulled.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.quality_checks import (
    EBITDA_MARGIN_ABS_LIMIT,
    MARGIN_RECOMPUTE_TOLERANCE,
    NET_INCOME_MATERIALITY_FLOOR,
    placeholder_margin_mask,
)


def drop_placeholder_margins(df):
    """Yahoo sends ebitdaMargins=0.0 when it has no EBITDA (every bank, most
    NBFCs). That is "not applicable". Read as a real zero it was shown as
    "0.0%", ranked as a percentile and averaged into the composite score."""
    out = df.copy()
    mask = placeholder_margin_mask(out)
    if mask.any():
        out.loc[mask, "ebitda_margin_pct"] = np.nan
    return out


def reconcile_margin(df):
    """The site defines margin as EBITDA / revenue. Where the feed's own margin
    contradicts its EBITDA and revenue, use the definition; if that lands
    outside the possible range, null it."""
    out = df.copy()
    if not {"ebitda", "revenue", "ebitda_margin_pct"} <= set(out.columns):
        return out
    ebitda, revenue, stored = (
        pd.to_numeric(out[c], errors="coerce") for c in ("ebitda", "revenue", "ebitda_margin_pct")
    )
    derived = ebitda / revenue.where(revenue > 0) * 100
    wrong = (derived - stored).abs() > MARGIN_RECOMPUTE_TOLERANCE
    if wrong.any():
        fixed = derived.where(derived.abs() <= EBITDA_MARGIN_ABS_LIMIT)
        out.loc[wrong, "ebitda_margin_pct"] = fixed[wrong]
    return out


def derive_trailing_pe(df):
    """P/E = market cap / trailing net income, as in valuation.py.

    The feed's ratio was frozen at the quarterly pull while market cap moved
    daily, and it printed multiples for loss-makers. Two exceptions keep the
    feed's ratio: no INR net income (USD reporters), and companies with more
    than one listed share class, where one class's cap over the whole
    company's income is not a P/E.
    """
    out = df.copy()
    needed = {"market_cap", "net_income", "trailing_pe"}
    if not needed <= set(out.columns):
        return out
    cap, income = (pd.to_numeric(out[c], errors="coerce") for c in ("market_cap", "net_income"))
    multi_class = pd.Series(False, index=out.index)
    if {"name", "revenue"} <= set(out.columns):
        multi_class = income.notna() & out.duplicated(["name", "revenue", "net_income"], keep=False)

    pe = pd.to_numeric(out["trailing_pe"], errors="coerce").astype("float64")
    computable = income.ge(NET_INCOME_MATERIALITY_FLOOR) & cap.gt(0) & ~multi_class
    pe = pe.mask(computable, cap / income)
    pe = pe.mask(income.notna() & income.lt(NET_INCOME_MATERIALITY_FLOOR), np.nan)
    out["trailing_pe"] = pe
    return out


def derive_fields(df):
    return derive_trailing_pe(reconcile_margin(drop_placeholder_margins(df)))
