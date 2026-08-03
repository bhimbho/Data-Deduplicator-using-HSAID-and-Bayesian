#!/usr/bin/env bash
# Full Wikipedia HSAIDS pipeline — runs after wiki_v1 extraction is complete.
# Steps: wiki_v2 mutation → Pass 1 ingest → Pass 2 ingest → compare runs
set -euo pipefail

cd "$(dirname "$0")"

log() { echo "[pipeline] $(date '+%H:%M:%S')  $*"; }

# ── Step 1: Mutate wiki_v1 → wiki_v2 ────────────────────────────────────────
if [ ! -d wiki_v2 ]; then
    log "Generating wiki_v2 (10% mutated articles from wiki_v1)…"
    python3 prepare_wikipedia.py \
      --output wiki_v2 \
      --mutate 0.10 \
      --source wiki_v1
    log "wiki_v2 ready."
else
    log "wiki_v2 already exists, skipping mutation step."
fi

# ── Step 2: Pass 1 — ingest wiki_v1 ─────────────────────────────────────────
if [ ! -f results_v1/hsaids_statistics_v1.json ]; then
    log "Pass 1: ingesting wiki_v1 into wiki_store…"
    python3 run_cdc_hsaids.py wiki_v1 \
      --store-dir wiki_store \
      --output-dir results_v1 \
      --run-label v1 \
      --reset-store \
      --hot-capacity 500000 \
      --hot-bloom-size 2000003 \
      --hot-cms-width 1000003 \
      --commit-every 2000
    log "Pass 1 complete. Results in results_v1/"
else
    log "results_v1/hsaids_statistics_v1.json exists, skipping Pass 1."
fi

# ── Step 3: Pass 2 — ingest wiki_v2 (same store, no reset) ──────────────────
log "Pass 2: ingesting wiki_v2 against existing wiki_store…"
python3 run_cdc_hsaids.py wiki_v2 \
  --store-dir wiki_store \
  --output-dir results_v2 \
  --run-label v2 \
  --hot-capacity 500000 \
  --hot-bloom-size 2000003 \
  --hot-cms-width 1000003 \
  --commit-every 2000
log "Pass 2 complete. Results in results_v2/"

# ── Step 4: Compare runs ─────────────────────────────────────────────────────
log "Comparing v1 vs v2…"
python3 compare_runs.py \
  results_v1/hsaids_statistics_v1.json \
  results_v2/hsaids_statistics_v2.json \
  --csv comparison.csv \
  --json comparison.json
log "Comparison written to comparison.csv and comparison.json"

log "Pipeline complete. Open wikipedia_analysis.ipynb to generate the presentation."
