"""Last line of defence: refuse to write frontend JSON that is wrong.

sanitize.py and derive.py repair; this only judges what is about to be
written. Any problem raises, so the job goes red, the tracking issue opens and
the site keeps its last good data.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, timedelta

ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FACTORS = ("factor_revenue_growth", "factor_ebitda_margin", "factor_roce", "factor_debt_level")
RANGES = {
    "revenue": (0, None),
    "total_debt": (0, None),
    "market_cap": (0, None),
    "trailing_pe": (0, None),
    "ev_ebitda_low": (0, None),
    "pe_implied_low": (0, None),
    "promoter_holding_pct": (0, 100),
    "promoter_pledge_pct": (0, 100),
    "ebitda_margin_pct": (-300, 300),
    "current_ratio": (0, 100),
    "beta": (-10, 10),
    **{f: (0, 100) for f in FACTORS},
}


class ExportValidationError(RuntimeError):
    pass


def _bad_number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return True
    return (low is not None and value < low) or (high is not None and value > high)


def company_problems(rows, today):
    problems = []
    for ticker, n in Counter(r.get("ticker") for r in rows).items():
        if n > 1 or not ticker:
            problems.append(f"ticker {ticker!r} is blank or duplicated")

    latest = (today + timedelta(days=1)).isoformat()
    for r in rows:
        t = r.get("ticker")
        if not r.get("name"):
            problems.append(f"{t}: blank name")
        for key, (low, high) in RANGES.items():
            value = r.get(key)
            if value is not None and _bad_number(value, low, high):
                problems.append(f"{t}: {key}={value!r} is impossible")
        if r.get("ebitda_margin_pct") == 0 and r.get("ebitda") is None:
            problems.append(f"{t}: 0% margin with no EBITDA is a feed placeholder")
        income = r.get("net_income")
        if income is not None and income <= 0 and r.get("trailing_pe") is not None:
            problems.append(f"{t}: P/E on non-positive earnings")
        for low_key, high_key in (("ev_ebitda_low", "ev_ebitda_high"), ("pe_implied_low", "pe_implied_high")):
            low, high = r.get(low_key), r.get(high_key)
            if (low is None) != (high is None) or (low is not None and low > high):
                problems.append(f"{t}: {low_key}/{high_key} is not a range")
        if r.get("sector") == "Unclassified" and any(r.get(f) is not None for f in FACTORS):
            problems.append(f"{t}: Unclassified must not be scored")
        if r.get("sector") == "Financial Services" and r.get("factor_debt_level") is not None:
            problems.append(f"{t}: Financial Services must not have a leverage factor")
        for key in ("as_of_date", "market_cap_as_of"):
            day = r.get(key)
            if day is not None and (not ISO_DAY.match(str(day)) or str(day) > latest):
                problems.append(f"{t}: {key}={day!r} is not a past ISO date")
        if not r.get("as_of_date"):
            problems.append(f"{t}: no as_of_date")
    return problems


def deal_problems(deals, sectors, today):
    problems = []
    for i, d in enumerate(deals):
        year, stake, value = d.get("report_year"), d.get("stake_pct"), d.get("deal_value_usdm")
        if not d.get("target"):
            problems.append(f"deal {i}: blank target")
        if year is not None and not 1990 <= year <= today.year + 1:
            problems.append(f"deal {i}: report_year={year}")
        if stake is not None and _bad_number(stake, 0, 100):
            problems.append(f"deal {i}: stake_pct={stake}")
        if value is not None and _bad_number(value, 0, None):
            problems.append(f"deal {i}: deal_value_usdm={value}")
        if d.get("sector_v2") not in sectors:
            problems.append(f"deal {i}: sector {d.get('sector_v2')!r} matches no company sector")
    return problems


def validate_export(companies, deals, today=None):
    today = today or date.today()
    problems = []
    if not companies:
        problems.append("no companies")
    problems += company_problems(companies, today)
    problems += deal_problems(deals, {r.get("sector") for r in companies}, today)
    if problems:
        shown = "\n".join(f"  - {p}" for p in problems[:25])
        raise ExportValidationError(f"{len(problems)} problem(s); nothing was written:\n{shown}")
