"""
tools/groundedness.py
---------------------
Cheap, deterministic check that every number in a Writer's text actually
appears in (or can be derived from) the underlying SQL result rows.
Used by the Orchestrator as an in-band hard-block step inside
`_write_grounded_answer` — if any numeric token in the Writer's prose
doesn't reconcile, the Writer is asked to rewrite (up to
MAX_GROUNDEDNESS_RETRIES = 2 attempts). On exhaustion the orchestrator
ships `GROUNDEDNESS_FAIL_TEXT` instead of the unverified text.

This is NOT an LLM. It's regex extraction + set membership.

Accepted "safe" derivations (each one a Writer compositional pattern
the prompt explicitly permits):
  1. Individual cell values from the result rows.
  2. Numbers embedded in string cell values — handles labels like
     "10 inch", "size 8", "under 25k" where the digit is part of an
     attribute_value name, not a count.
  3. Per-column grand totals.
  4. Each row's percentage of its column total.
  5. Per-group subtotals — sum of a numeric column for all rows that
     share a categorical value ("Necklace shows 41 requests").
  6. Per-group row counts ("4 distinct attribute values for Necklace").
  7. Top-N partial sums for N ∈ {2..5} of each numeric column sorted
     descending ("the top 3 attributes account for 36").
  8. Per-group subtotal as a percentage of the column total
     ("Bangles is 37% of X1's non-purchase volume" when 52 / 141 ≈ 37%).
  9. Per-group top-N partial sums ("within Bangles, the top 3 attributes
     account for 20 requests") for N ∈ {2..5} on the numeric column
     sorted descending WITHIN that group.
 10. Top-N partial sums OF the per-group subtotals (and their share of
     the column total) for N ∈ {2..5} — supports headlines like
     "Bangles and Finger Rings together drive 73% of requests"
     where 73 = (52 + 51) / 141 × 100.

A companion function `safe_derivations_summary` formats the above as
a Markdown section the Orchestrator INJECTS into the Writer's user
block. The Writer picks values from that list instead of computing
its own arithmetic — eliminates the failure mode where the Writer
synthesizes a number that happens to miss the candidate set.

Public exports (also re-exported from tools/__init__.py):
  groundedness_check         — verify a Writer text against rows
  safe_derivations_summary   — pre-compute the safe values list
  GroundednessResult         — dataclass(grounded, ungrounded, total_numbers)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Captures common numeric forms:
#   - "1,200" or "1,234.56"  (comma-separated)
#   - "47" or "0.5"          (plain int / float)
#   - "23%" or "23.5%"       (percent — trailing % is captured)
#
# The negative lookahead `(?!\w)` is critical: it prevents extraction
# from inside unit-suffixed labels that the Writer cites verbatim from
# attribute_value strings — "25k" (price band), "8g" (weight), "10inch"
# (size). Without this guard, "Necklace under 25k leads at 20" would
# try to verify both "25" and "20", and the 25 would always fail
# because the data carries the label as a string, not the number.
_NUMBER_RE = re.compile(
    r"""
    \b
    (?:
        \d{1,3}(?:,\d{3})+(?:\.\d+)?     # comma-separated like 1,200 / 1,234.56
        |
        \d+(?:\.\d+)?                     # plain integer / float
    )
    (?:\s*%)?                             # optional percent sign
    (?!\w)                                # NOT followed by a word char
    """,
    re.VERBOSE,
)


# Numeric tokens with absolute value below this are too common (years, IDs,
# the digits 0..9 appearing in unrelated places) to demand grounding for.
# Skipping them keeps the false-positive rate down.
_IGNORE_BELOW = 2.0


@dataclass
class GroundednessResult:
    grounded: bool
    ungrounded: list[str]
    total_numbers: int


def _normalize(token: str) -> tuple[float, bool] | None:
    """Strip commas / percent sign, return (value, is_percent) or None."""
    t = token.strip()
    is_pct = t.endswith("%")
    if is_pct:
        t = t[:-1].rstrip()
    t = t.replace(",", "")
    try:
        return float(t), is_pct
    except ValueError:
        return None


def _extract_numbers(text: str) -> list[tuple[float, bool, str]]:
    """Find all distinct numeric tokens in `text`."""
    seen_tokens: dict[str, tuple[float, bool, str]] = {}
    for m in _NUMBER_RE.finditer(text or ""):
        original = m.group(0).strip()
        norm = _normalize(original)
        if norm is None:
            continue
        val, is_pct = norm
        if abs(val) < _IGNORE_BELOW:
            continue
        # Dedupe by original token form so "47" cited three times is
        # checked once.
        seen_tokens.setdefault(original, (val, is_pct, original))
    return list(seen_tokens.values())


def _classify_columns(
    rows: list[dict],
    columns: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Split columns into (numeric_cols, categorical_cols) using the first
    non-null sample. Mixed-type columns are best-effort classified by their
    first usable value."""
    numeric_cols: list[str] = []
    categorical_cols: list[str] = []
    for c in (columns or []):
        sample = next((r.get(c) for r in rows if r.get(c) is not None), None)
        if isinstance(sample, bool):  # bool is a subclass of int — skip
            continue
        if isinstance(sample, (int, float)):
            numeric_cols.append(c)
        elif isinstance(sample, str):
            # Try to interpret as a numeric string anyway.
            try:
                float(sample)
                numeric_cols.append(c)
            except ValueError:
                categorical_cols.append(c)
    return numeric_cols, categorical_cols


def _candidate_values(
    rows: list[dict] | None,
    columns: list[str] | None,
) -> set[float]:
    """Build the set of values the Writer is allowed to cite.

    Accepted derivations (each one a known compositional pattern the
    Writer prompt explicitly permits):

      1. Individual cell values in the result.
      2. Per-column grand totals — "the dataset has N feedbacks".
      3. Each row's percentage of its column total — "X1 is 24%".
      4. Per-group subtotals (sum of a numeric column for all rows
         sharing a value of a categorical column) — "Necklace shows
         41 requests total across price bands".
      5. Per-group row counts — "Necklace has 4 attribute values
         requested".
      6. Top-N partial sums (N ∈ {2..5}) computed over each numeric
         column sorted descending — "the top 3 reasons account for
         36 of the feedbacks".

    Each pattern was added in response to a real Writer compositional
    style that produced false-positive groundedness rejections. Adding
    them broadens what the check accepts without meaningfully weakening
    its safety net — a fabricated number still has to coincide with one
    of these legitimate derivations to slip through, which is rare for
    counts in the tens-to-hundreds range typical of this dataset.
    """
    cands: set[float] = set()
    if not rows:
        return cands

    # 1. Every numeric cell value in the result.
    for r in rows:
        for v in r.values():
            try:
                cands.add(float(v))
            except (TypeError, ValueError):
                continue

    # 1b. Numbers embedded in STRING cell values — attribute_value labels
    # like "10 inch", "size 8", "under 25k", "below 1 lakh" carry digits
    # that the Writer legitimately cites when naming the label. Without
    # this rule, "Anklets - 10 inch (15 requests)" gets flagged because
    # "10" isn't a count anywhere; with it, the embedded numeric tokens
    # of every string cell are grounded.
    _label_num_re = re.compile(r"\d+(?:\.\d+)?")
    for r in rows:
        for v in r.values():
            if isinstance(v, str):
                for m in _label_num_re.finditer(v):
                    try:
                        cands.add(float(m.group(0)))
                    except ValueError:
                        continue

    if not columns:
        return cands

    numeric_cols, categorical_cols = _classify_columns(rows, columns)

    # 2 & 3. Column grand totals + per-row percentages.
    for c in numeric_cols:
        col_vals: list[float] = []
        for r in rows:
            v = r.get(c)
            try:
                col_vals.append(float(v))
            except (TypeError, ValueError):
                continue
        if not col_vals:
            continue

        total = sum(col_vals)
        cands.add(total)

        if total > 0:
            for v in col_vals:
                pct = (v / total) * 100
                cands.add(pct)
                cands.add(round(pct))
                cands.add(round(pct, 1))

        # 6. Top-N partial sums (sorted descending) — common in rankings.
        sorted_desc = sorted(col_vals, reverse=True)
        running = 0.0
        for n, v in enumerate(sorted_desc, start=1):
            running += v
            if 2 <= n <= 5:
                cands.add(running)
                cands.add(round(running))

    # Pre-compute per-numeric-column totals once — reused by both the
    # subtotal-as-percent derivation (pattern 8) and the per-group top-N
    # partial sums (pattern 9). Cheaper than re-summing per group.
    col_totals: dict[str, float] = {}
    for c in numeric_cols:
        s = 0.0
        for r in rows:
            try:
                s += float(r.get(c))
            except (TypeError, ValueError):
                continue
        col_totals[c] = s

    # 4 & 5 & 8 & 9 & 10. Per-group subtotals, row counts,
    # subtotal-as-percent, per-group top-N partial sums, AND top-N
    # partial sums of the per-group subtotals themselves.
    # For each (categorical, numeric) pair, sum the numeric column within
    # each distinct value of the categorical column. Also add: the row
    # count per group (the Writer might say "Necklace has 4 attribute
    # values requested"), the subtotal expressed as % of grand total
    # (so "Bangles 37%" verifies when 52/141 ≈ 37%), the top-N partial
    # sums computed WITHIN that group (so "the top 3 Bangles attributes
    # account for 20 requests" verifies), and the top-N partial sums OF
    # the group subtotals themselves (so "the top 2 products drive 73%
    # of all requests" verifies when 52+51=103 and 103/141≈73%).
    for cat_c in categorical_cols:
        # Row counts per category — independent of any numeric column.
        group_sizes: dict = {}
        for r in rows:
            key = r.get(cat_c)
            if key is None:
                continue
            group_sizes[key] = group_sizes.get(key, 0) + 1
        for sz in group_sizes.values():
            cands.add(float(sz))

        # Subtotals per (category, numeric column).
        for num_c in numeric_cols:
            groups: dict = {}
            for r in rows:
                key = r.get(cat_c)
                if key is None:
                    continue
                v = r.get(num_c)
                try:
                    groups.setdefault(key, []).append(float(v))
                except (TypeError, ValueError):
                    continue

            col_total = col_totals.get(num_c, 0.0)

            subtotals: list[float] = []
            for vals in groups.values():
                if not vals:
                    continue
                subtotal = sum(vals)
                subtotals.append(subtotal)
                cands.add(subtotal)

                # 8. Subtotal as a percentage of the grand total.
                if col_total > 0:
                    pct = (subtotal / col_total) * 100
                    cands.add(pct)
                    cands.add(round(pct))
                    cands.add(round(pct, 1))

                # 9. Per-group top-N partial sums (sorted desc within group).
                sorted_group = sorted(vals, reverse=True)
                running = 0.0
                for n, v in enumerate(sorted_group, start=1):
                    running += v
                    if 2 <= n <= 5:
                        cands.add(running)
                        cands.add(round(running))

            # 10. Top-N partial sums OF the per-group subtotals, and the
            # corresponding share of the column total. Enables headlines
            # like "Bangles and Finger Rings together drive 73% of all
            # requests" (top-2 product subtotals = 103; 103/141 ≈ 73%).
            sorted_subtotals = sorted(subtotals, reverse=True)
            running = 0.0
            for n, v in enumerate(sorted_subtotals, start=1):
                running += v
                if 2 <= n <= 5:
                    cands.add(running)
                    cands.add(round(running))
                    if col_total > 0:
                        share = (running / col_total) * 100
                        cands.add(share)
                        cands.add(round(share))
                        cands.add(round(share, 1))

    return cands


def _fmt(v: float) -> str:
    """Render a numeric candidate compactly — integers as ints, floats
    rounded to one decimal place."""
    return f"{int(v)}" if v == int(v) else f"{v:.1f}"


def safe_derivations_summary(
    rows: list[dict] | None,
    columns: list[str] | None,
    *,
    max_groups_per_column: int = 8,
) -> str:
    """Return a Writer-facing summary of pre-computed safe values to cite.

    Builds the same derivation set as `_candidate_values` but formats it
    as a readable list the Orchestrator injects into the Writer's
    user_block. The point is to take arithmetic OFF the Writer — instead
    of the Writer computing group sums and partial sums (which is where
    most groundedness failures originate), it picks from this list.

    Returns an empty string when there are no rows, no columns, or no
    numeric columns to derive against. The caller should treat an empty
    return as "no safe-values section to inject."
    """
    if not rows or not columns:
        return ""

    numeric_cols, categorical_cols = _classify_columns(rows, columns)
    if not numeric_cols:
        return ""

    lines: list[str] = []

    for nc in numeric_cols:
        col_vals: list[float] = []
        for r in rows:
            v = r.get(nc)
            try:
                col_vals.append(float(v))
            except (TypeError, ValueError):
                continue
        if not col_vals:
            continue

        # Grand total
        total = sum(col_vals)
        lines.append(f"  - Grand total of `{nc}` across all rows: **{_fmt(total)}**")

        # Top-N partial sums (sorted descending)
        sorted_desc = sorted(col_vals, reverse=True)
        running = 0.0
        for n, v in enumerate(sorted_desc, start=1):
            running += v
            if 2 <= n <= 5:
                lines.append(
                    f"  - Top-{n} partial sum of `{nc}` (sorted desc): "
                    f"**{_fmt(running)}**"
                )

        # Per-group subtotals + row counts + share-of-total + within-group
        # top-N partial sums, sorted by subtotal desc.
        for cat in categorical_cols:
            groups: dict = {}
            for r in rows:
                key = r.get(cat)
                if key is None:
                    continue
                v = r.get(nc)
                try:
                    groups.setdefault(key, []).append(float(v))
                except (TypeError, ValueError):
                    continue
            sorted_groups = sorted(
                groups.items(),
                key=lambda kv: sum(kv[1]),
                reverse=True,
            )
            group_subtotals_for_top: list[float] = []
            for key, vals in sorted_groups[:max_groups_per_column]:
                subtotal = sum(vals)
                group_subtotals_for_top.append(subtotal)
                line = (
                    f"  - `{cat}`={key!r}: subtotal of `{nc}` = "
                    f"**{_fmt(subtotal)}** across **{len(vals)}** row(s)"
                )
                # Share of grand total — surfaced so the Writer can cite
                # "Bangles is 37% of X1's non-purchase volume" verbatim
                # from this list rather than computing it.
                if total > 0:
                    pct = (subtotal / total) * 100
                    line += f" — **{pct:.1f}%** of grand total"
                lines.append(line)

                # Within-group top-N partial sums (only when the group
                # has more than one row — otherwise the running sum is
                # just the subtotal already shown).
                if len(vals) >= 2:
                    sorted_group = sorted(vals, reverse=True)
                    running = 0.0
                    partials: list[str] = []
                    for n, v in enumerate(sorted_group, start=1):
                        running += v
                        if 2 <= n <= min(5, len(sorted_group)):
                            partials.append(f"top-{n}={_fmt(running)}")
                    if partials:
                        lines.append(
                            f"    · within-group partial sums "
                            f"(`{cat}`={key!r}, sorted desc on `{nc}`): "
                            f"{', '.join(partials)}"
                        )

            # Top-N partial sums OF the group subtotals, plus the
            # corresponding share-of-grand-total — the planning-template
            # headline citation source ("top 2 products drive 73% of all
            # requests").
            if len(group_subtotals_for_top) >= 2:
                sorted_subs = sorted(group_subtotals_for_top, reverse=True)
                running = 0.0
                partials: list[str] = []
                for n, v in enumerate(sorted_subs, start=1):
                    running += v
                    if 2 <= n <= min(5, len(sorted_subs)):
                        share_str = ""
                        if total > 0:
                            share = (running / total) * 100
                            share_str = f" ({share:.1f}%)"
                        partials.append(f"top-{n}={_fmt(running)}{share_str}")
                if partials:
                    lines.append(
                        f"  - Top-N partial sums of `{cat}` subtotals "
                        f"on `{nc}` (and share of grand total): "
                        f"{', '.join(partials)}"
                    )

    if not lines:
        return ""

    return (
        "## Safe values for grounding (pre-computed — pick from these)\n"
        "Every number you cite MUST come from this list, the row table "
        "above, or a percentage of `Grand total`. Do not invent values.\n\n"
        + "\n".join(lines)
    )


def groundedness_check(
    text: str,
    rows: list[dict] | None,
    columns: list[str] | None,
    *,
    tolerance: float = 0.5,
) -> GroundednessResult:
    """Verify every number in `text` appears in the candidate set."""
    text_numbers = _extract_numbers(text)
    candidates = _candidate_values(rows, columns)

    ungrounded: list[str] = []
    for val, _is_pct, original in text_numbers:
        # `tolerance` of 0.5 handles common rounding (e.g., 23.5% ≈ 24%).
        if not any(abs(val - c) <= tolerance for c in candidates):
            ungrounded.append(original)

    return GroundednessResult(
        grounded=(len(ungrounded) == 0),
        ungrounded=ungrounded,
        total_numbers=len(text_numbers),
    )
