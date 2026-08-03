#!/usr/bin/env python3
"""
Compare two HSAIDS statistics JSON files (e.g. v1 vs v2) and print a
side-by-side table showing absolute values and deltas.

Usage
-----
    python comparisons/compare_runs.py comparisons/results/hsaids_v1/hsaids_statistics_v1.json \
                                       comparisons/results/hsaids_v2/hsaids_statistics_v2.json

Optional flags
--------------
    --csv PATH   Also write the comparison table to a CSV file.
    --json PATH  Also write the comparison data to a JSON file.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Metrics to compare, in display order
# ---------------------------------------------------------------------------

_METRICS: List[Tuple[str, str, str]] = [
    # (key_in_json, display_name, format_hint)
    ("run_label",                        "Run label",                     "str"),
    ("files_indexed",                    "Files indexed",                 "int"),
    ("logical_input_bytes",              "Logical input bytes",           "bytes"),
    ("total_chunks_processed",           "Chunks processed",              "int"),
    ("cold_unique_chunks",               "Unique chunks",                 "int"),
    ("duplicate_chunk_references",       "Duplicate chunk refs",          "int"),
    ("physical_unique_chunk_bytes",      "Physical unique chunk bytes",   "bytes"),
    ("avg_chunk_size",                   "Avg chunk size (B)",            "float2"),
    ("min_chunk_size",                   "Min chunk size (B)",            "int"),
    ("max_chunk_size",                   "Max chunk size (B)",            "int"),
    ("dedup_ratio",                      "Dedup ratio",                   "float4"),
    ("hot_hits",                         "Hot-layer hits",                "int"),
    ("cold_hits",                        "Cold-layer hits",               "int"),
    ("misses",                           "Index misses",                  "int"),
    ("bayesian_confidence_hot_hit",      "Bayesian confidence (hot hit)", "pct"),
    ("bayes_risk_hot_first_ns",          "Bayes risk: hot-first (ns)",    "float0"),
    ("bayes_risk_cold_first_ns",         "Bayes risk: cold-first (ns)",   "float0"),
    ("micro_cost_hot_lookup_ns",         "Micro-cost hot lookup (ns)",    "float0"),
    ("micro_cost_cold_lookup_ns",        "Micro-cost cold lookup (ns)",   "float0"),
    ("micro_cost_verify_ns",             "Micro-cost verify (ns)",        "float0"),
    ("micro_cost_cold_write_ns",         "Micro-cost cold write (ns)",    "float0"),
    ("cold_index_logical_bytes_written", "Cold index logical writes",     "bytes"),
    ("cold_index_physical_bytes_written","Cold index physical writes",    "bytes"),
    ("cold_index_waf",                   "Cold index WAF (app-level)",    "float4"),
    ("container_physical_bytes_written", "Container bytes written",       "bytes"),
    ("gc_reclaimed_chunks",              "GC reclaimed chunks",           "int"),
    ("gc_reclaimed_bytes",               "GC reclaimed bytes",            "bytes"),
    ("cdc_min_size",                     "CDC min chunk size (B)",        "int"),
    ("cdc_avg_target_size",              "CDC avg target size (B)",       "int"),
    ("cdc_max_size",                     "CDC max chunk size (B)",        "int"),
]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def _fmt(value: Any, hint: str) -> str:
    if value is None:
        return "—"
    if hint == "str":
        return str(value)
    if hint == "int":
        return f"{int(value):,}"
    if hint == "bytes":
        return _fmt_bytes(float(value))
    if hint == "float0":
        return f"{float(value):.0f}"
    if hint == "float2":
        return f"{float(value):.2f}"
    if hint == "float4":
        return f"{float(value):.4f}"
    if hint == "pct":
        return f"{float(value) * 100:.2f}%"
    return str(value)


def _delta(a: Any, b: Any, hint: str) -> str:
    """Return a formatted delta (b - a) for numeric hints, else '' for strings."""
    if hint == "str" or a is None or b is None:
        return ""
    try:
        diff = float(b) - float(a)
    except (TypeError, ValueError):
        return ""
    if hint == "bytes":
        sign = "+" if diff >= 0 else ""
        return f"{sign}{_fmt_bytes(diff)}"
    if hint == "pct":
        sign = "+" if diff >= 0 else ""
        return f"{sign}{diff * 100:.2f}pp"
    sign = "+" if diff >= 0 else ""
    if hint == "int":
        return f"{sign}{int(diff):,}"
    if hint == "float0":
        return f"{sign}{diff:.0f}"
    if hint == "float2":
        return f"{sign}{diff:.2f}"
    if hint == "float4":
        return f"{sign}{diff:.4f}"
    return f"{sign}{diff}"


# ---------------------------------------------------------------------------
# Table builder
# ---------------------------------------------------------------------------

def build_table(
    a: Dict[str, Any],
    b: Dict[str, Any],
    label_a: str,
    label_b: str,
) -> List[Dict[str, str]]:
    rows = []
    for key, name, hint in _METRICS:
        val_a = a.get(key)
        val_b = b.get(key)
        if val_a is None and val_b is None:
            continue
        rows.append(
            {
                "metric": name,
                label_a: _fmt(val_a, hint),
                label_b: _fmt(val_b, hint),
                "delta (b-a)": _delta(val_a, val_b, hint),
            }
        )
    return rows


def _col_width(rows: List[Dict[str, str]], col: str) -> int:
    return max(len(col), *(len(row.get(col, "")) for row in rows))


def print_table(rows: List[Dict[str, str]], label_a: str, label_b: str) -> None:
    cols = ["metric", label_a, label_b, "delta (b-a)"]
    widths = {c: _col_width(rows, c) for c in cols}
    sep = "+-" + "-+-".join("-" * widths[c] for c in cols) + "-+"
    header = "| " + " | ".join(c.ljust(widths[c]) for c in cols) + " |"
    print(sep)
    print(header)
    print(sep)
    for row in rows:
        line = "| " + " | ".join(row.get(c, "").ljust(widths[c]) for c in cols) + " |"
        print(line)
    print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two HSAIDS statistics JSON files and print a diff table."
    )
    parser.add_argument("stats_a", help="First statistics JSON file (baseline, e.g. v1).")
    parser.add_argument("stats_b", help="Second statistics JSON file (comparison, e.g. v2).")
    parser.add_argument("--csv", metavar="PATH", help="Write comparison table to CSV.")
    parser.add_argument("--json", metavar="PATH", help="Write comparison data to JSON.")
    args = parser.parse_args()

    path_a = Path(args.stats_a)
    path_b = Path(args.stats_b)

    for p in (path_a, path_b):
        if not p.exists():
            sys.exit(f"File not found: {p}")

    with path_a.open() as fh:
        data_a: Dict[str, Any] = json.load(fh)
    with path_b.open() as fh:
        data_b: Dict[str, Any] = json.load(fh)

    label_a = str(data_a.get("run_label", path_a.stem))
    label_b = str(data_b.get("run_label", path_b.stem))

    rows = build_table(data_a, data_b, label_a, label_b)

    print(f"\nHSAIDS Run Comparison: {label_a}  vs  {label_b}\n")
    print_table(rows, label_a, label_b)

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCSV written to: {out}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as fh:
            json.dump(
                {
                    "label_a": label_a,
                    "label_b": label_b,
                    "rows": rows,
                    "raw_a": data_a,
                    "raw_b": data_b,
                },
                fh,
                indent=2,
            )
        print(f"JSON written to: {out}")


if __name__ == "__main__":
    main()
