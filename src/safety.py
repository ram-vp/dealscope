"""Trust boundaries: feed text, model output, URL parameters, spreadsheet cells.

Everything here treats Yahoo / NSE / BSE / Google News text, model output and
query parameters as attacker-controlled.
"""

from __future__ import annotations

import math
import re
import threading
import time
from urllib.parse import urlsplit

MAX_PROMPT_FIELD = 120
MAX_AI_TEXT = 2000

# A summary of what a company does never needs any of these. Screened on the
# way out of the model, and again when exported.
_UNSAFE_AI_TEXT = re.compile(
    "|".join([
        r"https?://",
        r"\bwww\.",
        r"<\s*/?\s*(script|img|iframe|svg|a|system|style|object)\b",
        r"\bjavascript:",
        r"\bon(error|load|click)\s*=",
        r"\]\(",  # markdown link
        r"\b(ignore|disregard|forget) (all |any )?(previous|prior|above|earlier) (instructions|prompts?)",
        r"\bsystem prompt\b",
        r"\bstrong (buy|sell)\b",
        r"\b(should|must|recommend(s|ed)? (to|that you)) (buy|sell|short|accumulate) ",
        r"\bguaranteed returns?\b",
        r"\bprice target (of|is)\b",
    ]),
    re.IGNORECASE,
)
_FORMULA_START = re.compile(r"^[=+\-@\t\r]")


def safe_link(value):
    """An http(s) URL, or None."""
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    return text if parts.scheme in ("http", "https") and parts.netloc else None


def prompt_field(value):
    """A feed value as one short line that can't open or close prompt structure."""
    text = re.sub(r"[<>`{}]", "", str(value if value is not None else ""))
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_PROMPT_FIELD]


def safe_ai_text(value):
    """Model output that is fit to publish, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > MAX_AI_TEXT or _UNSAFE_AI_TEXT.search(value):
        return None
    return value


def bounded_number(raw, default, low, high):
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(high, max(low, number))


def csv_safe_frame(frame):
    """Text cells that a spreadsheet would run as a formula get a leading quote."""
    def neutralise(value):
        return f"'{value}" if isinstance(value, str) and _FORMULA_START.match(value) else value

    out = frame.copy()
    for column in out.columns:
        if out[column].dtype == object:
            out[column] = out[column].map(neutralise)
    return out


class CallBudget:
    """At most `limit` calls per `seconds`, process-wide. Streamlit has no
    per-visitor identity, and the thing to protect is the shared API quota."""

    def __init__(self, limit, seconds, clock=time.monotonic):
        self._limit, self._seconds, self._clock = limit, seconds, clock
        self._calls = []
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            now = self._clock()
            self._calls = [t for t in self._calls if now - t < self._seconds]
            if len(self._calls) >= self._limit:
                return False
            self._calls.append(now)
            return True
