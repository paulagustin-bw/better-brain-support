#!/usr/bin/env python3
"""Summarise what the BetterBrain bot has spent, and what it produced for it.

Cost alone is not ROI. This pairs spend from the bot's usage log with two
outcome signals already recorded in the corpus:

  * gap-log entries  -- questions the bot actually answered
  * promoted PKRs    -- answers that became durable knowledge

so the interesting numbers are cost-per-answer and cost-per-PKR, not just a
monthly total. A month where spend rose and PKRs rose is a different story from
one where spend rose alone.

Usage:
    python3 scripts/usage_report.py                 # all time
    python3 scripts/usage_report.py --since 2026-07-01
    python3 scripts/usage_report.py --by-model      # compare a model switch
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

USAGE_LOG = Path(
    os.getenv("BETTERBRAIN_USAGE_LOG")
    or Path.home() / "Library" / "Logs" / "BetterBrainSupport" / "usage.jsonl"
)
CORPUS = Path(os.getenv("BETTERBRAIN_REPO") or Path.home() / "dev" / "betterbrain")
GAP_LOG = CORPUS / "knowledge-corpus" / "generated" / "betterbrain-ask-gaps.jsonl"


def load_rows(since: str | None) -> list[dict]:
    if not USAGE_LOG.exists():
        return []
    rows = []
    for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and str(row.get("ts", "")) < since:
            continue
        rows.append(row)
    return rows


def gap_outcomes(since: str | None) -> tuple[int, int]:
    """(questions logged, questions that became or resolved to a PKR)."""
    if not GAP_LOG.exists():
        return (0, 0)
    answered = promoted = 0
    for line in GAP_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(entry.get("question", "")).startswith("["):
            continue  # meta/sweep entries, not real questions
        if since and str(entry.get("date", "")) < since[:10]:
            continue
        answered += 1
        if entry.get("promoted_to_pkr") or entry.get("resolved_by_pkr"):
            promoted += 1
    return (answered, promoted)


def money(value: float) -> str:
    return f"${value:,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="ISO date, e.g. 2026-07-01")
    parser.add_argument("--by-model", action="store_true", help="break down by model")
    args = parser.parse_args()

    rows = load_rows(args.since)
    if not rows:
        print(f"No usage rows in {USAGE_LOG}.")
        print("Rows are written by _record_usage() in src/slack/handler.py on each")
        print("`claude -p` call, so this stays empty until the bot next runs.")
        return 0

    window = f" since {args.since}" if args.since else ""
    total = sum(r.get("cost_usd") or 0 for r in rows)
    print(f"# BetterBrain bot usage{window}\n")
    print(f"calls: {len(rows)}    total: {money(total)}\n")

    key = (lambda r: (r.get("label"), r.get("model"))) if args.by_model else (lambda r: (r.get("label"),))
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)

    head = f"{'label':<20}{'model':<12}" if args.by_model else f"{'label':<20}"
    print(head + f"{'calls':>7}{'total':>11}{'avg':>9}{'avg turns':>11}{'avg sec':>9}")
    for bucket_key in sorted(buckets, key=lambda k: -sum(r.get("cost_usd") or 0 for r in buckets[k])):
        group = buckets[bucket_key]
        cost = sum(r.get("cost_usd") or 0 for r in group)
        turns = [r.get("turns") or 0 for r in group]
        secs = [(r.get("duration_ms") or 0) / 1000 for r in group]
        label = bucket_key[0] or "?"
        prefix = f"{label:<20}{(bucket_key[1] or 'default'):<12}" if args.by_model else f"{label:<20}"
        print(
            prefix
            + f"{len(group):>7}{money(cost):>11}{money(cost/len(group)):>9}"
            + f"{sum(turns)/len(group):>11.1f}{sum(secs)/len(secs):>9.1f}"
        )

    errors = sum(1 for r in rows if r.get("is_error"))
    if errors:
        print(f"\nerrored calls: {errors} ({errors/len(rows)*100:.0f}%) -- still billed")

    answered, promoted = gap_outcomes(args.since)
    print("\n## Return side\n")
    if not answered:
        print("  no gap-log entries in this window")
        return 0
    print(f"  questions answered and logged : {answered}")
    print(f"  of those, tied to a PKR       : {promoted}")
    print(f"  cost per question answered    : {money(total/answered)}")
    if promoted:
        print(f"  cost per PKR-tied answer      : {money(total/promoted)}")
    print(
        "\n  Caveat: spend covers every `claude -p` call, including declines and"
        "\n  retries, while the gap log counts only questions that reached an answer."
        "\n  Cost per question is therefore an upper bound, which is the honest"
        "\n  direction for a budget conversation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
