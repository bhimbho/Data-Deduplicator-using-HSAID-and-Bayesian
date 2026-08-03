# Point-by-Point Reviewer Response Notes

## Concern 1: The workload is too small and does not match a system designed for multi-GB or TB-scale backup streams.

We agree that the ISIC JPEG dataset alone is not sufficient to establish scalability for data-center backup workloads. In the implementation, ISIC is treated as an exact byte-level deduplication workload over compressed image files, not as the sole evidence for large-scale backup-stream behavior.

To address this concern, the HSAIDS experiment should report results on at least two workload classes:

- the ISIC JPEG dataset, used as a compressed-file exact-content workload;
- a backup-like workload, such as versioned directory snapshots, VM/disk-image snapshots, synthetic shifted streams, or multi-GB repeated file trees.

The chunk-level HSAIDS implementation supports this broader evaluation because it operates over byte streams and produces per-chunk recipes, duplicate chunk counts, hot/cold lookup statistics, dedup ratio, average chunk size, and cold-index write metrics. The ISIC result should therefore be presented as one workload category rather than as the main proof of scalability.

## Concern 2: The implementation may only be performing whole-file matching rather than block/chunk-level deduplication.

HSAIDS performs chunk-level deduplication. Each file is split into variable-size chunks using content-defined chunking before duplicate lookup is performed. The chunker uses a Gear rolling hash and emits boundaries based on a mask rule, with the following default parameters:

```text
minimum chunk size: 2 KiB
target average chunk size: 8 KiB
maximum chunk size: 64 KiB
```

Each chunk is hashed independently with SHA-256 and inserted into the HSAIDS index. Files are represented as ordered recipes in the `files` and `file_chunks` tables, where each recipe entry stores the chunk hash, file offset, chunk size, container ID, and container offset.

The average realized chunk size is not assumed; it is measured during execution and exported as `avg_chunk_size` in `hsaids_statistics_<label>.csv` under the run's `--output-dir`.

## Concern 3: If chunking is used on JPEG files, how is the boundary-shift problem handled?

The boundary-shift problem is handled by content-defined chunking rather than fixed-size chunking. HSAIDS selects chunk boundaries from the byte content itself using a rolling Gear hash. This allows chunk boundaries to resynchronize after insertions or deletions, so unchanged byte regions can still deduplicate even when their absolute offsets shift.

For JPEG files specifically, we acknowledge an important limitation: JPEGs are compressed bitstreams, so visually similar images generally do not imply byte-identical chunks. Therefore, JPEG results should be interpreted as byte-level deduplication over compressed files. They are useful for identifying exact or near-container-level byte reuse, but backup-like versioned datasets are more appropriate for demonstrating CDC’s boundary-shift advantage.

## Concern 4: The Bayesian confidence stabilizes at 83.33%; what exactly does it mean?

The Bayesian confidence is not the true positive rate of duplicate detection. It is the posterior mean probability that a hot-layer lookup will succeed for the next chunk query.

The definition is:

```text
P_hot = (alpha + hot_hits) / (alpha + beta + hot_queries)
```

where:

```text
alpha = prior hot-hit success count
beta = prior hot-hit failure count
hot_hits = number of chunk queries answered by the hot layer
hot_queries = number of hot-layer lookup attempts
```

In the current configuration, `alpha = 1` and `beta = 1`. Thus, a value such as 83.33% means that, after incorporating the prior and observed lookups, the system estimates an 83.33% probability that the hot layer will answer a future chunk query. It does not mean duplicate-detection accuracy is 83.33%.

## Concern 5: The Bayes-risk reduction rule lacks explicit loss functions and live micro-cost definitions.

HSAIDS uses measured runtime micro-costs in nanoseconds. The relevant costs are:

```text
C_hot        = average measured hot-layer lookup time
C_cold       = average measured cold-layer lookup time
C_verify     = average measured exact digest verification time
C_cold_write = average measured cold-index update time
```

The lookup-order decision compares:

```text
Risk(hot first)  = C_hot + (1 - P_hot)  * C_cold
Risk(cold first) = C_cold + (1 - P_cold) * C_hot
```

The system selects the lookup order with the lower expected risk. These costs are measured during execution using wall-clock nanosecond timings around the hot lookup, cold lookup, exact digest verification path, and cold-index write path. The exported statistics include:

```text
bayes_risk_hot_first_ns
bayes_risk_cold_first_ns
micro_cost_hot_lookup_ns
micro_cost_cold_lookup_ns
micro_cost_verify_ns
micro_cost_cold_write_ns
```

## Concern 6: SSD-resident Bloom filters and sketches may cause write amplification; what WAF is measured?

The cold layer is disk-backed through SQLite. Chunk metadata, reference counts, file recipes, Bloom positions, and Count-Min Sketch counters are persisted in `cold_index.sqlite`.

HSAIDS reports application-level cold-index write amplification as:

```text
cold_index_waf =
    cold_index_physical_bytes_written / cold_index_logical_bytes_written
```

Here, logical bytes represent the metadata update size submitted by HSAIDS, while physical bytes are estimated from the observed growth of the SQLite database, WAL, and SHM files.

This metric is intentionally described as application-level WAF. It is not the internal NAND-level write amplification of a physical SSD controller. Device-level WAF would require additional hardware telemetry, such as NVMe SMART counters or block-device write counters collected before and after each run.

## Concern 7: What results should be added to make the evaluation convincing?

To make the evaluation match the proposed architecture, the results section should include:

- average, minimum, and maximum chunk size;
- total logical input size;
- physical unique chunk bytes stored;
- total chunks processed;
- unique chunks and duplicate chunk references;
- deduplication ratio;
- hot-layer hit count and cold-layer hit count;
- Bayesian confidence and Bayes-risk values;
- cold-index logical and physical write bytes;
- application-level cold-index WAF;
- GC reclaimed chunks and bytes;
- results on at least one backup-like multi-GB workload.

This framing makes the ISIC dataset a limited compressed-file workload and uses larger versioned or backup-style workloads to evaluate the architecture’s intended strengths.
