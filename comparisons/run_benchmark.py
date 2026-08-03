#!/usr/bin/env python3
"""
Multi-method de-duplication benchmark harness.

Extracts the content-defined chunk sequence from a dataset exactly once,
then feeds that identical sequence of (digest, size, payload) records to
every method under comparison, so all methods see the same chunk
boundaries and the same duplicate/unique structure. This is what makes the
comparison fair: differences in the reported numbers come only from the
lookup/index strategy, not from different chunking.

Methods benchmarked:
  - naive_hash      : comparisons.baselines.baseline_hash_dedup (exact-match
                       SQLite lookup, no Bloom/CMS/Bayesian layer)
  - hsaids_bayesian  : hsaids.cdc_hsaids.ChunkLevelHSAIDS (Bloom + Count-Min
                       Sketch + Bayesian hot/cold lookup ordering)
  - triededup_trie    : vendored TrieDedup, trie-based exact matcher
  - triededup_pairwise: vendored TrieDedup, O(n^2) pairwise baseline

Each method is run for multiple trials (default 5); mean/p50/p95 latency,
throughput, and peak RSS are reported with standard deviation across trials.

See docs/COMPARISON_PLAN.md for why LoopDelta and the learning-based
record-deduplication method are not included here.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import resource
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List

from hsaids.cdc_hsaids import CDCConfig, ChunkLevelHSAIDS, scan_files, _iter_cdc_chunks_from_data
from comparisons.baselines.baseline_hash_dedup import NaiveHashDedup
from comparisons.external import triededup_adapter


def _peak_rss_bytes() -> int:
    """Peak resident set size for this process so far, normalized to bytes."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if platform.system() == "Darwin" else raw * 1024


def _percentile(sorted_values: List[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(int(p * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[idx]


def _load_chunk_records(input_dir: Path, cdc_config: CDCConfig) -> List[Dict[str, object]]:
    """Chunk every file once and materialize (digest, size, data) records shared by all methods."""
    records: List[Dict[str, object]] = []
    for path in scan_files(input_dir):
        data = path.read_bytes()
        for chunk in _iter_cdc_chunks_from_data(data, cdc_config):
            records.append({"digest": chunk.digest, "size": chunk.size, "data": chunk.data})
    return records


def _reference_dedup_ratio(records: List[Dict[str, object]]) -> float:
    """Dedup ratio computed once from the shared chunk records, independent of
    any single engine's internal bookkeeping, so every method is compared on
    the same definition: total logical bytes fed in / bytes of first-seen chunks."""
    seen: set = set()
    logical_bytes = 0
    unique_bytes = 0
    for rec in records:
        logical_bytes += rec["size"]
        if rec["digest"] not in seen:
            seen.add(rec["digest"])
            unique_bytes += rec["size"]
    return logical_bytes / unique_bytes if unique_bytes else 1.0


def _run_naive_hash(records: List[Dict[str, object]], work_dir: Path) -> Dict[str, float]:
    from hsaids.cdc_hsaids import Chunk

    store_dir = work_dir / "naive_hash_store"
    engine = NaiveHashDedup(store_dir=store_dir)
    try:
        start = time.perf_counter()
        for rec in records:
            engine.insert_chunk(Chunk(file_offset=0, size=rec["size"], digest=rec["digest"], data=rec["data"]))
        wall = time.perf_counter() - start
        stats = engine.statistics()
    finally:
        engine.close()
    return {
        "wall_time_s": wall,
        "throughput_chunks_per_s": len(records) / wall if wall > 0 else float("inf"),
        "lookup_latency_mean_ns": stats["lookup_latency_mean_ns"],
        "lookup_latency_p50_ns": stats["lookup_latency_p50_ns"],
        "lookup_latency_p95_ns": stats["lookup_latency_p95_ns"],
        "lookup_latency_p99_ns": stats["lookup_latency_p99_ns"],
        "unique_chunks": stats["unique_chunks_inserted"],
        "duplicate_chunks": stats["duplicate_chunks_detected"],
    }


def _run_hsaids_bayesian(records: List[Dict[str, object]], work_dir: Path) -> Dict[str, float]:
    from hsaids.cdc_hsaids import Chunk

    store_dir = work_dir / "hsaids_bayesian_store"
    engine = ChunkLevelHSAIDS(store_dir=store_dir)
    try:
        start = time.perf_counter()
        for rec in records:
            engine.insert_chunk(Chunk(file_offset=0, size=rec["size"], digest=rec["digest"], data=rec["data"]))
        wall = time.perf_counter() - start
        stats = engine.statistics()
    finally:
        engine.close()
    return {
        "wall_time_s": wall,
        "throughput_chunks_per_s": len(records) / wall if wall > 0 else float("inf"),
        "lookup_latency_mean_ns": stats["micro_cost_hot_lookup_ns"],
        "bayes_risk_hot_first_ns": stats["bayes_risk_hot_first_ns"],
        "bayes_risk_cold_first_ns": stats["bayes_risk_cold_first_ns"],
        "bayesian_confidence_hot_hit": stats["bayesian_confidence_hot_hit"],
        "unique_chunks": stats["unique_chunks_inserted"],
        "duplicate_chunks": stats["duplicate_chunks_detected"],
    }


def _run_triededup(records: List[Dict[str, object]], mode: str) -> Dict[str, float]:
    digests = [rec["digest"] for rec in records]
    fn = triededup_adapter.dedup_trie if mode == "trie" else triededup_adapter.dedup_pairwise
    result = fn(digests)
    wall = result["wall_time_s"]
    return {
        "wall_time_s": wall,
        "throughput_chunks_per_s": len(digests) / wall if wall > 0 else float("inf"),
        "unique_chunks": result["unique_count"],
        "duplicate_chunks": result["duplicate_count"],
    }


_METHODS: Dict[str, Callable] = {
    "naive_hash": lambda records, work_dir: _run_naive_hash(records, work_dir),
    "hsaids_bayesian": lambda records, work_dir: _run_hsaids_bayesian(records, work_dir),
    "triededup_trie": lambda records, work_dir: _run_triededup(records, "trie"),
    "triededup_pairwise": lambda records, work_dir: _run_triededup(records, "pairwise"),
}


def run_trials(method_name: str, records: List[Dict[str, object]], trials: int, dedup_ratio: float) -> Dict[str, object]:
    fn = _METHODS[method_name]
    per_trial: List[Dict[str, float]] = []
    peak_rss_samples: List[int] = []

    for _ in range(trials):
        gc.collect()
        work_dir = Path(tempfile.mkdtemp(prefix=f"bench_{method_name}_"))
        try:
            result = fn(records, work_dir)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        per_trial.append(result)
        peak_rss_samples.append(_peak_rss_bytes())

    wall_times = [t["wall_time_s"] for t in per_trial]
    throughputs = [t["throughput_chunks_per_s"] for t in per_trial]

    summary: Dict[str, object] = {
        "method": method_name,
        "trials": trials,
        "input_chunk_count": len(records),
        "wall_time_s_mean": statistics.mean(wall_times),
        "wall_time_s_stdev": statistics.stdev(wall_times) if trials > 1 else 0.0,
        "throughput_chunks_per_s_mean": statistics.mean(throughputs),
        "throughput_chunks_per_s_stdev": statistics.stdev(throughputs) if trials > 1 else 0.0,
        "peak_rss_bytes_max": max(peak_rss_samples),
        "unique_chunks": per_trial[-1].get("unique_chunks"),
        "duplicate_chunks": per_trial[-1].get("duplicate_chunks"),
        # Computed once from the shared chunk records (see _reference_dedup_ratio),
        # not from each engine's own bookkeeping, so it is directly comparable
        # across methods regardless of how each one defines "logical input".
        "dedup_ratio": dedup_ratio,
    }

    for key in ("lookup_latency_mean_ns", "lookup_latency_p50_ns", "lookup_latency_p95_ns", "lookup_latency_p99_ns",
                "bayes_risk_hot_first_ns", "bayes_risk_cold_first_ns", "bayesian_confidence_hot_hit"):
        values = [t[key] for t in per_trial if key in t]
        if values:
            summary[f"{key}_mean"] = statistics.mean(values)
            summary[f"{key}_stdev"] = statistics.stdev(values) if len(values) > 1 else 0.0

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", help="Directory to chunk and benchmark against")
    parser.add_argument("--methods", nargs="+", default=list(_METHODS.keys()), choices=list(_METHODS.keys()))
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--min-chunk-size", type=int, default=2048)
    parser.add_argument("--avg-chunk-size", type=int, default=8192)
    parser.add_argument("--max-chunk-size", type=int, default=65536)
    parser.add_argument("--output", default=None, help="Write JSON results to this path")
    args = parser.parse_args()

    cdc_config = CDCConfig(min_size=args.min_chunk_size, avg_size=args.avg_chunk_size, max_size=args.max_chunk_size)

    print(f"[benchmark] Chunking {args.input_dir} once for all methods...")
    records = _load_chunk_records(Path(args.input_dir), cdc_config)
    dedup_ratio = _reference_dedup_ratio(records)
    print(f"[benchmark] {len(records)} chunks extracted; running {args.trials} trial(s) per method")

    results = []
    for method_name in args.methods:
        print(f"[benchmark] Running {method_name}...")
        results.append(run_trials(method_name, records, args.trials, dedup_ratio))

    print(json.dumps(results, indent=2))
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2))
        print(f"[benchmark] Results written to {out_path}")


if __name__ == "__main__":
    main()
