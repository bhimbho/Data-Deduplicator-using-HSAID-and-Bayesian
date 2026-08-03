# Implementation Update Plan: Wikipedia Dump as Evaluation Dataset

## What the reviewer actually requires

The four questions reduce to two concrete gaps:

1. **Scale + format**: run on uncompressed, multi-GB text data so CDC chunking actually fires on non-trivial boundaries
2. **Metrics proof**: produce every metric the reviewer names — chunk sizes, WAF, Bayesian confidence definition, Bayes-risk values — from a real run, not synthetic data

---

## Phase 1 — Wikipedia data acquisition and preprocessing

**Goal**: produce a clean, flat directory of `.txt` files the existing `ingest_directory()` can walk without any code change.

| Step | What | Why |
|---|---|---|
| 1.1 | Download the English Wikipedia `articles.xml.bz2` dump from dumps.wikimedia.org (choose a recent monthly dump, ~22 GB compressed / ~85 GB uncompressed) | Provides real multi-GB uncompressed text — directly counters the 830 MB JPEG complaint |
| 1.2 | Write `prepare_wikipedia.py` using `mwxml` or raw SAX parsing to extract article text, one file per article, saved as UTF-8 `.txt` | CDC on plain text produces meaningful variable-size chunks; boundary-shift behavior is demonstrable |
| 1.3 | Organize output into versioned subdirectories: `wiki_v1/` (first 500k articles), `wiki_v2/` (same articles with 10% modified — simulating a backup revision) | This is the "backup-like versioned workload" the reviewer demands |

---

## Phase 2 — Tune HSAIDS parameters for text workloads

**Goal**: set CDC and hot-layer parameters appropriate for text scale, not the 2,357-file JPEG run.

| Parameter | Current default | Recommended for Wikipedia | Reason |
|---|---|---|---|
| `hot_capacity` | 50,000 | 500,000 | Wikipedia has millions of unique chunks; hot layer evicts too aggressively at 50k |
| `bloom_size` (hot) | 200,003 | 2,000,003 | Reduce false-positive rate at 10× item count |
| `cms_width` (hot) | 100,003 | 1,000,003 | Same rationale |
| `avg_chunk_size` | 8,192 | 8,192 | Keep same; reviewer wants to see the realized average, not change it |

These are CLI flags on `run_cdc_hsaids.py` — no code change needed, just documented invocation commands.

---

## Phase 3 — Two-pass versioned run (the backup simulation)

**Goal**: demonstrate boundary-shift deduplication, which is the core claim the reviewer disputes.

| Pass | Input | Purpose |
|---|---|---|
| Pass 1 | `wiki_v1/` (original articles) | Populate index; measure baseline chunk distribution |
| Pass 2 | `wiki_v2/` (modified articles, same store, `--reset-store` NOT set) | Measure cross-version dedup ratio; proves CDC resynchronizes after edits |

The delta between Pass 1 and Pass 2 statistics directly answers: "is this file-level or chunk-level dedup?"

---

## Phase 4 — Extend `run_cdc_hsaids.py` for per-pass comparison output

**What to add** (minimal code change):

- `--run-label` argument: tags each pass's JSON output with a label (`v1`, `v2`)
- Side-by-side summary printed at end of Pass 2 showing: logical bytes ingested, unique chunks added, duplicate chunks found, dedup ratio, and Bayesian confidence — per pass
- A `compare_runs.py` script that reads two `hsaids_statistics.json` files and prints a diff table

No changes to `cdc_hsaids.py` itself — the engine already tracks all required metrics.

---

## Phase 5 — Reviewer response document update

After the run produces real numbers, update `REVIEWER_RESPONSE_NOTES.md` with:

| Concern | What to insert |
|---|---|
| 1 (scale) | Actual total logical bytes from Wikipedia run (expected: 80–90 GB) |
| 2 (file vs chunk) | Pass 2 duplicate chunk count with cross-version boundary evidence |
| 3 (boundary-shift on JPEG) | Acknowledge limitation explicitly; cite Wikipedia versioned run as the correct workload |
| 4 (Bayesian confidence) | Show the stabilized value from the real run alongside the mathematical definition |
| 5 (loss functions) | Insert the actual measured micro-cost values in nanoseconds from the run |
| 6 (WAF) | Insert `cold_index_waf` from statistics JSON; clarify it is application-level, not NAND-level |

---

## File changes summary

| File | Action |
|---|---|
| `prepare_wikipedia.py` | **New** — download + parse Wikipedia XML dump into per-article `.txt` files |
| `compare_runs.py` | **New** — diff two `hsaids_statistics.json` files, print comparison table |
| `run_cdc_hsaids.py` | **Edit** — add `--run-label` flag, save labeled output, print per-pass summary |
| `REVIEWER_RESPONSE_NOTES.md` | **Edit** — fill in real numbers after run completes |
| `README.md` | **Edit** — update dataset section, add Wikipedia run instructions |

`cdc_hsaids.py` and `tests/test_cdc_hsaids.py` require **no changes** — the engine already produces every metric the reviewer asks for.

---

## Execution order

```bash
# Step 1: prepare both dataset versions
python prepare_wikipedia.py --output wiki_v1 --limit 500000
python prepare_wikipedia.py --output wiki_v2 --limit 500000 --mutate 0.10

# Step 2: Pass 1 — populate index from original articles
python run_cdc_hsaids.py wiki_v1 \
    --store-dir wiki_store \
    --output-dir results_v1 \
    --run-label v1 \
    --reset-store \
    --hot-capacity 500000

# Step 3: Pass 2 — ingest modified articles against the same store
python run_cdc_hsaids.py wiki_v2 \
    --store-dir wiki_store \
    --output-dir results_v2 \
    --run-label v2 \
    --hot-capacity 500000

# Step 4: compare and report
python compare_runs.py results_v1/hsaids_statistics.json results_v2/hsaids_statistics.json
```

---

## Expected outcomes per concern

| Reviewer concern | Evidence produced |
|---|---|
| Dataset too small (830 MB) | Wikipedia run covers 80–90 GB logical input |
| File-level vs chunk-level dedup | Pass 2 shows duplicate chunks from modified articles — files differ at whole-file hash but share chunks |
| Boundary-shift on JPEGs | Explicitly scoped as a known limitation; Wikipedia text is the appropriate workload |
| Bayesian confidence meaning | Real stabilized value printed alongside the Beta-posterior definition |
| Loss functions / micro-costs | Actual nanosecond timings from the run fill in the placeholders |
| SSD WAF | `cold_index_waf` from statistics JSON; documented as application-level, not NAND-level |
