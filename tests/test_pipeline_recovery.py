"""The 2026-11 root causes: holiday stamps, silent empty pulls, stuck merges, write-once news.

Run: python3 tests/test_pipeline_recovery.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from refresh_daily_prices import last_trading_session, rescale_price_to_book

# The fundamentals script imports yfinance at module level; CI doesn't install it.
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))
import refresh_quarterly_fundamentals as quarterly

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


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


no_sleep = lambda _s: None  # noqa: E731

print("=== Prices are stamped with the session, not the run date ===")
history = pd.DataFrame({"Close": [1.0, 2.0, float("nan")]},
                       index=pd.to_datetime(["2026-11-06", "2026-11-09", "2026-11-10"]).tz_localize("Asia/Kolkata"))
check("a holiday keeps the previous session", last_trading_session(lambda: history, no_sleep) == "2026-11-09")
try:
    last_trading_session(lambda: history.iloc[0:0], no_sleep)
    check("unknown session fails loudly", False)
except RuntimeError:
    check("unknown session fails loudly", True)
check("P/B moves with price", rescale_price_to_book(2.0, 100, 110) == 2.2)
check("P/B survives a missing previous cap", rescale_price_to_book(2.0, float("nan"), 110) == 2.0)

print("=== An empty pull is a failure ===")
check("throttled empty answer has not landed", not quarterly.is_landed({"data_pull_date": "2026-11-22"}))
check("26% failed is broken", quarterly.pull_is_broken(26, 100))
check("25% failed is tolerated", not quarterly.pull_is_broken(25, 100))
check("nothing attempted is broken", quarterly.pull_is_broken(0, 0))


class FakeTicker:
    def __init__(self, info):
        self.info = info
        self.balance_sheet = pd.DataFrame()


def fetch(info):
    quarterly.yf.Ticker = lambda _s: FakeTicker(info)
    quarterly.fetch_roce = lambda _s: ("", "")
    return quarterly.fetch_fundamentals("X")


bank = fetch({"totalRevenue": 1e12, "ebitdaMargins": 0.0})
check("bank placeholder margin is written as null", pd.isna(bank["ebitda_margin_pct"]))
check("insider holding refreshes", fetch({"totalRevenue": 1.0, "heldPercentInsiders": 0.5})["insider_holding_pct"] == 50)
check("'Infinity' text is ignored", "trailing_pe" not in fetch({"totalRevenue": 1.0, "trailingPE": "Infinity"}))
check("empty info has not landed", not quarterly.is_landed(fetch({})))

print("=== News refresh merges ===")
news = load("export_news", "scripts/export_news_and_filings.py")
old = [{"headline": "Old", "link": "https://a/old", "date": "29-Jun-2026 20:53"}]
new = [{"headline": "New", "link": "https://a/new", "date": "2026-11-20"},
       {"headline": "Bad", "link": "javascript:alert(1)", "date": "2026-11-21"}]
merged = news.merge_items(new, old, 6)
check("newest first", [m["headline"] for m in merged] == ["New", "Old"], str(merged))
check("mixed dates are normalised", merged[1]["date"] == "2026-06-29")
check("empty fetch keeps what we have", len(news.merge_items([], old, 6)) == 1)
check("duplicates collapse", len(news.merge_items(old, old, 6)) == 1)
check("cap is honoured", len(news.merge_items(new, old, 1)) == 1)

print("=== News failures get their own issue ===")
ping = load("healthcheck_ping", ".github/scripts/healthcheck_ping.py")
check("news title is distinct", ping.ISSUE_TITLE.get("news") not in (None, ping.ISSUE_TITLE["daily"]))

print("=== Merge does not strand a PR on mergeable=UNKNOWN ===")
tmp = Path(tempfile.mkdtemp())
try:
    def git(*args, cwd):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              cwd=cwd, capture_output=True, text=True, check=True).stdout

    def scenario(mergeable):
        box = Path(tempfile.mkdtemp(dir=tmp))
        git("init", "-q", "--bare", "-b", "main", "remote.git", cwd=box)
        git("clone", "-q", str(box / "remote.git"), "work", cwd=box)
        work = box / "work"
        git("checkout", "-q", "-B", "main", cwd=work)
        (work / "data.txt").write_text("old\n")
        git("add", ".", cwd=work)
        git("commit", "-qm", "seed", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)
        git("checkout", "-q", "-b", "price-refresh/1", cwd=work)
        (work / "data.txt").write_text("new\n")
        git("commit", "-qam", "data", cwd=work)
        git("push", "-q", "origin", "price-refresh/1", cwd=work)
        git("checkout", "-q", "main", cwd=work)

        stubs = box / "bin"
        stubs.mkdir()
        (stubs / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (stubs / "gh").write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            f"  *mergeable*) echo {mergeable};;\n"
            '  *"pr view"*) echo \'{"number":1,"title":"Daily","headRefName":"price-refresh/1","state":"OPEN"}\';;\n'
            "esac\n")
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        p = subprocess.run(["bash", str(REPO_ROOT / ".github/scripts/merge_pr_retry.sh"), "1"], cwd=work,
                           env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"},
                           capture_output=True, text=True)
        return p.returncode, git("--git-dir", str(box / "remote.git"), "show", "main:data.txt", cwd=box).strip(), p.stderr

    rc, content, err = scenario("UNKNOWN")
    check("UNKNOWN still merges clean data", rc == 0 and content == "new", err[-300:])
    rc, content, err = scenario("CONFLICTING")
    check("CONFLICTING is refused", rc != 0 and content == "old")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{checks - failures}/{checks} checks passed")
if failures:
    sys.exit(1)
