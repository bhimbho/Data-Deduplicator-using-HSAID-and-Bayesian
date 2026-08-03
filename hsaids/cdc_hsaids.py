#!/usr/bin/env python3
"""
Chunk-level HSAIDS prototype.

This module implements a content-defined chunking (CDC) deduplication path with:
- variable-size chunks produced by a deterministic Gear rolling hash,
- chunk-level recipes for each input file,
- a hot in-memory Bloom/CMS layer,
- a disk-backed cold layer using SQLite,
- container files for unique chunk payloads,
- Bayesian risk based lookup ordering,
- reference counts, garbage-collection metadata, and write amplification metrics.

It is intentionally still a research prototype, but the unit of deduplication is
now a chunk rather than a whole file.
"""

from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple


def _stable_hash_int(value: str, seed: int, modulo: int) -> int:
    data = f"{seed}:{value}".encode("utf-8")
    digest = hashlib.blake2b(data, digest_size=8).digest()
    return int.from_bytes(digest, "big") % modulo


class BloomFilter:
    def __init__(self, size: int, num_hashes: int = 4):
        self.size = size
        self.num_hashes = num_hashes
        self.bits = bytearray(size)
        self.updates = 0
        self.queries = 0
        self.positives = 0

    def add(self, item: str) -> None:
        for i in range(self.num_hashes):
            self.bits[_stable_hash_int(item, i, self.size)] = 1
        self.updates += 1

    def contains(self, item: str) -> bool:
        self.queries += 1
        result = all(self.bits[_stable_hash_int(item, i, self.size)] for i in range(self.num_hashes))
        if result:
            self.positives += 1
        return result


class CountMinSketch:
    def __init__(self, width: int, depth: int = 4):
        self.width = width
        self.depth = depth
        self.table = [[0] * width for _ in range(depth)]
        self.total_updates = 0

    def update(self, item: str, count: int = 1) -> None:
        for row in range(self.depth):
            self.table[row][_stable_hash_int(item, row + 1000, self.width)] += count
        self.total_updates += count

    def estimate(self, item: str) -> int:
        return min(self.table[row][_stable_hash_int(item, row + 1000, self.width)] for row in range(self.depth))


@dataclass(frozen=True)
class CDCConfig:
    min_size: int = 2048
    avg_size: int = 8192
    max_size: int = 65536
    read_size: int = 1024 * 1024

    @property
    def boundary_mask(self) -> int:
        if self.avg_size <= 0 or self.avg_size & (self.avg_size - 1):
            raise ValueError("avg_size must be a power of two for mask-based CDC")
        return self.avg_size - 1


@dataclass
class Chunk:
    file_offset: int
    size: int
    digest: str
    data: bytes


def _gear_table() -> Tuple[int, ...]:
    rng = random.Random(0xC0DEC0DE)
    return tuple(rng.getrandbits(64) for _ in range(256))


GEAR_TABLE = _gear_table()
UINT64_MASK = (1 << 64) - 1


def _iter_cdc_chunks_from_data(data: bytes, config: CDCConfig) -> Iterator[Chunk]:
    """Yield variable-size chunks from an already-loaded bytes buffer."""
    n = len(data)
    if n == 0:
        return
    mask = config.boundary_mask
    min_size = config.min_size
    max_size = config.max_size
    chunk_start = 0

    while chunk_start < n:
        # Jump past the minimum size without checking boundaries
        i = min(chunk_start + min_size, n)
        rolling = 0
        # Rolling hash over the min_size prefix (no boundary check needed)
        for j in range(chunk_start, i):
            rolling = ((rolling << 1) + GEAR_TABLE[data[j]]) & UINT64_MASK

        # Scan for a natural boundary up to max_size
        limit = min(chunk_start + max_size, n)
        while i < limit:
            rolling = ((rolling << 1) + GEAR_TABLE[data[i]]) & UINT64_MASK
            i += 1
            if (rolling & mask) == 0:
                break

        payload = data[chunk_start:i]
        yield Chunk(
            file_offset=chunk_start,
            size=len(payload),
            digest=hashlib.sha256(payload).hexdigest(),
            data=payload,
        )
        chunk_start = i


def iter_cdc_chunks(path: Path, config: CDCConfig) -> Iterator[Chunk]:
    """Yield variable-size chunks using Gear CDC boundaries."""
    yield from _iter_cdc_chunks_from_data(path.read_bytes(), config)


class BayesianRiskOptimizer:
    """Chooses lookup order from measured hit probabilities and micro-costs."""

    def __init__(self, alpha: float = 1.0, beta: float = 1.0):
        self.alpha = alpha
        self.beta = beta
        self.hot_queries = 0
        self.hot_hits = 0
        self.cold_queries = 0
        self.cold_hits = 0
        self.cost_samples_ns: Dict[str, List[int]] = {
            "hot_lookup": [],
            "cold_lookup": [],
            "verify": [],
            "cold_write": [],
        }

    def _bounded_samples(self, name: str, elapsed_ns: int) -> None:
        samples = self.cost_samples_ns[name]
        samples.append(max(elapsed_ns, 1))
        if len(samples) > 1000:
            del samples[: len(samples) - 1000]

    def record_hot_lookup(self, hit: bool, elapsed_ns: int) -> None:
        self.hot_queries += 1
        self.hot_hits += int(hit)
        self._bounded_samples("hot_lookup", elapsed_ns)

    def record_cold_lookup(self, hit: bool, elapsed_ns: int) -> None:
        self.cold_queries += 1
        self.cold_hits += int(hit)
        self._bounded_samples("cold_lookup", elapsed_ns)

    def record_verify(self, elapsed_ns: int) -> None:
        self._bounded_samples("verify", elapsed_ns)

    def record_cold_write(self, elapsed_ns: int) -> None:
        self._bounded_samples("cold_write", elapsed_ns)

    def _hit_probability(self, hits: int, queries: int) -> float:
        return (self.alpha + hits) / (self.alpha + self.beta + queries)

    def _cost(self, name: str, default_ns: int) -> float:
        samples = self.cost_samples_ns[name]
        return sum(samples) / len(samples) if samples else float(default_ns)

    def hot_hit_probability(self) -> float:
        return self._hit_probability(self.hot_hits, self.hot_queries)

    def cold_hit_probability(self) -> float:
        return self._hit_probability(self.cold_hits, self.cold_queries)

    def risk_hot_first(self) -> float:
        p_hot = self.hot_hit_probability()
        return self._cost("hot_lookup", 5_000) + (1.0 - p_hot) * self._cost("cold_lookup", 200_000)

    def risk_cold_first(self) -> float:
        p_cold = self.cold_hit_probability()
        return self._cost("cold_lookup", 200_000) + (1.0 - p_cold) * self._cost("hot_lookup", 5_000)

    def should_check_hot_first(self) -> bool:
        return self.risk_hot_first() <= self.risk_cold_first()

    def confidence(self) -> float:
        """Posterior probability that hot lookup succeeds, not duplicate accuracy."""
        return self.hot_hit_probability()

    def micro_costs_ns(self) -> Dict[str, float]:
        return {
            name: self._cost(name, default)
            for name, default in {
                "hot_lookup": 5_000,
                "cold_lookup": 200_000,
                "verify": 50_000,
                "cold_write": 500_000,
            }.items()
        }


class ContainerStore:
    def __init__(self, root: Path, max_container_bytes: int = 64 * 1024 * 1024):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_container_bytes = max_container_bytes
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


class DiskBackedColdLayer:
    def __init__(
        self,
        db_path: Path,
        bloom_size: int = 1_000_003,
        cms_width: int = 200_003,
        cms_depth: int = 4,
        commit_every: int = 2000,
    ):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.bloom_size = bloom_size
        self.cms_width = cms_width
        self.cms_depth = cms_depth
        self.commit_every = commit_every
        self.logical_index_bytes_written = 0
        self.physical_index_bytes_written = 0
        self._write_count = 0
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-65536")   # 64 MB page cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self._init_schema()
        self._initial_db_bytes = self._storage_bytes()

    def close(self) -> None:
        self.conn.commit()
        after = self._storage_bytes()
        self.physical_index_bytes_written = max(after - self._initial_db_bytes, 0)
        self.conn.close()

    def _storage_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.db_path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def _track_write(self, logical_bytes: int, func) -> Tuple[object, int]:
        start = time.perf_counter_ns()
        result = func()
        elapsed = time.perf_counter_ns() - start
        self.logical_index_bytes_written += logical_bytes
        self._write_count += 1
        if self._write_count >= self.commit_every:
            self.conn.commit()
            self._write_count = 0
        return result, elapsed

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                hash TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                refcount INTEGER NOT NULL,
                frequency INTEGER NOT NULL,
                container_id INTEGER NOT NULL,
                container_offset INTEGER NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                logical_size INTEGER NOT NULL,
                whole_file_sha256 TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_chunks (
                file_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                file_offset INTEGER NOT NULL,
                size INTEGER NOT NULL,
                container_id INTEGER NOT NULL,
                container_offset INTEGER NOT NULL,
                PRIMARY KEY (file_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS cold_bloom (
                position INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS cold_cms (
                row INTEGER NOT NULL,
                col INTEGER NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (row, col)
            );
            CREATE TABLE IF NOT EXISTS gc_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reclaimed_chunks INTEGER NOT NULL,
                reclaimed_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self.conn.commit()

    def bloom_contains(self, chunk_hash: str, num_hashes: int = 4) -> bool:
        positions = [_stable_hash_int(chunk_hash, i + 2000, self.bloom_size) for i in range(num_hashes)]
        rows = self.conn.execute(
            f"SELECT position FROM cold_bloom WHERE position IN ({','.join('?' for _ in positions)})",
            positions,
        ).fetchall()
        return len(rows) == len(positions)

    def bloom_add(self, chunk_hash: str, num_hashes: int = 4) -> int:
        positions = [_stable_hash_int(chunk_hash, i + 2000, self.bloom_size) for i in range(num_hashes)]

        def write_positions():
            self.conn.executemany(
                "INSERT OR IGNORE INTO cold_bloom(position) VALUES (?)",
                [(position,) for position in positions],
            )

        _, elapsed = self._track_write(8 * len(positions), write_positions)
        return elapsed

    def cms_update(self, chunk_hash: str, count: int = 1) -> int:
        cells = [(row, _stable_hash_int(chunk_hash, row + 3000, self.cms_width), count) for row in range(self.cms_depth)]

        def write_cells():
            self.conn.executemany(
                """
                INSERT INTO cold_cms(row, col, count) VALUES (?, ?, ?)
                ON CONFLICT(row, col) DO UPDATE SET count = count + excluded.count
                """,
                cells,
            )

        _, elapsed = self._track_write(24 * len(cells), write_cells)
        return elapsed

    def chunk_lookup(self, chunk_hash: str) -> Optional[Dict[str, int]]:
        row = self.conn.execute(
            """
            SELECT hash, size, refcount, frequency, container_id, container_offset
            FROM chunks WHERE hash = ?
            """,
            (chunk_hash,),
        ).fetchone()
        if row is None:
            return None
        return {
            "hash": row[0],
            "size": row[1],
            "refcount": row[2],
            "frequency": row[3],
            "container_id": row[4],
            "container_offset": row[5],
        }

    def add_unique_chunk(self, chunk_hash: str, size: int, container_id: int, container_offset: int) -> int:
        now = time.time()
        logical_bytes = len(chunk_hash) + 48

        def write_chunk():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO chunks(hash, size, refcount, frequency, container_id, container_offset, first_seen, last_seen)
                VALUES (?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (chunk_hash, size, container_id, container_offset, now, now),
            )

        _, elapsed = self._track_write(logical_bytes, write_chunk)
        return elapsed

    def increment_chunk(self, chunk_hash: str) -> int:
        logical_bytes = len(chunk_hash) + 24

        def write_refcount():
            self.conn.execute(
                """
                UPDATE chunks
                SET refcount = refcount + 1,
                    frequency = frequency + 1,
                    last_seen = ?
                WHERE hash = ?
                """,
                (time.time(), chunk_hash),
            )

        _, elapsed = self._track_write(logical_bytes, write_refcount)
        return elapsed

    def create_file_recipe(self, path: Path, logical_size: int, whole_file_sha256: str, chunks: List[Dict[str, int]]) -> int:
        def write_recipe():
            cursor = self.conn.execute(
                """
                INSERT INTO files(path, logical_size, whole_file_sha256, chunk_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(path), logical_size, whole_file_sha256, len(chunks), time.time()),
            )
            file_id = int(cursor.lastrowid)
            self.conn.executemany(
                """
                INSERT INTO file_chunks(file_id, ordinal, chunk_hash, file_offset, size, container_id, container_offset)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        file_id,
                        idx,
                        item["hash"],
                        item["file_offset"],
                        item["size"],
                        item["container_id"],
                        item["container_offset"],
                    )
                    for idx, item in enumerate(chunks)
                ],
            )
            return file_id

        logical_bytes = 128 + sum(len(item["hash"]) + 32 for item in chunks)
        file_id, _elapsed = self._track_write(logical_bytes, write_recipe)
        return int(file_id)

    def garbage_collect(self) -> Dict[str, int]:
        rows = self.conn.execute("SELECT hash, size FROM chunks WHERE refcount <= 0").fetchall()
        reclaimed_chunks = len(rows)
        reclaimed_bytes = sum(row[1] for row in rows)

        def delete_dead():
            self.conn.execute("DELETE FROM chunks WHERE refcount <= 0")
            self.conn.execute(
                "INSERT INTO gc_events(reclaimed_chunks, reclaimed_bytes, created_at) VALUES (?, ?, ?)",
                (reclaimed_chunks, reclaimed_bytes, time.time()),
            )

        self._track_write(32 * max(reclaimed_chunks, 1), delete_dead)
        return {"reclaimed_chunks": reclaimed_chunks, "reclaimed_bytes": reclaimed_bytes}

    def delete_file_recipe(self, file_id: int) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT chunk_hash FROM file_chunks WHERE file_id = ?",
            (file_id,),
        ).fetchall()
        chunk_hashes = [row[0] for row in rows]

        def delete_recipe():
            self.conn.executemany(
                "UPDATE chunks SET refcount = refcount - 1 WHERE hash = ?",
                [(chunk_hash,) for chunk_hash in chunk_hashes],
            )
            self.conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
            self.conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))

        logical_bytes = sum(len(chunk_hash) + 16 for chunk_hash in chunk_hashes) + 64
        self._track_write(logical_bytes, delete_recipe)
        return {"file_id": file_id, "released_chunk_references": len(chunk_hashes)}

    def stats(self) -> Dict[str, float]:
        chunk_count = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        file_count = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        unique_bytes = self.conn.execute("SELECT COALESCE(SUM(size), 0) FROM chunks").fetchone()[0]
        total_refs = self.conn.execute("SELECT COALESCE(SUM(refcount), 0) FROM chunks").fetchone()[0]
        duplicate_refs = max(total_refs - chunk_count, 0)
        return {
            "cold_unique_chunks": chunk_count,
            "files_indexed": file_count,
            "physical_unique_chunk_bytes": unique_bytes,
            "total_chunk_references": total_refs,
            "duplicate_chunk_references": duplicate_refs,
            "cold_index_logical_bytes_written": self.logical_index_bytes_written,
            "cold_index_physical_bytes_written": self.physical_index_bytes_written,
            "cold_index_waf": (
                self.physical_index_bytes_written / self.logical_index_bytes_written
                if self.logical_index_bytes_written
                else 0.0
            ),
        }


class ChunkLevelHSAIDS:
    def __init__(
        self,
        store_dir: Path,
        cdc_config: Optional[CDCConfig] = None,
        hot_bloom_size: int = 200_003,
        hot_cms_width: int = 100_003,
        hot_capacity: int = 50_000,
        commit_every: int = 2000,
    ):
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.cdc_config = cdc_config or CDCConfig()
        self.hot_bloom = BloomFilter(hot_bloom_size)
        self.hot_cms = CountMinSketch(hot_cms_width)
        self.hot_exact: OrderedDict[str, Dict[str, int]] = OrderedDict()
        self.hot_capacity = hot_capacity
        self.cold = DiskBackedColdLayer(
            self.store_dir / "cold_index.sqlite",
            commit_every=commit_every,
        )
        self.containers = ContainerStore(self.store_dir / "containers")
        self.optimizer = BayesianRiskOptimizer()
        # Running stats replace the unbounded chunk_sizes list (~361 MB at 10M chunks)
        self._chunk_count = 0
        self._chunk_sum = 0
        self._chunk_min = 0
        self._chunk_max = 0
        self.logical_input_bytes = 0
        self.unique_chunk_bytes = 0
        self.duplicate_chunks = 0
        self.unique_chunks = 0
        self.boundary_resets = 0
        self.hot_hits = 0
        self.cold_hits = 0
        self.misses = 0

    def close(self) -> None:
        self.cold.close()

    def _hot_lookup(self, chunk_hash: str) -> Optional[Dict[str, int]]:
        if not self.hot_bloom.contains(chunk_hash):
            return None
        entry = self.hot_exact.get(chunk_hash)
        if entry is not None:
            self.hot_exact.move_to_end(chunk_hash)
        return entry

    def _remember_hot(self, chunk_hash: str, entry: Dict[str, int]) -> None:
        self.hot_bloom.add(chunk_hash)
        self.hot_cms.update(chunk_hash)
        self.hot_exact[chunk_hash] = entry
        self.hot_exact.move_to_end(chunk_hash)
        while len(self.hot_exact) > self.hot_capacity:
            self.hot_exact.popitem(last=False)

    def _lookup(self, chunk_hash: str) -> Tuple[Optional[Dict[str, int]], str]:
        # The cold Bloom filter acts as a pre-filter hint only; at Wikipedia scale
        # (894k+ chunks, bloom_size=1M) the filter is ~97% saturated, so we skip
        # the Bloom gate for reads and always call chunk_lookup directly to avoid
        # false-negative misses that would cause UNIQUE constraint crashes on insert.
        hot_first = self.optimizer.should_check_hot_first()
        if hot_first:
            start = time.perf_counter_ns()
            hot_entry = self._hot_lookup(chunk_hash)
            elapsed = time.perf_counter_ns() - start
            self.optimizer.record_hot_lookup(hot_entry is not None, elapsed)
            if hot_entry is not None:
                return hot_entry, "hot"

            start = time.perf_counter_ns()
            cold_entry = self.cold.chunk_lookup(chunk_hash)
            elapsed = time.perf_counter_ns() - start
            self.optimizer.record_cold_lookup(cold_entry is not None, elapsed)
            if cold_entry is not None:
                return cold_entry, "cold"
            return None, "miss"

        start = time.perf_counter_ns()
        cold_entry = self.cold.chunk_lookup(chunk_hash)
        elapsed = time.perf_counter_ns() - start
        self.optimizer.record_cold_lookup(cold_entry is not None, elapsed)
        if cold_entry is not None:
            return cold_entry, "cold"

        start = time.perf_counter_ns()
        hot_entry = self._hot_lookup(chunk_hash)
        elapsed = time.perf_counter_ns() - start
        self.optimizer.record_hot_lookup(hot_entry is not None, elapsed)
        if hot_entry is not None:
            return hot_entry, "hot"
        return None, "miss"

    def insert_chunk(self, chunk: Chunk) -> Dict[str, int]:
        s = chunk.size
        self._chunk_count += 1
        self._chunk_sum += s
        if self._chunk_count == 1:
            self._chunk_min = self._chunk_max = s
        elif s < self._chunk_min:
            self._chunk_min = s
        elif s > self._chunk_max:
            self._chunk_max = s
        entry, layer = self._lookup(chunk.digest)

        if entry is not None:
            verify_start = time.perf_counter_ns()
            # The SHA-256 digest is the verifier. This hook measures the cost
            # of the exact check path and keeps the risk model tied to runtime.
            hashlib.sha256(chunk.data).hexdigest() == chunk.digest
            self.optimizer.record_verify(time.perf_counter_ns() - verify_start)

            elapsed = self.cold.increment_chunk(chunk.digest)
            self.optimizer.record_cold_write(elapsed)
            self.cold.cms_update(chunk.digest)
            self.duplicate_chunks += 1
            if layer == "hot":
                self.hot_hits += 1
            else:
                self.cold_hits += 1
                self._remember_hot(chunk.digest, entry)
            return {**entry, "hash": chunk.digest, "file_offset": chunk.file_offset, "size": chunk.size, "duplicate": 1}

        container_id, container_offset = self.containers.append(chunk.data)
        write_start = time.perf_counter_ns()
        self.cold.add_unique_chunk(chunk.digest, chunk.size, container_id, container_offset)
        self.cold.bloom_add(chunk.digest)
        self.cold.cms_update(chunk.digest)
        self.optimizer.record_cold_write(time.perf_counter_ns() - write_start)

        entry = {
            "hash": chunk.digest,
            "size": chunk.size,
            "refcount": 1,
            "frequency": 1,
            "container_id": container_id,
            "container_offset": container_offset,
        }
        self._remember_hot(chunk.digest, entry)
        self.unique_chunks += 1
        self.unique_chunk_bytes += chunk.size
        self.misses += 1
        return {**entry, "file_offset": chunk.file_offset, "duplicate": 0}

    def ingest_file(self, path: Path) -> Dict[str, int]:
        path = Path(path)
        data = path.read_bytes()
        logical_size = len(data)
        self.logical_input_bytes += logical_size
        whole_file_sha256 = hashlib.sha256(data).hexdigest()
        recipe_chunks: List[Dict[str, int]] = []

        for chunk in _iter_cdc_chunks_from_data(data, self.cdc_config):
            recipe_chunks.append(self.insert_chunk(chunk))

        file_id = self.cold.create_file_recipe(path, logical_size, whole_file_sha256, recipe_chunks)
        return {
            "file_id": file_id,
            "path": str(path),
            "logical_size": logical_size,
            "chunk_count": len(recipe_chunks),
            "duplicate_chunks": sum(item["duplicate"] for item in recipe_chunks),
            "unique_chunks": sum(1 - item["duplicate"] for item in recipe_chunks),
        }

    def garbage_collect(self) -> Dict[str, int]:
        return self.cold.garbage_collect()

    def delete_file_recipe(self, file_id: int) -> Dict[str, int]:
        return self.cold.delete_file_recipe(file_id)

    def statistics(self) -> Dict[str, float]:
        cold_stats = self.cold.stats()
        avg_chunk_size = self._chunk_sum / self._chunk_count if self._chunk_count else 0.0
        min_chunk_size = self._chunk_min if self._chunk_count else 0
        max_chunk_size = self._chunk_max if self._chunk_count else 0
        dedup_ratio = (
            self.logical_input_bytes / cold_stats["physical_unique_chunk_bytes"]
            if cold_stats["physical_unique_chunk_bytes"]
            else 1.0
        )
        return {
            **cold_stats,
            "logical_input_bytes": self.logical_input_bytes,
            "unique_chunk_bytes": self.unique_chunk_bytes,
            "container_physical_bytes_written": self.containers.physical_bytes_written,
            "total_chunks_processed": self._chunk_count,
            "unique_chunks_inserted": self.unique_chunks,
            "duplicate_chunks_detected": self.duplicate_chunks,
            "avg_chunk_size": avg_chunk_size,
            "min_chunk_size": min_chunk_size,
            "max_chunk_size": max_chunk_size,
            "dedup_ratio": dedup_ratio,
            "hot_hits": self.hot_hits,
            "cold_hits": self.cold_hits,
            "misses": self.misses,
            "bayesian_confidence_hot_hit": self.optimizer.confidence(),
            "bayes_risk_hot_first_ns": self.optimizer.risk_hot_first(),
            "bayes_risk_cold_first_ns": self.optimizer.risk_cold_first(),
            **{f"micro_cost_{name}_ns": value for name, value in self.optimizer.micro_costs_ns().items()},
            "cdc_min_size": self.cdc_config.min_size,
            "cdc_avg_target_size": self.cdc_config.avg_size,
            "cdc_max_size": self.cdc_config.max_size,
        }


def scan_files(root: Path, include_hidden: bool = False) -> List[Path]:
    root = Path(root)
    files: List[Path] = []
    for current_root, dirs, filenames in os.walk(root):
        if not include_hidden:
            dirs[:] = [name for name in dirs if not name.startswith(".")]
        for filename in filenames:
            if not include_hidden and filename.startswith("."):
                continue
            path = Path(current_root) / filename
            if path.is_file():
                files.append(path)
    return files


def ingest_directory(
    input_dir: Path,
    store_dir: Path,
    include_hidden: bool = False,
    cdc_config: Optional[CDCConfig] = None,
    hot_capacity: int = 50_000,
    hot_bloom_size: int = 200_003,
    hot_cms_width: int = 100_003,
    commit_every: int = 2000,
) -> Tuple[List[Dict[str, int]], Dict[str, float]]:
    engine = ChunkLevelHSAIDS(
        store_dir=store_dir,
        cdc_config=cdc_config,
        hot_capacity=hot_capacity,
        hot_bloom_size=hot_bloom_size,
        hot_cms_width=hot_cms_width,
        commit_every=commit_every,
    )
    file_summaries: List[Dict[str, int]] = []
    try:
        for path in scan_files(input_dir, include_hidden=include_hidden):
            file_summaries.append(engine.ingest_file(path))
        gc_stats = engine.garbage_collect()
        stats = {**engine.statistics(), **{f"gc_{key}": value for key, value in gc_stats.items()}}
        return file_summaries, stats
    finally:
        engine.close()
