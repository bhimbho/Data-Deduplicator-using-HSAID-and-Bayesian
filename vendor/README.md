# Vendored third-party sources

Code in this directory is fetched from upstream projects for benchmarking purposes
and is not authored by this project. See `docs/COMPARISON_PLAN.md` for how it is used.

## triededup/

Source: https://github.com/lolrenceH/TrieDedup (commit at time of vendoring: master,
fetched 2026-08-03). License: Apache License 2.0 (see `triededup/LICENSE`).

Citation: Hu J, Luo S, Tian M, Ye AY. "TrieDedup: a fast trie-based deduplication
algorithm to handle ambiguous bases in high-throughput sequencing." BMC Bioinformatics.
2024;25:154. https://doi.org/10.1186/s12859-024-05775-w

We use `triededup/Python/lib/trie.py` and `triededup/Python/lib/pairwise.py` directly;
`comparisons/external/triededup_adapter.py` adapts them to deduplicate chunk-hash
digests rather than DNA sequences. Everything else under `triededup/` (Cpp, Java,
TrieDedupWrapper.py, test_data) is kept for reference/attribution but is not used by
our benchmark.
