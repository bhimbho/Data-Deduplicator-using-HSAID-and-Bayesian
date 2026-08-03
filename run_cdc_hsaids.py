#!/usr/bin/env python3
"""Run the chunk-level HSAIDS/CDC deduplication experiment."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List

from cdc_hsaids import CDCConfig, ingest_directory


def _write_csv(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_chunk_tables(store_dir: Path, output_dir: Path) -> None:
    db_path = store_dir / "cold_index.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        duplicate_chunks = [
            {
                "chunk_hash": row[0],
                "chunk_size": row[1],
                "refcount": row[2],
                "duplicate_references": row[2] - 1,
                "container_id": row[3],
                "container_offset": row[4],
            }
            for row in conn.execute(
                """
                SELECT hash, size, refcount, container_id, container_offset
                FROM chunks
                WHERE refcount > 1
                ORDER BY refcount DESC, size DESC
                """
            )
        ]
        _write_csv(output_dir / "duplicate_chunks.csv", duplicate_chunks)

        recipe_rows = [
            {
                "file_id": row[0],
                "file_path": row[1],
                "ordinal": row[2],
                "chunk_hash": row[3],
                "file_offset": row[4],
                "chunk_size": row[5],
                "container_id": row[6],
                "container_offset": row[7],
            }
            for row in conn.execute(
                """
                SELECT f.file_id, f.path, fc.ordinal, fc.chunk_hash, fc.file_offset,
                       fc.size, fc.container_id, fc.container_offset
                FROM files f
                JOIN file_chunks fc ON fc.file_id = f.file_id
                ORDER BY f.file_id, fc.ordinal
                """
            )
        ]
        _write_csv(output_dir / "file_recipes.csv", recipe_rows)

        chunk_rows = [
            {
                "chunk_hash": row[0],
                "chunk_size": row[1],
                "refcount": row[2],
                "frequency": row[3],
                "container_id": row[4],
                "container_offset": row[5],
            }
            for row in conn.execute(
                """
                SELECT hash, size, refcount, frequency, container_id, container_offset
                FROM chunks
                ORDER BY hash
                """
            )
        ]
        _write_csv(output_dir / "unique_chunks.csv", chunk_rows)
    finally:
        conn.close()


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def main() -> None:
    parser = argparse.ArgumentParser(description="Chunk-level HSAIDS experiment with CDC")
    parser.add_argument("input_dir", help="Directory to scan")
    parser.add_argument("--store-dir", default=".hsaids_cdc_store", help="Disk-backed cold-layer/container directory")
    parser.add_argument("--output-dir", default="cdc_results", help="Directory for CSV/JSON results")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden files and directories")
    parser.add_argument("--reset-store", action="store_true", help="Delete the store/output directories before running")
    parser.add_argument("--min-chunk-size", type=int, default=2048)
    parser.add_argument("--avg-chunk-size", type=int, default=8192)
    parser.add_argument("--max-chunk-size", type=int, default=65536)
    parser.add_argument("--hot-capacity", type=int, default=50000)
    parser.add_argument(
        "--hot-bloom-size",
        type=int,
        default=200_003,
        help="Hot-layer Bloom filter size in bits (default: 200003). "
             "For Wikipedia scale use 2000003 or larger.",
    )
    parser.add_argument(
        "--hot-cms-width",
        type=int,
        default=100_003,
        help="Hot-layer Count-Min Sketch width (default: 100003). "
             "For Wikipedia scale use 1000003 or larger.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=2000,
        help="Commit SQLite writes every N operations (default: 2000). "
             "Lower = more durable but slower; higher = faster but more WAL growth.",
    )
    parser.add_argument(
        "--run-label",
        default=None,
        help="Optional label (e.g. v1, v2) written into the statistics JSON and "
             "used to name the output file as hsaids_statistics_<label>.json.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    store_dir = Path(args.store_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if args.reset_store:
        shutil.rmtree(store_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)

    output_dir.mkdir(parents=True, exist_ok=True)

    config = CDCConfig(
        min_size=args.min_chunk_size,
        avg_size=args.avg_chunk_size,
        max_size=args.max_chunk_size,
    )

    file_summaries, stats = ingest_directory(
        input_dir=input_dir,
        store_dir=store_dir,
        include_hidden=args.include_hidden,
        cdc_config=config,
        hot_capacity=args.hot_capacity,
        hot_bloom_size=args.hot_bloom_size,
        hot_cms_width=args.hot_cms_width,
        commit_every=args.commit_every,
    )

    if args.run_label:
        stats["run_label"] = args.run_label

    label_suffix = f"_{args.run_label}" if args.run_label else ""

    _write_csv(output_dir / "file_summary.csv", file_summaries)
    _write_csv(output_dir / f"hsaids_statistics{label_suffix}.csv", [stats])
    export_chunk_tables(store_dir, output_dir)

    stats_json_path = output_dir / f"hsaids_statistics{label_suffix}.json"
    with stats_json_path.open("w") as handle:
        json.dump(stats, handle, indent=2, sort_keys=True)

    label_tag = f" [{args.run_label}]" if args.run_label else ""
    print(f"Chunk-level HSAIDS run complete{label_tag}")
    print(f"  Files indexed          : {stats['files_indexed']}")
    print(f"  Logical input size     : {_fmt_bytes(stats['logical_input_bytes'])}")
    print(f"  Chunks processed       : {stats['total_chunks_processed']:,}")
    print(f"  Unique chunks          : {stats['cold_unique_chunks']:,}")
    print(f"  Duplicate chunk refs   : {stats['duplicate_chunk_references']:,}")
    print(f"  Avg chunk size         : {stats['avg_chunk_size']:.0f} B  "
          f"(min {stats['min_chunk_size']} B / max {stats['max_chunk_size']} B)")
    print(f"  Dedup ratio            : {stats['dedup_ratio']:.4f}x")
    print(f"  Bayesian confidence    : {stats['bayesian_confidence_hot_hit']*100:.2f}%")
    print(f"  Bayes risk hot-first   : {stats['bayes_risk_hot_first_ns']:.0f} ns")
    print(f"  Bayes risk cold-first  : {stats['bayes_risk_cold_first_ns']:.0f} ns")
    print(f"  Cold index WAF (app)   : {stats['cold_index_waf']:.4f}")
    print(f"  GC reclaimed chunks    : {stats.get('gc_reclaimed_chunks', 0)}")
    print(f"  GC reclaimed bytes     : {_fmt_bytes(stats.get('gc_reclaimed_bytes', 0))}")
    print(f"  Results                : {output_dir}")
    print(f"  Store                  : {store_dir}")
    print(f"  Stats JSON             : {stats_json_path}")


if __name__ == "__main__":
    main()
