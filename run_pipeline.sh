#!/usr/bin/env bash
# Full Wikipedia HSAIDS pipeline — runs after wiki_v1 extraction is complete.
# Steps: wiki_v2 mutation → Pass 1 ingest → Pass 2 ingest → compare runs
set -euo pipefail

cd "$(dirname "$0")"

log() { echo "[pipeline] $(date '+%H:%M:%S')  $*"; }

# ── Step 1: Mutate wiki_v1 → wiki_v2 ────────────────────────────────────────
if [ ! -d data/wiki_v2 ]; then
    log "Generating wiki_v2 (10% mutated articles from wiki_v1)…"
    python3 data_prep/prepare_wikipedia.py \
      --output data/wiki_v2 \
      --mutate 0.10 \
      --source data/wiki_v1
    log "wiki_v2 ready."
else
    log "wiki_v2 already exists, skipping mutation step."
fi

# ── Step 2: Pass 1 — ingest wiki_v1 ─────────────────────────────────────────
if [ ! -f comparisons/results/hsaids_v1/hsaids_statistics_v1.json ]; then
    log "Pass 1: ingesting wiki_v1 into wiki_store…"
    python3 -m hsaids.run_cdc_hsaids data/wiki_v1 \
      --store-dir data/wiki_store \
      --output-dir comparisons/results/hsaids_v1 \
      --run-label v1 \
      --reset-store \
      --hot-capacity 500000 \
      --hot-bloom-size 2000003 \
      --hot-cms-width 1000003 \
      --commit-every 2000
    log "Pass 1 complete. Results in comparisons/results/hsaids_v1/"
else
    log "hsaids_v1/hsaids_statistics_v1.json exists, skipping Pass 1."
fi

# ── Step 3: Pass 2 — ingest wiki_v2 (same store, no reset) ──────────────────
log "Pass 2: ingesting wiki_v2 against existing wiki_store…"
python3 -m hsaids.run_cdc_hsaids data/wiki_v2 \
  --store-dir data/wiki_store \
  --output-dir comparisons/results/hsaids_v2 \
  --run-label v2 \
  --hot-capacity 500000 \
  --hot-bloom-size 2000003 \
  --hot-cms-width 1000003 \
  --commit-every 2000
log "Pass 2 complete. Results in comparisons/results/hsaids_v2/"

# ── Step 4: Compare runs ─────────────────────────────────────────────────────
log "Comparing v1 vs v2…"
python3 comparisons/compare_runs.py \
  comparisons/results/hsaids_v1/hsaids_statistics_v1.json \
  comparisons/results/hsaids_v2/hsaids_statistics_v2.json \
  --csv comparisons/results/comparison_v1_v2.csv \
  --json comparisons/results/comparison_v1_v2.json
log "Comparison written to comparisons/results/comparison_v1_v2.{csv,json}"

log "Pipeline complete. Open notebooks/wikipedia_analysis.ipynb to generate the presentation."
