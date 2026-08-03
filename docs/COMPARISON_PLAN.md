# Plan: Quantitative Comparison Against Existing De-duplication Methods

## Why this exists

Reviewer feedback on the submission (automation-4277307) was explicit:

> "Design of comparison with state-of-the-art systems is still quite weak. You do not
> provide quantitative experimental comparisons (plots, tables with numbers) supporting
> the advantages of the proposed approach according to the scientific standards."

Tables 3 and 4 in the current draft are qualitative ("Faster delta search", "High",
"Moderate", "Negligible") for every competing method. The only real, measured numbers
in the whole comparison are our own v1 → v2 lookup cost (135 μs → 99 μs, a 26.7%
reduction) — a comparison against our own baseline, not against an external system.
This plan replaces qualitative cells with real, run, measured numbers wherever
feasible, and is explicit about what remains out of scope and why.

## Scope decision on the three cited systems

| System | Verdict | Reason |
|---|---|---|
| **TrieDedup** | Adapt and run on our dataset | Pure Python, no I/O assumptions baked into the core trie/pairwise algorithm (`lib/trie.py`, `lib/pairwise.py`). Confirmed by inspecting the cloned repo: the trie dedup logic operates on arbitrary strings over a configurable alphabet (`--symbols`), not hardcoded to `ACGTN`. We can feed it our chunk hash digests instead of DNA sequences. |
| **LoopDelta** | Cite qualitatively only, not benchmarked | It's a systems-level backup framework (C/C++, containers, GC, restore cache) solving a different problem: delta-compressing *similar-but-not-identical* chunks that dedup already decided to keep. It is not a drop-in alternative exact-match dedup algorithm, so there's no fair apples-to-apples number to extract without reimplementing a large fraction of its I/O stack. Framing it as complementary future work (already partially true in the current draft) is the honest and defensible position. |
| **Learning-based record de-duplication [23]** | Cite qualitatively only, not benchmarked | Solves entity/record matching, not storage-level chunk lookup — different unit of deduplication entirely (records vs. content-defined chunks), different objective (classification accuracy vs. lookup latency). Forcing a number here would be a category error, and the review didn't specifically flag this row as the problem. |
| **Conventional HSAIDS v1 (no Bayesian ordering)** | Already qualitatively true, must be re-verified as a *real measured run*, not an assumed number | This is the strongest, most defensible baseline because it's the same codebase, same dataset, same experimental conditions — only the lookup-ordering strategy differs. |

**Net effect on Table 3 / Table 4:** two of five rows (ours v1, ours v2, and TrieDedup)
become real numbers under matched conditions. LoopDelta and the learning-based method
stay qualitative, but the text will say so explicitly ("reported in [17]/[23], not
independently reproduced under our conditions") instead of implying equivalence.

## What "real number" means here

For every method that gets benchmarked, report, at minimum:
- Lookup latency (mean, p50, p95 — not just mean, since HSAIDS's whole pitch is about
  cutting tail latency via hot/cold ordering)
- Throughput (chunks/sec or MB/sec processed)
- Memory footprint (peak RSS during the run)
- False-positive rate (where applicable — Bloom-filter-based methods only)
- Same dataset, same machine, same trial count (≥5 runs, report variance/stddev, not
  single-shot numbers)

## Step-by-step plan

### 1. Lock down the experimental harness
- Reuse `results_v1/` (conventional HSAIDS) and `results_v2/` (Bayesian HSAIDS) as-is;
  confirm the 135 μs / 99 μs figures in the paper actually come from
  `hsaids_statistics_v1.json` / `hsaids_statistics_v2.json` in this repo, not from an
  earlier back-of-envelope estimate. If they don't match, re-run and use the real
  numbers — do not keep a number in the paper that isn't reproducible from checked-in
  code.
- Add explicit multi-trial support to `run_cdc_hsaids.py` (currently appears to be a
  single run per invocation) so latency numbers can be reported with variance.

### 2. Build the naive exact-hash baseline (fills out "conventional HSAIDS v1" honestly)
- New script `baseline_hash_dedup.py`: same CDC chunker (`cdc_hsaids.CDCConfig`,
  `ingest_directory`) but a dead-simple lookup path — hash set / SQLite exact match,
  no Bloom filter, no Count-Min Sketch, no Bayesian hot/cold ordering.
- Run on the same Wikipedia dataset (`wiki_v1`/`wiki_v2` equivalent corpus) used for
  the existing v1/v2 results, same machine, ≥5 trials.
- This produces the real "conventional/static baseline" row the paper already claims
  to have (currently the 135 μs figure) but now with full methodology behind it.

### 3. Adapt TrieDedup to chunk-hash deduplication
- Vendor the relevant pieces of `lib/trie.py` and `lib/pairwise.py` (Apache 2.0
  licensed — attribution required, no redistribution restriction) into a thin adapter,
  `external/triededup_adapter.py`, rather than depending on the genomics-specific
  CLI/FASTA parsing in `TrieDedup.py`/`TrieDedupWrapper.py`.
- Feed it our chunk fingerprints (hex digest strings, alphabet `0-9a-f`, no ambiguous
  characters — set `max_missing=0` since exact chunk hashes have no equivalent of
  low-quality bases) instead of DNA reads.
- Run both TrieDedup's trie mode and its pairwise mode (pairwise is its own paper's
  slow baseline) on the same chunk set that HSAIDS v1/v2 and the naive baseline
  process, so all four numbers are on identical input.
- Record the same metrics (latency, throughput, memory) under the same trial protocol.
- Caveat to state explicitly in the paper: TrieDedup was designed for fixed-length
  biological reads with ambiguous bases; running it on hash digests removes its core
  differentiator (ambiguous-base tolerance) and exercises only its trie-matching
  speed. This is a fair speed/memory comparison, not a claim that TrieDedup was built
  for this use case.

### 4. Re-run compare_runs.py / extend it
- Extend `compare_runs.py` to ingest four result sets (naive baseline, HSAIDS v1,
  HSAIDS v2, TrieDedup-adapted) and emit one comparison table (CSV/JSON) plus the
  plot(s) the reviewer explicitly asked for ("plots, tables with numbers").
- Minimum plots: (a) lookup latency distribution (box or violin, not just mean bar) per
  method, (b) throughput vs. dataset size if we test multiple corpus sizes, (c) memory
  footprint bar chart.

### 5. Rewrite Tables 3 and 4 and surrounding text
- Table 3: replace qualitative "Lookup performance / Throughput / Memory reduction"
  cells for HSAIDS v1, HSAIDS v2, and TrieDedup with real numbers ± stddev. Keep
  LoopDelta and learning-based rows qualitative but relabel that column header or add a
  footnote: "Reported by original authors under their own experimental conditions; not
  independently reproduced here" — so it's never presented as equivalent-looking data
  next to numbers we actually measured.
- Table 4: same treatment — real values where measured, explicit "Not independently
  benchmarked" instead of implied qualitative parity.
- Update Section 5.4 prose to state dataset, hardware, trial count, and point to the
  new plots.
- Update the Conclusion's "Future study will benchmark..." paragraph — it currently
  promises future work; once TrieDedup is actually benchmarked, narrow that promise to
  what's still genuinely unbenchmarked (LoopDelta's I/O-integrated framework,
  learning-based record matching) rather than repeating a blanket statement that's now
  partially resolved.

### 6. Update `RESULTS_AND_DISCUSSION.md` / `REVIEWER_RESPONSE_NOTES.md`
- Add a direct point-by-point response mapped to this review comment, referencing the
  new tables/plots and explaining the LoopDelta/learning-based scoping decision so the
  next reviewer pass sees the reasoning, not just the output.

## Deliverables checklist
- [ ] `baseline_hash_dedup.py` (naive exact-match baseline)
- [ ] `external/triededup_adapter.py` (vendored trie/pairwise wrapped for hash inputs)
- [ ] Multi-trial run harness update in `run_cdc_hsaids.py` (or new `run_benchmark.py`)
- [ ] Extended `compare_runs.py` producing comparison table + latency/throughput/memory
      plots
- [ ] Updated `results_v*/` and new `results_triededup/`, `results_baseline/` output
      directories (gitignored, per existing `.gitignore` convention)
- [ ] Revised Table 3, Table 4, Section 5.4 text, and Conclusion paragraph in the paper
- [ ] Updated `REVIEWER_RESPONSE_NOTES.md` with point-by-point response to this comment

## Explicit non-goals (say so in the paper, don't silently skip)
- Not running LoopDelta's actual C++ framework against our dataset — out of scope for
  this response cycle given its integration cost; stated as future work.
- Not running the learning-based record-deduplication system — different unit of
  deduplication (records vs. chunks), stated as a scope boundary, not a limitation of
  our method.
- Not claiming TrieDedup "loses" on our benchmark in its native domain — we are
  explicit that we removed the ambiguous-base tolerance feature by testing on hash
  digests, so this is a narrow speed/memory comparison only.
