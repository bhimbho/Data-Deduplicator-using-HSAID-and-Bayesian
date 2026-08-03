# HSAIDS Implementation Explanation

This repository has two HSAIDS-related paths:

- `cdc_hsaids.py` plus `run_cdc_hsaids.py`: the current chunk-level implementation.
- `hsaids.py` plus `hard_disk_hsad.py`: the legacy whole-file duplicate detector.

The current implementation should be used for architecture and reviewer discussions.

## Current Chunk-Level Flow

1. A file is scanned as bytes.
2. A Gear rolling hash chooses content-defined chunk boundaries.
3. Each chunk is hashed with SHA-256.
4. The hot layer checks an in-memory Bloom filter, Count-Min Sketch, and exact cache.
5. The cold layer checks a SQLite-backed Bloom filter, Count-Min Sketch, and exact chunk table.
6. Unique chunks are appended to container files.
7. Duplicate chunks increment reference counts instead of writing payload bytes again.
8. A file recipe is stored as ordered chunk references.

This means deduplication happens at chunk level, not whole-file level.

## Content-Defined Chunking

The default CDC parameters are:

```text
min chunk size: 2 KiB
average target size: 8 KiB
max chunk size: 64 KiB
```

The boundary condition is:

```text
chunk_size >= min_size and (rolling_hash & (avg_size - 1)) == 0
```

A forced boundary is emitted when `chunk_size >= max_size`.

The actual average size is measured and exported as `avg_chunk_size`.

## Boundary-Shift Handling

Fixed-size chunking performs poorly when bytes are inserted or deleted near the start of a stream, because every later block boundary shifts. CDC reduces this problem because boundaries are selected from local byte content. After an insertion or deletion, boundaries can resynchronize later in the file, allowing unchanged byte regions to share chunk hashes.

For compressed formats such as JPEG, this helps only when byte-identical compressed regions exist. Visually similar JPEGs usually do not deduplicate well because compression changes the byte stream.

## Hot And Cold Layers

The hot layer is memory-resident:

- Bloom filter
- Count-Min Sketch
- bounded exact cache

The cold layer is disk-backed:

- SQLite chunk table
- SQLite file recipe tables
- SQLite Bloom positions
- SQLite Count-Min Sketch counters

The hot cache capacity can be constrained with `--hot-capacity` to evaluate behavior under limited DRAM.

## Bayesian Risk Model

The Bayesian confidence metric is:

```text
P_hot = (alpha + hot_hits) / (alpha + beta + hot_queries)
```

It estimates whether the hot layer will answer a lookup. It is not duplicate-detection accuracy.

The lookup order compares:

```text
Risk(hot first)  = C_hot + (1 - P_hot)  * C_cold
Risk(cold first) = C_cold + (1 - P_cold) * C_hot
```

where costs are measured from live runtime operations in nanoseconds.

## Garbage Collection

Chunk records maintain reference counts. Garbage collection removes chunk records whose reference count reaches zero and reports reclaimed chunk count and bytes. The current experiment does not physically compact container files; it records reclaimable payload bytes and index cleanup.

## Write Amplification Metric

The exported `cold_index_waf` is an application-level estimate:

```text
cold_index_waf =
  cold_index_physical_bytes_written / cold_index_logical_bytes_written
```

It is based on SQLite database/WAL/SHM file growth. It does not claim to be NAND-level SSD controller WAF.

## Legacy Whole-File Path

The legacy `hard_disk_hsad.py` script computes one MD5 hash per file and detects exact whole-file duplicates. It is useful as a simple baseline, but it should not be presented as block-level or CDC deduplication.
