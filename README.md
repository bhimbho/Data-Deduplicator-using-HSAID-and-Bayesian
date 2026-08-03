# HSAIDS Chunk-Level Deduplication Prototype

This repository now contains two implementations, both under `hsaids/`:

- `hsaids/run_cdc_hsaids.py` and `hsaids/cdc_hsaids.py`: the current chunk-level HSAIDS prototype.
- `hsaids/hard_disk_hsad.py` and `hsaids/hsaids.py`: the original legacy whole-file duplicate detector.

Repository layout:

- `hsaids/` — the core implementation (both chunk-level and legacy whole-file paths).
- `data_prep/` — dataset preparation scripts (Wikipedia corpus extraction/mutation).
- `comparisons/` — quantitative comparison against baseline and external de-duplication
  methods (see `docs/COMPARISON_PLAN.md`); fully separate from `hsaids/` and imports it
  only through its public API.
- `docs/` — explanatory documents, results write-ups, and reviewer-response notes.
- `notebooks/` — analysis/presentation notebooks.
- `data/` — generated datasets (gitignored).
- `tests/` — unit tests.

The reviewer-facing implementation is the CDC/chunk-level path. It uses variable-size content-defined chunks, stores file recipes, maintains reference counts, writes unique chunks to containers, and keeps a disk-backed cold index in SQLite.

## Current Architecture

The chunk-level path performs:

1. Recursive file scan.
2. Content-defined chunking with a deterministic Gear rolling hash.
3. SHA-256 digest calculation per chunk.
4. Hot-layer lookup using an in-memory Bloom filter, Count-Min Sketch, and exact hot cache.
5. Cold-layer lookup using a SQLite-backed chunk index, persisted Bloom filter, and persisted Count-Min Sketch.
6. Unique chunk writes into append-only container files.
7. File recipe creation in SQLite, mapping each file to ordered chunk references.
8. Reference-count updates for duplicate chunks.
9. Garbage-collection bookkeeping for unreferenced chunks.
10. Metrics export for dedup ratio, chunk sizes, Bayes-risk costs, and application-level cold-index write amplification.

## Running The Chunk-Level Experiment

### Quick start (any local directory)

```bash
python3 -m hsaids.run_cdc_hsaids /path/to/dataset --reset-store
```

Useful options:

```bash
python3 -m hsaids.run_cdc_hsaids /path/to/dataset \
  --store-dir .hsaids_cdc_store \
  --output-dir cdc_results \
  --min-chunk-size 2048 \
  --avg-chunk-size 8192 \
  --max-chunk-size 65536 \
  --hot-capacity 50000 \
  --reset-store
```

The average chunk size must be a power of two because the CDC boundary rule is mask-based.

---

## Wikipedia Evaluation (Multi-GB Workload)

This is the recommended evaluation path to satisfy reviewers requiring multi-gigabyte,
uncompressed text workloads and demonstrated boundary-shift deduplication.

### Step 1 — Prepare the dataset

```bash
# Extract up to 500,000 articles from the latest English Wikipedia dump.
# The dump (~22 GB compressed) is downloaded automatically on first run.
python3 data_prep/prepare_wikipedia.py --output data/wiki_v1 --limit 500000

# Produce a mutated revision (10% of articles altered) to simulate a backup version.
python3 data_prep/prepare_wikipedia.py --output data/wiki_v2 --limit 500000 \
    --mutate 0.10 --source data/wiki_v1
```

To use an existing local dump instead of downloading:

```bash
python3 data_prep/prepare_wikipedia.py \
    --dump-file /path/to/enwiki-latest-pages-articles.xml.bz2 \
    --output data/wiki_v1 --limit 500000
```

### Step 2 — Pass 1: ingest original articles

```bash
python3 -m hsaids.run_cdc_hsaids data/wiki_v1 \
  --store-dir data/wiki_store \
  --output-dir comparisons/results/hsaids_v1 \
  --run-label v1 \
  --reset-store \
  --hot-capacity 500000 \
  --avg-chunk-size 8192
```

### Step 3 — Pass 2: ingest mutated articles against the same store

Do **not** pass `--reset-store` here — the index from Pass 1 must persist.

```bash
python3 -m hsaids.run_cdc_hsaids data/wiki_v2 \
  --store-dir data/wiki_store \
  --output-dir comparisons/results/hsaids_v2 \
  --run-label v2 \
  --hot-capacity 500000 \
  --avg-chunk-size 8192
```

### Step 4 — Compare passes

```bash
python3 comparisons/compare_runs.py \
  comparisons/results/hsaids_v1/hsaids_statistics_v1.json \
  comparisons/results/hsaids_v2/hsaids_statistics_v2.json \
  --csv comparisons/results/comparison_v1_v2.csv \
  --json comparisons/results/comparison_v1_v2.json
```

The comparison table shows per-pass values and deltas for every reported metric.
A positive `duplicate_chunk_references` delta in Pass 2 confirms that chunks from
unmodified article regions were deduplicated across the version boundary — this is
the evidence of chunk-level (not file-level) deduplication that the reviewer requires.

### Parameter guidance for Wikipedia scale

| Parameter | JPEG run | Wikipedia run | Reason |
|---|---|---|---|
| `--hot-capacity` | 50,000 | 500,000 | Millions of unique chunks; avoid premature eviction |
| `--avg-chunk-size` | 8192 | 8192 | Keep constant to compare realized average |

### Expected output scale

| Metric | Approximate value |
|---|---|
| Logical input (Pass 1 + Pass 2) | 80–90 GB |
| Articles processed | ~500,000 per pass |
| Unique chunks | Several million |
| Dedup ratio (Pass 2) | > 1.0 due to cross-version chunk reuse |

## Output Files

The runner writes into whatever `--output-dir` is given (e.g. `comparisons/results/hsaids_v1/`):

- `hsaids_statistics_<label>.csv`
- `hsaids_statistics_<label>.json`
- `file_summary.csv`
- `file_recipes.csv`
- `unique_chunks.csv`
- `duplicate_chunks.csv`

Important reported metrics include:

- `total_chunks_processed`
- `cold_unique_chunks`
- `duplicate_chunk_references`
- `avg_chunk_size`
- `min_chunk_size`
- `max_chunk_size`
- `dedup_ratio`
- `bayesian_confidence_hot_hit`
- `bayes_risk_hot_first_ns`
- `bayes_risk_cold_first_ns`
- `micro_cost_hot_lookup_ns`
- `micro_cost_cold_lookup_ns`
- `micro_cost_verify_ns`
- `micro_cost_cold_write_ns`
- `cold_index_logical_bytes_written`
- `cold_index_physical_bytes_written`
- `cold_index_waf`

## What Changed From The Original Prototype

The original code hashed each file once with MD5 and detected duplicates when whole-file hashes matched. That behavior is still available as a legacy baseline in `hsaids/hard_disk_hsad.py`, but it should not be used as evidence for block-level deduplication.

The current implementation deduplicates chunks. Each file is represented by a recipe of chunk hashes and container locations. Identical chunks across different files or shifted file versions are stored once and referenced multiple times.

## Bayesian Confidence Definition

The reported Bayesian confidence is not classification accuracy. It is the posterior mean probability that a hot-layer lookup succeeds:

```text
P_hot = (alpha + hot_hits) / (alpha + beta + hot_queries)
```

with `alpha = 1` and `beta = 1` in the current implementation.

Bayes-risk lookup ordering is based on measured runtime micro-costs:

```text
Risk(hot first)  = C_hot + (1 - P_hot)  * C_cold
Risk(cold first) = C_cold + (1 - P_cold) * C_hot
```

The engine chooses the lower-risk lookup order for each chunk.

## SSD / WAF Scope

The cold layer is disk-backed through SQLite. The reported `cold_index_waf` is an application-level write amplification estimate:

```text
cold_index_waf = physical cold-index file growth / logical cold-index update bytes
```

This is not the internal NAND flash WAF of a physical SSD controller. To report device-level SSD WAF, the experiment must be run on hardware where block-device write counters or SMART/NVMe telemetry can be sampled before and after the run.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

The tests verify that CDC produces multiple variable-size chunks, identical files deduplicate as chunk references, and shifted files can reuse content-defined chunks.
