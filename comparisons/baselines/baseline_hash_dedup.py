#!/usr/bin/env python3
"""
Naive exact-hash chunk-level deduplication baseline.

Uses the same Gear-hash CDC chunker as hsaids.cdc_hsaids so the unit of
deduplication (variable-size content-defined chunks) is identical to the
Bayesian HSAIDS engine. The lookup path here is intentionally the simplest
possible correct implementation: a single SQLite table keyed by chunk hash,
queried directly with no Bloom filter, no Count-Min Sketch, and no hot/cold
tier or Bayesian lookup-order optimization.

This is the "conventional/static baseline" referenced in the paper as
"HSAIDS v1" — this module makes that baseline's numbers reproducible from
checked-in code and a real measured run, rather than an assumed figure.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hsaids.cdc_hsaids import CDCConfig, Chunk, _iter_cdc_chunks_from_data, scan_files


@dataclass
class ContainerStore:
    root: Path
    max_container_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_id = 0
        self.current_size = 0
        self.physical_bytes_written = 0
        self._open_next_container()

    def _open_next_container(self) -> None:
        self.current_id += 1
        self.current_path = self.root / f"container_{self.current_id:06d}.bin"
        self.current_size = self.current_path.stat().st_size if self.current_path.exists() else 0

    def append(self, data: bytes) -> Tuple[int, int]:
        if self.current_size + len(data) > self.max_container_bytes and self.current_size > 0:
            self._open_next_container()
        offset = self.current_size
        with self.current_path.open("ab") as handle:
            handle.write(data)
        self.current_size += len(data)
        self.physical_bytes_written += len(data)
        return self.current_id, offset


class NaiveHashDedup:
    """Exact-match chunk dedup: one SQLite lookup per chunk, no hot/cold tiering."""

    def __init__(self, store_dir: Path, cdc_config: Optional[CDCConfig] = None):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.cdc_config = cdc_config or CDCConfig()
        self.containers = ContainerStore(self.store_dir / "containers")
        self.db_path = self.store_dir / "index.sqlite"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                hash TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                refcount INTEGER NOT NULL,
                container_id INTEGER NOT NULL,
                container_offset INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                logical_size INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

        self._chunk_count = 0
        self._chunk_sum = 0
        self._chunk_min = 0
        self._chunk_max = 0
        self.logical_input_bytes = 0
        self.unique_chunks = 0
        self.duplicate_chunks = 0
        self.unique_chunk_bytes = 0
        self.lookup_latencies_ns: List[int] = []
        self._write_count = 0

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def _lookup(self, chunk_hash: str) -> Tuple[Optional[Tuple[int, int, int, int]], int]:
        start = time.perf_counter_ns()
        row = self.conn.execute(
            "SELECT size, refcount, container_id, container_offset FROM chunks WHERE hash = ?",
            (chunk_hash,),
        ).fetchone()
        elapsed = time.perf_counter_ns() - start
        return row, elapsed

    def insert_chunk(self, chunk: Chunk) -> Dict[str, int]:
        s = chunk.size
        self._chunk_count += 1
        self._chunk_sum += s
        self.logical_input_bytes += s
        if self._chunk_count == 1:
            self._chunk_min = self._chunk_max = s
        elif s < self._chunk_min:
            self._chunk_min = s
        elif s > self._chunk_max:
            self._chunk_max = s

        row, elapsed = self._lookup(chunk.digest)
        self.lookup_latencies_ns.append(elapsed)

        if row is not None:
            self.conn.execute(
                "UPDATE chunks SET refcount = refcount + 1 WHERE hash = ?",
                (chunk.digest,),
            )
            self.duplicate_chunks += 1
            self._maybe_commit()
            return {"hash": chunk.digest, "size": chunk.size, "duplicate": 1}

        container_id, container_offset = self.containers.append(chunk.data)
        self.conn.execute(
            "INSERT OR IGNORE INTO chunks(hash, size, refcount, container_id, container_offset) VALUES (?, ?, 1, ?, ?)",
            (chunk.digest, chunk.size, container_id, container_offset),
        )
        self.unique_chunks += 1
        self.unique_chunk_bytes += chunk.size
        self._maybe_commit()
        return {"hash": chunk.digest, "size": chunk.size, "duplicate": 0}

    def _maybe_commit(self, every: int = 2000) -> None:
        self._write_count += 1
        if self._write_count >= every:
            self.conn.commit()
            self._write_count = 0

    def ingest_file(self, path: Path) -> Dict[str, int]:
        path = Path(path)
        data = path.read_bytes()
        logical_size = len(data)

        chunk_count = 0
        duplicate_count = 0
        for chunk in _iter_cdc_chunks_from_data(data, self.cdc_config):
            result = self.insert_chunk(chunk)
            chunk_count += 1
            duplicate_count += result["duplicate"]

        self.conn.execute(
            "INSERT INTO files(path, logical_size, chunk_count) VALUES (?, ?, ?)",
            (str(path), logical_size, chunk_count),
        )
        return {
            "path": str(path),
            "logical_size": logical_size,
            "chunk_count": chunk_count,
            "duplicate_chunks": duplicate_count,
            "unique_chunks": chunk_count - duplicate_count,
        }

    def statistics(self) -> Dict[str, float]:
        avg_chunk_size = self._chunk_sum / self._chunk_count if self._chunk_count else 0.0
        latencies = sorted(self.lookup_latencies_ns)
        n = len(latencies)

        def percentile(p: float) -> float:
            if not n:
                return 0.0
            idx = min(int(p * n), n - 1)
            return float(latencies[idx])

        dedup_ratio = (
            self.logical_input_bytes / self.unique_chunk_bytes if self.unique_chunk_bytes else 1.0
        )
        return {
            "total_chunks_processed": self._chunk_count,
            "unique_chunks_inserted": self.unique_chunks,
            "duplicate_chunks_detected": self.duplicate_chunks,
            "avg_chunk_size": avg_chunk_size,
            "min_chunk_size": self._chunk_min,
            "max_chunk_size": self._chunk_max,
            "logical_input_bytes": self.logical_input_bytes,
            "unique_chunk_bytes": self.unique_chunk_bytes,
            "dedup_ratio": dedup_ratio,
            "lookup_latency_mean_ns": sum(latencies) / n if n else 0.0,
            "lookup_latency_p50_ns": percentile(0.50),
            "lookup_latency_p95_ns": percentile(0.95),
            "lookup_latency_p99_ns": percentile(0.99),
        }


def ingest_directory(
    input_dir: Path,
    store_dir: Path,
    cdc_config: Optional[CDCConfig] = None,
    include_hidden: bool = False,
) -> Tuple[List[Dict[str, int]], Dict[str, float]]:
    engine = NaiveHashDedup(store_dir=store_dir, cdc_config=cdc_config)
    file_summaries: List[Dict[str, int]] = []
    try:
        for path in scan_files(input_dir, include_hidden=include_hidden):
            file_summaries.append(engine.ingest_file(path))
        stats = engine.statistics()
        return file_summaries, stats
    finally:
        engine.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Naive exact-hash chunk dedup baseline")
    parser.add_argument("input_dir")
    parser.add_argument("--store-dir", default=".naive_hash_store")
    parser.add_argument("--output", default=None, help="Write statistics JSON to this path")
    parser.add_argument("--min-chunk-size", type=int, default=2048)
    parser.add_argument("--avg-chunk-size", type=int, default=8192)
    parser.add_argument("--max-chunk-size", type=int, default=65536)
    args = parser.parse_args()

    config = CDCConfig(
        min_size=args.min_chunk_size,
        avg_size=args.avg_chunk_size,
        max_size=args.max_chunk_size,
    )
    _summaries, stats = ingest_directory(Path(args.input_dir), Path(args.store_dir), cdc_config=config)
    print(json.dumps(stats, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(stats, indent=2))
